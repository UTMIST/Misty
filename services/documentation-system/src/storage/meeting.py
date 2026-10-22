"""Pure validation shared by both meeting storage adapters."""

from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from contracts.meeting import MeetingRecord
from src.content import clamp_content, content_hash
from src.url_norm import normalize_url


def changed(record, values, actor):
    if (
        record.google_document_id
        and values.get("google_document_id", record.google_document_id) != record.google_document_id
    ):
        raise ValueError("canonical document is immutable")
    if record.removed_at and (
        values.get("removed_at", record.removed_at) != record.removed_at
        or values.get("persistence_status", record.persistence_status)
        not in {"removing", "removed"}
    ):
        raise ValueError("removed meeting cannot reopen")
    return MeetingRecord.model_validate(
        record.model_dump()
        | values
        | {
            "version": record.version + 1,
            "updated_at": datetime.now(timezone.utc),
            "updated_by": actor,
        }
    )


def catalog_values(record, result, actor):
    now = datetime.now(timezone.utc)
    body, _ = clamp_content(result.content or "")
    doc_id = uuid5(NAMESPACE_URL, f"misty:meeting:{record.meeting_id}")
    values = dict(
        id=doc_id,
        url=record.doc_url,
        url_normalized=normalize_url(record.doc_url),
        source_id="gdocs",
        title=result.title or record.title,
        description=None,
        owning_person_id=record.requester_person_id,
        owning_person_label=None,
        owning_team_id=record.audience_team_ids[0] if len(record.audience_team_ids) == 1 else None,
        owning_team_label=None,
        content_snapshot=body[:4000] or None,
        fetched_at=now,
        source_access_synced_at=now,
        source_access_attempted_at=now,
        created_at=now,
        updated_at=now,
        created_by=actor,
        updated_by=actor,
        active=True,
    )
    return values, body, content_hash(body), now
