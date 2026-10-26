"""Login state belongs to the gateway; code secrets belong to verification."""

import re
from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress, SecretStr, field_validator


class VerificationUnavailable(Exception):
    """Private verification failed; no upstream payload may escape."""


class VerificationClient(Protocol):
    def request_code(self, subject: str, email: str) -> None: ...
    def confirm_code(self, subject: str, code: str) -> str | None: ...
    def close(self) -> None: ...


class LoginChallenge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    owner: str
    email: str
    created_at: datetime
    expires_at: datetime
    person_id: UUID | None = None
    attempts: int = 0
    redeemed: bool = False


class EmailStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=254)
    # An attestation from the authenticated backend, never a public browser header.
    client_ip: IPvAnyAddress

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("invalid email")
        return value


class EmailConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: UUID
    code: SecretStr
    client_ip: IPvAnyAddress

    @field_validator("code")
    @classmethod
    def valid_code(cls, value: SecretStr) -> SecretStr:
        if not re.fullmatch(r"[0-9]{6}", value.get_secret_value()):
            raise ValueError("expected six digits")
        return value


class EmailStartOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: UUID
    expires_in: int = 600
    message: str = "If an active Misty account matches, a code will be sent."


class EmailConfirmOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    person_id: UUID
    email: str


class PersonStatusOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool
