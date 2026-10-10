import hashlib
import re
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, computed_field, field_validator


AccessLevel = Literal["member", "admin", "superuser"]


def _normalize_email(v: str) -> str:
    """Canonical email normalization: strip whitespace and lowercase."""
    return v.strip().lower()


class DirectoryBase(BaseModel):
    """Common audit fields on every directory record.

    Subclasses declare their own `id` with the appropriate concrete type
    (UUID for people/teams/memberships, str slug for role_kinds).
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    created_at: datetime
    updated_at: datetime
    created_by: str
    updated_by: str


class Person(DirectoryBase):
    id: UUID
    display_name: str
    primary_email: str
    active: bool = True
    access_level: AccessLevel = "member"

    @field_validator("primary_email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return _normalize_email(v)


class Team(DirectoryBase):
    id: UUID
    slug: str
    label: str
    description: str | None = None
    parent_id: UUID | None = None
    active: bool = True


class RoleKind(DirectoryBase):
    id: str  # slug PK, e.g. "executive", "director", "lead", "member"
    label: str
    description: str | None = None
    active: bool = True


class TeamMembership(DirectoryBase):
    id: UUID
    person_id: UUID
    team_id: UUID
    role_kind_id: str
    is_team_admin: bool = False
    started_at: date
    ended_at: date | None = None


class Provider(DirectoryBase):
    id: str  # slug PK, e.g. "discord", "github", "notion", "uoft_email"
    label: str
    description: str | None = None
    active: bool = True


class PersonIdentifier(DirectoryBase):
    id: UUID
    person_id: UUID
    provider: str
    external_id: str
    handle: str | None = None


# --- Input DTOs (for API create/update payloads) ---


class PersonCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str
    primary_email: str
    access_level: AccessLevel = "member"

    @field_validator("primary_email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return _normalize_email(v)


class PersonUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = None
    primary_email: str | None = None
    active: bool | None = None
    access_level: AccessLevel | None = None

    @field_validator("primary_email")
    @classmethod
    def normalize_email(cls, v: str | None) -> str | None:
        return _normalize_email(v) if v is not None else None


class TeamCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str
    label: str
    description: str | None = None
    parent_id: UUID | None = None

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9_.]+", v):
            raise ValueError("slug must match [a-z0-9_.]+")
        return v


class TeamUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str | None = None
    label: str | None = None
    description: str | None = None
    parent_id: UUID | None = None
    active: bool | None = None

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"[a-z0-9_.]+", v):
            raise ValueError("slug must match [a-z0-9_.]+")
        return v


class TeamMembershipCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    person_id: UUID
    team_id: UUID
    role_kind_id: str = "member"
    is_team_admin: bool = False
    started_at: date | None = None  # defaults to today at storage layer
    ended_at: date | None = None


class TeamMembershipUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role_kind_id: str | None = None
    is_team_admin: bool | None = None
    ended_at: date | None = None


class PersonIdentifierCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    external_id: str
    handle: str | None = None


class PersonIdentifierUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_id: str | None = None
    handle: str | None = None


class ChannelTeams(BaseModel):
    """Teams whose documents may inform answers in a Discord channel.

    An unconfigured channel has `team_ids == []` and no document access.
    """

    model_config = ConfigDict(extra="forbid")
    guild_id: str
    channel_id: str
    team_ids: list[UUID]  # active teams only, sorted
    updated_by: str | None = None

    @computed_field
    @property
    def version(self) -> str:
        """Fingerprint of the effective access. Derived from content, not
        timestamps, so it changes exactly when `team_ids` does regardless of the
        order concurrent writes commit in."""
        return hashlib.sha256(",".join(sorted(map(str, self.team_ids))).encode()).hexdigest()[:16]


class ChannelTeamsReplace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    team_ids: list[UUID]

    @field_validator("team_ids")
    @classmethod
    def no_duplicates(cls, v: list[UUID]) -> list[UUID]:
        if len(set(v)) != len(v):
            raise ValueError("team_ids must not contain duplicates")
        return v


class ApiKey(DirectoryBase):
    """A named, scoped API key. `key_hash` is never returned by public APIs
    (only exists on writes back to storage); consumers get to see the plaintext
    key ONCE at issuance time.
    """

    id: UUID
    name: str
    prefix: str
    scopes: list[str]
    active: bool = True
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None


class ApiKeyCreate(BaseModel):
    """Input DTO for issuing a new API key. The plaintext key is not part of
    this DTO — the storage layer generates it and returns it in an issuance
    result the caller must handle carefully (log to stdout ONCE, never persist)."""

    model_config = ConfigDict(extra="forbid")
    name: str
    scopes: list[str] = []


class IssuedApiKey(BaseModel):
    """Result of issuing a key: the plaintext key (shown ONCE) plus the stored ApiKey row."""

    model_config = ConfigDict(extra="forbid")
    plaintext: str
    api_key: ApiKey
