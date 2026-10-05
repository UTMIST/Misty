from pydantic import BaseModel, Field


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
