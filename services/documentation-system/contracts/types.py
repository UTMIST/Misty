from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


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
