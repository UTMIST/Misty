from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GitHubTeamMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    team_id: UUID
    organization_id: int = Field(gt=0)
    github_team_id: int | None = Field(default=None, gt=0)
    slug: str | None = None
    name: str | None = None
    status: Literal["pending", "retryable", "active", "inactive"] = "pending"
    version: int = 1
    created_at: datetime
    updated_at: datetime
    created_by: str
    updated_by: str


class GitHubMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["adopt", "provision"]
    github_team_id: int | None = Field(default=None, gt=0)
    # Explicit owner review: existing users/maintainers are preserved, no member writes.
    adoption_reviewed: bool = False

    @model_validator(mode="after")
    def validate_mode(self):
        if self.mode == "adopt" and (not self.github_team_id or not self.adoption_reviewed):
            raise ValueError("adoption requires a team ID and explicit adoption review")
        if self.mode == "provision" and self.github_team_id:
            raise ValueError("provision must not supply an existing team ID")
        return self


class GitHubProviderError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class GitHubTeamsClient(Protocol):
    organization_id: int

    def inspect(self, team_id: int) -> dict: ...
    def provision(self, team_id: UUID, *, reconcile_only: bool) -> dict: ...
