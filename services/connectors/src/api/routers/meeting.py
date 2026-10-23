from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from contracts.meeting import CreateDoc, MeetingSearch, MeetingWriter, MeetingWriteError, Sharing
from src.api.auth import require_scope
from src.api.deps import get_meeting_writer

router = APIRouter(prefix="/google/docs", tags=["meeting records"])
DocumentId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,200}$")]


def _call(fn, *args):
    try:
        return fn(*args)
    except MeetingWriteError as exc:
        status = 503 if exc.code == "writer_not_configured" else 502 if exc.uncertain else 409
        raise HTTPException(status, detail={"code": exc.code, "uncertain": exc.uncertain}) from None


@router.post("")
def create(
    body: CreateDoc,
    _key=Depends(require_scope("meeting-write")),
    writer: MeetingWriter = Depends(get_meeting_writer),
):
    return _call(writer.create, body)


@router.post("/search")
def search(
    body: MeetingSearch,
    _key=Depends(require_scope("meeting-write")),
    writer: MeetingWriter = Depends(get_meeting_writer),
):
    return _call(writer.search, body.meeting_id)


@router.put("/{document_id}/sharing")
def share(
    document_id: DocumentId,
    body: Sharing,
    _key=Depends(require_scope("meeting-write")),
    writer: MeetingWriter = Depends(get_meeting_writer),
):
    return _call(writer.share, document_id, body.commenters)


@router.post("/{document_id}/fetch")
def fetch(
    document_id: DocumentId,
    _key=Depends(require_scope("meeting-fetch")),
    writer: MeetingWriter = Depends(get_meeting_writer),
):
    return _call(writer.fetch, document_id)


@router.get("/{document_id}/status")
def status(
    document_id: DocumentId,
    _key=Depends(require_scope("meeting-fetch")),
    writer: MeetingWriter = Depends(get_meeting_writer),
):
    return {"status": _call(writer.status, document_id)}


@router.delete("/{document_id}")
def delete(
    document_id: DocumentId,
    _key=Depends(require_scope("meeting-write")),
    writer: MeetingWriter = Depends(get_meeting_writer),
):
    _call(writer.delete, document_id)
    return {"status": "deleted"}
