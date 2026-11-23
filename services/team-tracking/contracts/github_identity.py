from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class VerifiedGitHubIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    person_id: UUID
    github_id: int | None = Field(default=None, gt=0)
    login: str | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9-]+$"
    )
    verified_at: datetime
    updated_by: str


class GitHubIdentityWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discord_id: str = Field(pattern=r"^[0-9]{1,30}$")
    github_id: int = Field(gt=0)
    login: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9-]+$")
