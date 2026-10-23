from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Protocol


class Minutes(BaseModel):
    title: str = ""  # LLM-generated meeting title (empty -> PDF uses a fallback)
    summary: str
    decisions: list[str] = []
    action_items: list[str] = []


class Segment(BaseModel):
    speaker: str
    start_ms: int
    text: str


class TranscriptView(BaseModel):
    segments: list[Segment] = []


class StopResponse(BaseModel):
    transcript: str
    minutes: Minutes
    pdf_b64: str
    record: "RecordResult | None" = None


class RecordParticipant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discord_id: str = Field(pattern=r"^[0-9]{1,20}$")
    display_name: str = Field(min_length=1, max_length=200)


class RecordContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    guild_id: str = Field(pattern=r"^[0-9]{1,20}$")
    text_channel_id: str = Field(pattern=r"^[0-9]{1,20}$")
    voice_channel_id: str = Field(pattern=r"^[0-9]{1,20}$")
    requester_discord_id: str = Field(pattern=r"^[0-9]{1,20}$")
    started_at: AwareDatetime
    participants: list[RecordParticipant] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def unique_participants(self):
        if len({p.discord_id for p in self.participants}) != len(self.participants):
            raise ValueError("duplicate participant")
        return self


class RecordResult(BaseModel):
    status: str
    doc_url: str | None = None
    admin_only: bool = False


class RecordHandoff(Protocol):
    async def submit(self, meeting_id: str, payload: dict) -> RecordResult: ...
    async def close(self) -> None: ...


class StopRequest(BaseModel):
    title: str | None = Field(default=None, max_length=100)
    record: RecordContext | None = None


class QnaRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)

    @field_validator("question")
    @classmethod
    def question_contains_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must contain text")
        return value.strip()


class QnaReference(BaseModel):
    speaker: str
    start_ms: int


class QnaResponse(BaseModel):
    answer: str
    references: list[QnaReference] = []
    window_start_ms: int
    window_end_ms: int
    shortened: bool = False
