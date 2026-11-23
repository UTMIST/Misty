from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from contracts.github import (
    GitHubMappingRequest,
    GitHubProviderError,
    GitHubTeamMapping,
    GitHubTeamsClient,
)
from contracts.storage import StorageAdapter
from src.api.auth import AuthedKey, get_actor, require_scope
from src.api.deps import get_github_teams, get_storage

router = APIRouter(prefix="/teams/{team_id}/github-team", tags=["github-teams"])


def _team(storage, team_id):
    team = storage.get_team(team_id)
    if not team:
        raise HTTPException(404, "team not found")
    if not team.active:
        raise HTTPException(409, "team is retired")


def _finish(storage, provider, team_id, data, actor):
    if data["organization_id"] != provider.organization_id:
        raise HTTPException(409, "organization mismatch")
    try:
        return storage.finish_github_mapping(
            team_id,
            data["organization_id"],
            data["team_id"],
            data["slug"],
            data["name"],
            actor=actor,
        )
    except ValueError as e:
        raise HTTPException(409, str(e)) from None


def _provider_call(fn, *, on_retryable=None):
    try:
        return fn()
    except GitHubProviderError as e:
        if e.retryable and on_retryable is not None:
            on_retryable()
        if e.code in (
            "organization_mismatch",
            "nested_team_unsupported",
            "idp_managed_team_unsupported",
            "name_conflict",
        ):
            raise HTTPException(409, e.code) from None
        raise HTTPException(
            503 if e.code == "not_configured" else 502,
            "GitHub operation unavailable; inspect pending state before retrying",
        ) from None


@router.get("", response_model=GitHubTeamMapping)
def get_mapping(
    team_id: UUID,
    storage: StorageAdapter = Depends(get_storage),
    _: AuthedKey = Depends(require_scope("github-teams:read")),
    provider: GitHubTeamsClient = Depends(get_github_teams),
    actor: str = Depends(get_actor),
):
    item = storage.get_github_mapping(team_id)
    if not item:
        raise HTTPException(404, "mapping not found")
    if item.status == "active":
        try:
            data = provider.inspect(item.github_team_id)
        except GitHubProviderError as e:
            if e.code == "not_found":
                return storage.deactivate_github_mapping(team_id, actor=actor)
            raise HTTPException(502, "GitHub team status unavailable") from None
        if (
            data["organization_id"] != item.organization_id
            or data["team_id"] != item.github_team_id
        ):
            raise HTTPException(409, "GitHub identity mismatch")
        if data["slug"] != item.slug or data["name"] != item.name:
            return _finish(storage, provider, team_id, data, actor)
    return item


@router.post("", response_model=GitHubTeamMapping)
def create_mapping(
    team_id: UUID,
    body: GitHubMappingRequest,
    storage: StorageAdapter = Depends(get_storage),
    provider: GitHubTeamsClient = Depends(get_github_teams),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("github-teams:manage")),
):
    _team(storage, team_id)
    if provider.organization_id <= 0:
        raise HTTPException(503, "GitHub team management not configured")
    # Reservation prevents concurrent create calls and survives lost responses/restarts.
    try:
        reservation = storage.reserve_github_mapping(
            team_id, provider.organization_id, actor=actor, github_team_id=body.github_team_id
        )
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    data = _provider_call(
        lambda: (
            provider.inspect(body.github_team_id)
            if body.mode == "adopt"
            else provider.provision(team_id, reconcile_only=False)
        ),
        on_retryable=(
            lambda: storage.mark_github_mapping_retryable(team_id, reservation.version, actor=actor)
        )
        if body.mode == "provision"
        else None,
    )
    return _finish(storage, provider, team_id, data, actor)


@router.post("/reconcile", response_model=GitHubTeamMapping)
def reconcile(
    team_id: UUID,
    storage: StorageAdapter = Depends(get_storage),
    provider: GitHubTeamsClient = Depends(get_github_teams),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("github-teams:manage")),
):
    _team(storage, team_id)
    item = storage.get_github_mapping(team_id)
    if not item or item.status not in ("pending", "active"):
        raise HTTPException(409, "no live mapping")
    try:
        data = (
            provider.inspect(item.github_team_id)
            if item.github_team_id
            else provider.provision(team_id, reconcile_only=True)
        )
    except GitHubProviderError as e:
        if e.code == "not_found" and item.github_team_id:
            storage.deactivate_github_mapping(team_id, actor=actor)
        raise HTTPException(502, "GitHub reconciliation failed; mapping not authorized") from None
    return _finish(storage, provider, team_id, data, actor)


@router.delete("", response_model=GitHubTeamMapping)
def deactivate(
    team_id: UUID,
    storage: StorageAdapter = Depends(get_storage),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("github-teams:manage")),
):
    item = storage.deactivate_github_mapping(team_id, actor=actor)
    if not item:
        raise HTTPException(404, "mapping not found")
    return item
