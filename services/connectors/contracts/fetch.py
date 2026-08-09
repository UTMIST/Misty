from pydantic import BaseModel, Field


class FetchRequest(BaseModel):
    url: str = Field(min_length=1)
    # Supplied by the caller rather than re-derived here: documentation-system
    # already resolves it during ingest, and deriving it independently in two
    # services is how the two drift apart.
    source_id: str = Field(min_length=1)


class FetchResponse(BaseModel):
    title: str | None = None
    content: str | None = None
    warnings: list[str] = []
