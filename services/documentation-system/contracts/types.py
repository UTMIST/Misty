from datetime import datetime
from math import isfinite
import struct
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


GranteeType = Literal["person", "team", "org"]


def _validate_grantee_shape(grantee_type: str, grantee_id) -> None:
    if grantee_type == "org" and grantee_id is not None:
        raise ValueError("org grants must not carry a grantee_id")
    if grantee_type in ("person", "team") and grantee_id is None:
        raise ValueError(f"{grantee_type} grants require a grantee_id")


class DirectoryBase(BaseModel):
    """Common audit fields on every stored record."""

    model_config = ConfigDict(frozen=False, extra="forbid")

    created_at: datetime
    updated_at: datetime
    created_by: str
    updated_by: str


class Source(DirectoryBase):
    id: str  # slug PK, e.g. "web", "gdocs"
    label: str
    url_patterns: list[str]
    requires_auth: bool
    has_api: bool
    content_fetch_enabled: bool
    active: bool = True


class Doc(DirectoryBase):
    id: UUID
    url: str
    url_normalized: str
    title: str | None = None
    source_id: str
    description: str | None = None
    owning_team_id: UUID | None = None
    owning_team_label: str | None = None
    owning_person_id: UUID | None = None
    owning_person_label: str | None = None
    content_snapshot: str | None = None
    fetched_at: datetime | None = None
    active: bool = True
    tags: list[str] = []
    grants: list["DocGrant"] = []


# --- Input DTOs ---


class DocGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grantee_type: GranteeType
    grantee_id: UUID | None = None
    grantee_label: str | None = None
    created_at: datetime
    created_by: str


class DocGrantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grantee_type: GranteeType
    grantee_id: UUID | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> "DocGrantInput":
        _validate_grantee_shape(self.grantee_type, self.grantee_id)
        return self


class DocIngest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    source_id: str | None = None
    title: str | None = None
    description: str | None = None
    owning_team_id: UUID | None = None
    owning_person_id: UUID | None = None
    tags: list[str] = []
    grants: list[DocGrantInput] = []

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, v: list[str]) -> list[str]:
        return [t.strip().lower() for t in v]


class DocUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    description: str | None = None
    owning_team_id: UUID | None = None
    owning_person_id: UUID | None = None
    active: bool | None = None


# --- Results ---


class IngestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc: Doc
    created: bool
    warnings: list[str] = []


class DocContentMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_hash: str
    fetched_at: datetime | None = None


class DocChunk(BaseModel):
    """An embedded slice of a document, identified within its replacement set.

    Offsets are half-open Unicode code-point indices into the source text whose
    SHA-256 is source_content_hash. The caller supplies the resolved /embed model,
    not its configuration alias. Vectors use pgvector's float32 precision in both
    adapters. A new vector width requires a schema migration.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, revalidate_instances="always", allow_inf_nan=False
    )

    ordinal: int = Field(ge=0, le=2**31 - 1, strict=True)
    chunk_text: str = Field(min_length=1, repr=False)
    start_offset: int = Field(ge=0, le=2**31 - 1, strict=True)
    end_offset: int = Field(gt=0, le=2**31 - 1, strict=True)
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding: list[float] = Field(min_length=1536, max_length=1536, repr=False)
    embedding_model: str = Field(min_length=1)
    dimensions: Literal[1536]

    @field_validator("chunk_text", "embedding_model")
    @classmethod
    def _valid_text(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("text must be nonblank and contain no NUL characters")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("text must be valid UTF-8") from None
        return value

    @field_validator("embedding")
    @classmethod
    def _float32(cls, value: list[float]) -> list[float]:
        try:
            rounded = list(struct.unpack("1536f", struct.pack("1536f", *value)))
        except (OverflowError, struct.error):
            raise ValueError("embedding values must fit finite float32") from None
        if not all(isfinite(element) for element in rounded):
            raise ValueError("embedding values must fit finite float32")
        return rounded

    @model_validator(mode="after")
    def _offsets_match_text(self) -> "DocChunk":
        if self.end_offset - self.start_offset != len(self.chunk_text):
            raise ValueError("offset span must equal the chunk text's Unicode code-point length")
        return self


# --- API keys (Level 2 security) ---


class ApiKey(DirectoryBase):
    id: UUID
    name: str
    prefix: str
    scopes: list[str]
    active: bool = True
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    scopes: list[str] = []


class IssuedApiKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plaintext: str
    api_key: ApiKey
