from contextlib import AbstractContextManager
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class GitHubOAuthError(Exception):
    """Safe static code only. Never wrap vendor responses or credential values."""


class GitHubLinkSubject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    person_id: UUID
    discord_id: str = Field(pattern=r"^[0-9]{1,30}$")


class GitHubOAuthProvider(Protocol):
    def authorize_url(self, state: str, challenge: str) -> str: ...
    def exchange(self, code: str, verifier: str) -> dict: ...
    def refresh(self, refresh_token: str) -> dict: ...
    def profile(self, token: str) -> dict: ...
    def revoke(self, token: str) -> None: ...


class GitHubDirectory(Protocol):
    def validate(self, person_id: str, discord_id: str) -> None: ...
    def verify(self, person_id: str, discord_id: str, github_id: int, login: str) -> None: ...
    def unlink(self, person_id: str) -> None: ...


class VaultRecords(Protocol):
    def get(self, key: str) -> dict | None: ...
    def set(self, key: str, data: dict) -> None: ...
    def delete(self, key: str) -> None: ...
    def keys(self, prefix: str) -> list[str]: ...


class GitHubCredentialVault(Protocol):
    def transaction(self) -> AbstractContextManager[VaultRecords]: ...


class GitHubLinkConfirm(GitHubLinkSubject):
    confirmation_code: SecretStr = Field(min_length=1, max_length=200)
