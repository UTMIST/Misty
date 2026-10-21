"""Portable search API and query-embedding boundary; no service implementations."""

from math import isfinite
import struct
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=10, ge=1, le=50, strict=True)

    @field_validator("query")
    @classmethod
    def valid_query(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("query must be nonblank and contain no NUL characters")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("query must be valid UTF-8") from None
        return value.strip()


class QueryEmbedding(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, revalidate_instances="always"
    )

    model: str = Field(min_length=1)
    dimensions: Literal[1536] = 1536
    vector: tuple[float, ...] = Field(min_length=1536, max_length=1536, repr=False)

    @field_validator("vector", mode="before")
    @classmethod
    def valid_vector(cls, value):
        if not isinstance(value, (list, tuple)) or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) for v in value
        ):
            raise ValueError("vector must contain finite numbers")
        try:
            rounded = struct.unpack("1536f", struct.pack("1536f", *value))
        except (OverflowError, struct.error):
            raise ValueError("vector must fit 1536 finite float32 values") from None
        if not all(isfinite(v) for v in rounded) or not any(rounded):
            raise ValueError("vector must be finite and nonzero")
        return rounded


class EmbeddingError(Exception):
    """Safe public failure classification; never carries provider response text."""

    def __init__(self, status_code: int = 502) -> None:
        self.status_code = status_code
        super().__init__("query embedding unavailable")


class EmbeddingClient(Protocol):
    def embed_query(self, query: str) -> QueryEmbedding:
        """Embed one query, checking the configured resolved model and width.

        Raises EmbeddingError on dependency failure or malformed output.
        """
        ...


class RetrievalUnavailable(Exception):
    """Storage could not complete an authorized retrieval query."""


class ChunkCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ordinal: int
    start_offset: int
    end_offset: int
    source_content_hash: str


class SearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: UUID
    title: str | None
    url: str
    source_id: str
    chunk: ChunkCitation
    text: str = Field(repr=False)
    score: float


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[SearchHit]
