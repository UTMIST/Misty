from typing import Literal
from uuid import UUID

from anyio import from_thread
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from contracts.directory import DirectoryClient
from contracts.email import (
    EmailPollResult,
    GmailClient,
    GmailError,
    ReceiptStatus,
    SubmissionStatus,
)
from contracts.storage import StorageAdapter
from platform_auth import AuthedKey
from src.api.auth import get_actor, get_on_behalf_actor, require_api_key, require_scope
from src.api.deps import get_directory, get_gmail_client, get_storage
from src.config import get_settings
from src.email_submissions import EmailSubmissions

router = APIRouter(prefix="/email-submissions", tags=["email submissions"])


def get_email_service(
    storage: StorageAdapter = Depends(get_storage),
    gmail: GmailClient = Depends(get_gmail_client),
    directory: DirectoryClient = Depends(get_directory),
) -> EmailSubmissions:
    return EmailSubmissions(storage, gmail, directory, get_settings())


def _submission_read_scope(
    actor_id: UUID | None = Depends(get_on_behalf_actor),
    key: AuthedKey = Depends(require_api_key),
) -> UUID | None:
    """Return submitter UUID filter, or None for an operator emails:admin caller."""
    if actor_id is not None:
        if not (key.has_scope("emails:read") or key.has_scope("emails:admin")):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="missing scope: emails:read",
            )
        return actor_id
    if key.has_scope("emails:admin"):
        return None
    if key.has_scope("emails:read"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="X-On-Behalf-Of required without emails:admin",
        )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="missing scope: emails:read",
    )


class FailSubmissionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    error_code: str = Field(pattern=r"^[a-z0-9_]{1,64}$")


# Literal routes registered before /{submission_id}.
@router.post("/poll-runs/{run_id}")
def acquire_email_poll_run(
    run_id: UUID,
    storage: StorageAdapter = Depends(get_storage),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("emails:write")),
) -> dict[str, UUID]:
    if not storage.acquire_email_poll_run(run_id, actor=actor):
        raise HTTPException(status_code=409, detail="email poll run already active")
    return {"run_id": run_id}


@router.delete("/poll-runs/{run_id}")
def release_email_poll_run(
    run_id: UUID,
    storage: StorageAdapter = Depends(get_storage),
    _: AuthedKey = Depends(require_scope("emails:write")),
) -> dict[str, UUID]:
    if not storage.release_email_poll_run(run_id):
        raise HTTPException(status_code=409, detail="email poll run owner does not match")
    return {"run_id": run_id}


@router.post("/poll", response_model=EmailPollResult)
def poll_email_submissions(
    request: Request,
    limit: int = Query(default=25, ge=1, le=100),
    max_pages: int = Query(default=5, ge=1, le=20),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("emails:write")),
    service: EmailSubmissions = Depends(get_email_service),
) -> EmailPollResult:
    try:
        result = service.poll(
            limit=limit,
            max_pages=max_pages,
            actor=actor,
            disconnected=lambda: from_thread.run(request.is_disconnected),
        )
    except GmailError as exc:
        code = 503 if exc.code == "gmail_not_configured" else 502
        raise HTTPException(
            status_code=code,
            detail={"code": exc.code, "uncertain": exc.uncertain},
        ) from None
    request.state.audit_extra = {
        "accepted": result.accepted,
        "duplicates": result.duplicates,
        "complete": result.complete,
    }
    return result


@router.get("")
def list_email_submissions(
    status_filter: SubmissionStatus | None = Query(default=None, alias="status"),
    receipt_status: ReceiptStatus | None = None,
    submitter_filter: UUID | None = Depends(_submission_read_scope),
    service: EmailSubmissions = Depends(get_email_service),
) -> list[dict]:
    rows = service.storage.list_email_submissions(
        submitter_person_id=submitter_filter,
        status=status_filter,
        receipt_status=receipt_status,
    )
    return [r.public() for r in rows]


@router.get("/{submission_id}")
def get_email_submission(
    submission_id: UUID,
    submitter_filter: UUID | None = Depends(_submission_read_scope),
    service: EmailSubmissions = Depends(get_email_service),
) -> dict:
    sub = service.storage.get_email_submission(submission_id)
    if sub is None or (
        submitter_filter is not None and sub.submitter_person_id != submitter_filter
    ):
        raise HTTPException(status_code=404, detail="email submission not found")
    return sub.public()


@router.post("/{submission_id}/fail")
def fail_email_submission(
    submission_id: UUID,
    body: FailSubmissionBody,
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("emails:admin")),
    service: EmailSubmissions = Depends(get_email_service),
) -> dict:
    try:
        updated = service.mark_failed(submission_id, error_code=body.error_code, actor=actor)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if updated is None:
        raise HTTPException(status_code=404, detail="email submission not found")
    return updated.public()


@router.post("/{submission_id}/retry-notifications")
def retry_submission_notifications(
    submission_id: UUID,
    kind: Literal["receipt", "failure_notice"] = Query(default="receipt"),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("emails:admin")),
    service: EmailSubmissions = Depends(get_email_service),
) -> dict:
    sub = service.storage.get_email_submission(submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="email submission not found")
    status_field = f"{kind}_status"
    cur = getattr(sub, status_field)
    if cur == "failed":
        now = service.now()
        sub = (
            service.storage.change_email_submission(
                sub,
                {
                    status_field: "pending",
                    f"{kind}_attempts": 0,
                    "next_attempt_at": now,
                },
                actor=actor,
                now=now,
            )
            or sub
        )
    service._advance_slot(submission_id, kind=kind, actor=actor)
    latest = service.storage.get_email_submission(submission_id)
    if latest is None:
        raise HTTPException(status_code=404, detail="email submission not found")
    return latest.public()
