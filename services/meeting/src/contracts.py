from pydantic import BaseModel, Field, field_validator


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


class StopRequest(BaseModel):
    title: str | None = Field(default=None, max_length=100)


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
