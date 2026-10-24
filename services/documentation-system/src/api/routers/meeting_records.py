from uuid import UUID
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict
from typing import Literal

from contracts.directory import DirectoryClient, DirectoryUnavailable
from contracts.embedder import Embedder
from contracts.meeting import MeetingPayload, MeetingRetryResult, MeetingWriter, PersistenceStatus
from contracts.storage import StorageAdapter
from src.api.auth import get_actor, require_scope
from src.api.deps import (
    get_directory,
    get_embedder,
    get_meeting_writer,
    get_storage,
    get_target_embedder,
)
from src.config import get_settings
from src.meeting_records import MeetingRecords

router = APIRouter(prefix="/meeting-records", tags=["meeting records"])
logger = logging.getLogger("documentation_system.audit")


def _advance_once(records, meeting_id):
    try:
        records.advance(meeting_id)
    except Exception:
        # An adapter failure may contain SQL parameters, including transcript.
        # Acceptance is already durable: leave recovery to retry-due.
        logger.warning("meeting.background_failed meeting_id=%s", meeting_id)


def get_records(
    storage: StorageAdapter = Depends(get_storage),
    writer: MeetingWriter = Depends(get_meeting_writer),
    directory: DirectoryClient = Depends(get_directory),
    embedder: Embedder = Depends(get_embedder),
    target: Embedder | None = Depends(get_target_embedder),
):
    return MeetingRecords(
        storage, writer, directory, get_settings(), embedder=embedder, target=target
    )


@router.post("/retry-due", response_model=MeetingRetryResult)
def retry_due(
    limit: int = Query(default=10, ge=1, le=100),
    _key=Depends(require_scope("meetings:write")),
    records=Depends(get_records),
):
    return records.retry_due(limit)


@router.get("")
def list_records(
    persistence_status: PersistenceStatus | None = None,
    delivery_status: Literal["pending", "posted", "failed", "disabled"] | None = None,
    _key=Depends(require_scope("meetings:read")),
    records=Depends(get_records),
):
    return [
        r.public()
        for r in records.storage.list_meetings()
        if (persistence_status is None or r.persistence_status == persistence_status)
        and (delivery_status is None or r.delivery_status == delivery_status)
    ]


@router.put("/{meeting_id}")
def accept(
    meeting_id: UUID,
    body: MeetingPayload,
    response: Response,
    tasks: BackgroundTasks,
    _key=Depends(require_scope("meetings:write")),
    actor=Depends(get_actor),
    records=Depends(get_records),
):
    try:
        record, created = records.accept(meeting_id, body, actor=actor)
    except ValueError:
        raise HTTPException(409, "meeting payload conflict") from None
    except DirectoryUnavailable:
        raise HTTPException(503, "meeting audience unavailable; retry hand-off") from None
    except Exception:
        raise HTTPException(503, "meeting hand-off unavailable; retry the same payload") from None
    response.status_code = 202 if created else 200
    if created:
        tasks.add_task(_advance_once, records, meeting_id)
    return record.public()


@router.get("/{meeting_id}")
def get_record(
    meeting_id: UUID, _key=Depends(require_scope("meetings:read")), records=Depends(get_records)
):
    record = records.storage.get_meeting(meeting_id)
    if record is None:
        raise HTTPException(404, "meeting record not found")
    return record.public()


@router.post("/{meeting_id}/retry")
def retry_record(
    meeting_id: UUID, _key=Depends(require_scope("meetings:admin")), records=Depends(get_records)
):
    if records.storage.get_meeting(meeting_id) is None:
        raise HTTPException(404, "meeting record not found")
    records.advance(meeting_id, explicit=True)
    return records.storage.get_meeting(meeting_id).public()


@router.post("/{meeting_id}/remove")
def remove_record(
    meeting_id: UUID,
    _key=Depends(require_scope("meetings:admin")),
    actor=Depends(get_actor),
    records=Depends(get_records),
):
    record = records.remove(meeting_id, actor=actor)
    if record is None:
        raise HTTPException(404, "meeting record not found")
    return record.public()


class Delivery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["posted", "failed"]
    discord_message_id: str | None = None


@router.patch("/{meeting_id}/delivery")
def delivery(
    meeting_id: UUID,
    body: Delivery,
    _key=Depends(require_scope("meetings:write")),
    actor=Depends(get_actor),
    records=Depends(get_records),
):
    record = records.storage.get_meeting(meeting_id)
    if record is None:
        raise HTTPException(404, "meeting record not found")
    if record.removed_at or record.persistence_status != "saved":
        raise HTTPException(409, "record is not available for delivery")
    if body.status == "posted" and not body.discord_message_id:
        raise HTTPException(422, "posted delivery requires discord_message_id")
    updated = records.storage.change_meeting(
        record,
        {
            "delivery_status": body.status,
            "discord_message_id": body.discord_message_id,
        },
        actor=actor,
    )
    if updated is None:
        raise HTTPException(409, "record changed; retry")
    return updated.public()
