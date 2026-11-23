from fastapi import APIRouter, Depends, HTTPException, Path

from contracts.github_teams import GitHubTeam, GitHubTeamError, GitHubTeams, ProvisionTeam
from platform_auth import AuthedKey
from src.api.auth import require_scope
from src.api.deps import get_github_teams

router = APIRouter(prefix="/github/teams", tags=["github-teams"])


def _call(fn):
    try:
        return fn()
    except GitHubTeamError as e:
        code = 503 if e.code == "not_configured" else 502
        if e.code == "not_found":
            code = 404
        if e.code in (
            "organization_mismatch",
            "nested_team_unsupported",
            "idp_managed_team_unsupported",
            "name_conflict",
        ):
            code = 409
        raise HTTPException(code, detail={"code": e.code, "uncertain": e.uncertain}) from None


@router.get("/{team_id}", response_model=GitHubTeam)
def inspect(
    team_id: int = Path(gt=0),
    provider: GitHubTeams = Depends(get_github_teams),
    _: AuthedKey = Depends(require_scope("github-teams:read")),
):
    return _call(lambda: provider.inspect(team_id))


@router.post("", response_model=GitHubTeam)
def provision(
    body: ProvisionTeam,
    provider: GitHubTeams = Depends(get_github_teams),
    _: AuthedKey = Depends(require_scope("github-teams:manage")),
):
    return _call(lambda: provider.provision(body.misty_team_id, reconcile_only=body.reconcile_only))
