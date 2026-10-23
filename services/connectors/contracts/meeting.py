"""Authenticated meeting writer boundary. No vendor or application imports."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from contracts.fetch import FetchResponse, SourcePermission


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class CreateDoc(StrictModel):
    meeting_id: UUID
    attempt_id: UUID
    voice_channel_id: str = Field(pattern=r"^[0-9]{1,20}$")
    title: str = Field(min_length=1, max_length=200)
    html: str = Field(min_length=1, max_length=4_000_000, repr=False)


class MeetingSearch(StrictModel):
    meeting_id: UUID


class Sharing(StrictModel):
    commenters: list[str] = Field(default_factory=list, max_length=100)


class GoogleDoc(StrictModel):
    document_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    url: str


class TaggedDoc(StrictModel):
    document_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,200}$")
    attempt_id: UUID
    created_at: datetime
    trashed: bool


class MeetingWriteError(Exception):
    """Content-free code; uncertain=True forbids blind create retries."""

    def __init__(self, code: str, *, uncertain: bool = False):
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


class MeetingWriter(Protocol):
    def create(self, body: CreateDoc) -> GoogleDoc: ...
    def search(self, meeting_id: UUID) -> list[TaggedDoc]: ...
    def share(self, document_id: str, commenters: list[str]) -> list[SourcePermission]: ...
    def fetch(self, document_id: str) -> FetchResponse: ...
    def status(self, document_id: str) -> str: ...
    def delete(self, document_id: str) -> None: ...
