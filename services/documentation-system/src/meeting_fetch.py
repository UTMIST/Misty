"""Select the owning writer for meeting Docs in every catalog refresh path."""

from contracts.fetcher import FetchError
from contracts.meeting import WriterError
from src.google_identity import google_file_id


def fetch_catalog_content(source_id, url, *, storage, fetchers):
    file_id = google_file_id(url)
    record = storage.get_meeting_by_google_id(file_id) if file_id else None
    if record is None:
        return fetchers.fetch_for(source_id, url)
    if record.removed_at:
        raise FetchError("meeting record removed")
    writer = getattr(fetchers, "meeting_writer", None)
    if writer is None:
        raise FetchError("meeting owner fetch unavailable")
    try:
        return writer.fetch(file_id)
    except WriterError as exc:
        if exc.code == "google_404":
            try:
                state = writer.status(file_id)
            except WriterError:
                state = "inaccessible"
            if state in {"deleted", "trashed"}:
                # Confirmed removal uses the full cascade, never SourceGone's
                # generic partial cleanup. Import avoids a dependency cycle.
                from src.meeting_records import MeetingRecords
                from src.config import get_settings

                MeetingRecords(storage, writer, None, get_settings()).remove(
                    record.meeting_id, actor="source-removed"
                )
            else:
                storage.change_meeting(
                    record,
                    {"persistence_status": "inaccessible", "last_error": "owner_inaccessible"},
                    actor="meeting-owner-fetch",
                )
        raise FetchError("meeting owner fetch failed") from None
