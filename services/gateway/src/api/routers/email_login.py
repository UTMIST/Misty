import logging
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response

from contracts.directory import DirectoryClient
from contracts.login import (
    EmailConfirmIn,
    EmailConfirmOut,
    EmailStartIn,
    EmailStartOut,
    IssuerOut,
    LoginChallenge,
    PersonStatusOut,
    VerificationClient,
)
from contracts.storage import StorageAdapter
from src import login_policy as policy
from src.api.auth import AuthedKey, require_scope
from src.api.deps import get_directory, get_storage, get_verification
from src.config import get_settings

logger = logging.getLogger("gateway.login")
router = APIRouter(prefix="/v1/auth", tags=["email login"])


def _issuer() -> str:
    return f"misty:{get_settings().gateway_env}"


def _enabled():
    if not get_settings().email_login_enabled:
        raise HTTPException(503, "email login unavailable")


def _quota(storage, key, operation, client_ip=None):
    _enabled()
    if not storage.allow_login_request(key.name, operation, client_ip, policy.now()):
        raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "60"})


def _eligible(person: dict | None, email: str | None = None) -> UUID | None:
    if person is None:
        return None
    # Reject malformed upstream data, including truthy strings for active.
    from contracts.directory import DirectoryUnavailable

    try:
        if not isinstance(person, dict) or not isinstance(person.get("id"), str):
            raise ValueError()
        person_id = UUID(person["id"])
        if type(person["active"]) is not bool or not isinstance(person["primary_email"], str):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise DirectoryUnavailable("invalid directory person response") from None
    if not person["active"] or (email is not None and person["primary_email"].lower() != email):
        return None
    return person_id


def _deliver(challenge, storage, directory, verification):
    # Runs after the generic response has been sent. Upstream latency and failures
    # must not disclose account existence. No retries after ambiguous delivery.
    try:
        person_id = _eligible(directory.get_person_by_email(challenge.email), challenge.email)
        if person_id is None:
            return
        verification.request_code(f"gateway-login:{challenge.id}", challenge.email)
        storage.activate_login(challenge.id, person_id, policy.now())
    except Exception:
        # Exceptions may carry URLs, SQL parameters or provider response bodies.
        logger.warning("email login delivery failed")


@router.post("/email/start", response_model=EmailStartOut, status_code=202)
def start(
    payload: EmailStartIn,
    background: BackgroundTasks,
    response: Response,
    key: AuthedKey = Depends(require_scope("auth:email")),
    storage: StorageAdapter = Depends(get_storage),
    directory: DirectoryClient = Depends(get_directory),
    verification: VerificationClient = Depends(get_verification),
) -> EmailStartOut:
    _quota(storage, key, "start", str(payload.client_ip))
    response.headers["Cache-Control"] = "no-store"
    now = policy.now()
    challenge = LoginChallenge(
        id=uuid4(), owner=key.name, email=payload.email, created_at=now, expires_at=now + policy.TTL
    )
    if storage.reserve_login(challenge):
        background.add_task(_deliver, challenge, storage, directory, verification)
    # Mailbox throttling returns an indistinguishable, nonredeemable challenge.
    return EmailStartOut(challenge_id=challenge.id, issuer=_issuer())


@router.post("/email/confirm", response_model=EmailConfirmOut)
def confirm(
    payload: EmailConfirmIn,
    response: Response,
    key: AuthedKey = Depends(require_scope("auth:email")),
    storage: StorageAdapter = Depends(get_storage),
    directory: DirectoryClient = Depends(get_directory),
    verification: VerificationClient = Depends(get_verification),
) -> EmailConfirmOut:
    _quota(storage, key, "confirm", str(payload.client_ip))
    response.headers["Cache-Control"] = "no-store"
    invalid = HTTPException(400, "invalid or expired login challenge")
    challenge = storage.admit_login_attempt(payload.challenge_id, key.name, policy.now())
    if challenge is None:
        raise invalid
    # Unknown/inactive/pending challenges take the same private verification
    # path: otherwise latency or an outage's 400/503 difference reveals members.
    email = verification.confirm_code(
        f"gateway-login:{challenge.id}", payload.code.get_secret_value()
    )
    if challenge.person_id is None or email != challenge.email:
        raise invalid
    person_id = _eligible(directory.get_person(str(challenge.person_id)), challenge.email)
    if person_id != challenge.person_id:
        raise invalid
    if not storage.redeem_login(challenge.id, key.name, policy.now()):
        raise invalid
    return EmailConfirmOut(person_id=person_id, email=challenge.email, issuer=_issuer())


@router.get("/issuer", response_model=IssuerOut)
def issuer(
    response: Response,
    key: AuthedKey = Depends(require_scope("auth:status")),
    storage: StorageAdapter = Depends(get_storage),
) -> IssuerOut:
    _quota(storage, key, "status")
    response.headers["Cache-Control"] = "no-store"
    return IssuerOut(issuer=_issuer())


@router.get("/people/{person_id}/status", response_model=PersonStatusOut)
def status(
    person_id: UUID,
    response: Response,
    key: AuthedKey = Depends(require_scope("auth:status")),
    storage: StorageAdapter = Depends(get_storage),
    directory: DirectoryClient = Depends(get_directory),
) -> PersonStatusOut:
    _quota(storage, key, "status")
    response.headers["Cache-Control"] = "no-store"
    return PersonStatusOut(
        active=_eligible(directory.get_person(str(person_id))) == person_id, issuer=_issuer()
    )
