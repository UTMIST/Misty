"""Durable meeting hand-off and injected Google writer contract."""

from datetime import datetime
from html import escape
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from contracts.fetcher import FetchResult, SourcePermission


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


Snowflake = Annotated[str, Field(pattern=r"^[0-9]{1,20}$")]


class Participant(StrictModel):
    discord_id: Snowflake
    display_name: str = Field(min_length=1, max_length=200)


class MeetingPayload(StrictModel):
    guild_id: Snowflake
    text_channel_id: Snowflake
    voice_channel_id: Snowflake
    requester_discord_id: Snowflake
    started_at: AwareDatetime
    ended_at: AwareDatetime
    title: str = Field(min_length=1, max_length=200)
    participants: list[Participant] = Field(default_factory=list, max_length=1000)
    summary: str = Field(max_length=1_000_000, repr=False)
    decisions: list[str] = Field(default_factory=list, max_length=1000)
    action_items: list[str] = Field(default_factory=list, max_length=1000)
    transcript: str = Field(max_length=1_000_000, repr=False)

    @model_validator(mode="after")
    def valid_times(self):
        if self.ended_at < self.started_at:
            raise ValueError("ended_at precedes started_at")
        if len({p.discord_id for p in self.participants}) != len(self.participants):
            raise ValueError("duplicate participant")
        text = [
            self.title,
            self.summary,
            self.transcript,
            *self.decisions,
            *self.action_items,
            *(p.display_name for p in self.participants),
        ]
        try:
            if any("\x00" in t for t in text) or sum(len(escape(t)) for t in text) > 3_500_000:
                raise ValueError("meeting content exceeds transport limits or contains NUL")
            for value in text:
                value.encode("utf-8")
        except UnicodeError:
            raise ValueError("meeting content must be valid UTF-8") from None
        return self


PersistenceStatus = Literal[
    "pending",
    "creating",
    "create_uncertain",
    "doc_created",
    "shared",
    "saved",
    "failed",
    "inaccessible",
    "removing",
    "removed",
]


class MeetingRecord(StrictModel):
    meeting_id: UUID
    payload_hash: str
    version: int = 0
    doc_id: UUID | None = None
    google_document_id: str | None = None
    google_deleted_at: datetime | None = None
    removed_google_document_ids: list[str] = []
    index_pending: bool = False
    doc_url: str | None = None
    guild_id: str | None = None
    text_channel_id: str | None = None
    voice_channel_id: str | None = None
    requester_discord_id: str | None = None
    requester_person_id: UUID | None = None
    title: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    participants: list[dict] = []
    audience_team_ids: list[UUID] = []
    audience_group_emails: list[str] = []
    summary: str | None = Field(default=None, repr=False)
    decisions: list[str] = []
    action_items: list[str] = []
    generated_at: datetime | None = None
    failed_at: datetime | None = None
    pending_payload: dict | None = Field(default=None, repr=False)
    persistence_status: PersistenceStatus = "pending"
    create_attempt_id: UUID | None = None
    create_lease_until: datetime | None = None
    next_reconcile_at: datetime | None = None
    attempts: int = 0
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    delivery_status: Literal["pending", "posted", "failed", "disabled"] = "pending"
    discord_message_id: str | None = None
    removed_at: datetime | None = None
    removed_by: str | None = None
    created_at: datetime
    updated_at: datetime
    created_by: str
    updated_by: str

    def public(self):
        return self.model_dump(exclude={"pending_payload", "payload_hash", "audience_group_emails"})


class MeetingRetryResult(StrictModel):
    attempted: int = Field(default=0, strict=True, ge=0)
    succeeded: int = Field(default=0, strict=True, ge=0)
    failed: int = Field(default=0, strict=True, ge=0)
    persistence_failed: int = Field(default=0, strict=True, ge=0)
    index_failed: int = Field(default=0, strict=True, ge=0)
    cleanup_failed: int = Field(default=0, strict=True, ge=0)

    @model_validator(mode="after")
    def consistent_counts(self):
        categories = (self.persistence_failed, self.index_failed, self.cleanup_failed)
        if (
            self.succeeded + self.failed != self.attempted
            or max(categories) > self.failed
            or sum(categories) < self.failed
        ):
            raise ValueError("inconsistent meeting retry counts")
        return self


class WriterError(Exception):
    def __init__(self, code: str, *, uncertain: bool = False):
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


class TaggedDoc(StrictModel):
    document_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,200}$")
    attempt_id: UUID
    created_at: AwareDatetime
    trashed: bool


class MeetingWriter(Protocol):
    def create(
        self, *, meeting_id: UUID, attempt_id: UUID, voice_channel_id: str, title: str, html: str
    ) -> str: ...
    def search(self, meeting_id: UUID) -> list[TaggedDoc]: ...
    def share(self, document_id: str, commenters: list[str]) -> list[SourcePermission]: ...
    def fetch(self, document_id: str) -> FetchResult: ...
    def status(self, document_id: str) -> str: ...
    def delete(self, document_id: str) -> None: ...
