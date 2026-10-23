from datetime import datetime
from math import isfinite
import struct
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


GranteeType = Literal["person", "team", "org"]

# Well above the ~500-token target, but bounded for generated keyword metadata.
MAX_CHUNK_CHARS = 16_384


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
    purged_at: datetime | None = None
    source_access_synced_at: datetime | None = None
    source_access_attempted_at: datetime | None = None
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
    origin: Literal["manual", "google_drive"] = "manual"
    source_permission_id: str | None = None
    source_principal: str | None = None
    source_role: str | None = None
    source_inherited: bool = False
    source_inherited_from: list[str] = []
    source_expires_at: datetime | None = None
    expires_at: datetime | None = None


class SourceGrant(BaseModel):
    """Resolved source permission ready for atomic storage reconciliation."""

    model_config = ConfigDict(extra="forbid")
    grantee_type: Literal["person", "team"]
    grantee_id: UUID
    source_permission_id: str
    source_principal: str
    source_role: str
    source_inherited: bool = False
    source_inherited_from: list[str] = []
    source_expires_at: datetime | None = None


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


class DocIndexState(BaseModel):
    """Provenance of a doc's current chunk set (one body/title hash and model)."""

    model_config = ConfigDict(extra="forbid")
    source_content_hash: str
    source_title_hash: str | None = None
    embedding_model: str
    chunk_count: int


class IndexModelCoverage(BaseModel):
    """How much of the catalog one embedding model's chunk sets cover."""

    model_config = ConfigDict(extra="forbid")
    model: str
    # Docs with a set for this model, its chunks, and their total chunk_text
    # length in characters (the input size behind a re-embed estimate).
    docs: int
    chunks: int
    chunk_chars: int
    # Active docs with content indexed under some other model but not this
    # one. Search under this model cannot return them (#209).
    missing_docs: int


class IndexCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Active docs with stored content: everything indexing should cover.
    indexable_docs: int
    # Every stored model plus each requested one, ordered by model id.
    models: list[IndexModelCoverage]


class DocChunk(BaseModel):
    """An embedded slice of a document, identified within its replacement set.

    Offsets are half-open Unicode code-point indices into the source text whose
    SHA-256 is source_content_hash. The caller supplies the resolved /embed model,
    not its configuration alias. Vectors use pgvector's float32 precision in both
    adapters, stored as an immutable tuple. Text is capped at MAX_CHUNK_CHARS
    Unicode code points to bound generated keyword metadata. A new vector width
    requires a schema migration.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, revalidate_instances="always", allow_inf_nan=False
    )

    ordinal: int = Field(ge=0, le=2**31 - 1, strict=True)
    chunk_text: str = Field(min_length=1, max_length=MAX_CHUNK_CHARS, repr=False)
    start_offset: int = Field(ge=0, le=2**31 - 1, strict=True)
    end_offset: int = Field(gt=0, le=2**31 - 1, strict=True)
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Separate from the raw source hash used by citations. NULL means a legacy
    # set whose title context is unknown and must be re-indexed once.
    source_title_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    embedding: tuple[float, ...] = Field(min_length=1536, max_length=1536, repr=False)
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
    def _float32(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        try:
            rounded = struct.unpack("1536f", struct.pack("1536f", *value))
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
