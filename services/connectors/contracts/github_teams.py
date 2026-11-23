from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class GitHubTeam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_id: int = Field(gt=0, strict=True)
    team_id: int = Field(gt=0, strict=True)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9-]+$")
    name: str = Field(min_length=1, max_length=100)
    parent_id: int | None = None
    managed_marker: str | None = None


class ProvisionTeam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    misty_team_id: UUID
    # Reconciliation is read-only after any uncertain creation. Never blind retry.
    reconcile_only: bool = False


class GitHubTeamError(Exception):
    def __init__(self, code: str, *, uncertain: bool = False):
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


class GitHubTeams(Protocol):
    def inspect(self, team_id: int) -> GitHubTeam: ...
    def provision(self, team_id: UUID, *, reconcile_only: bool) -> GitHubTeam: ...
