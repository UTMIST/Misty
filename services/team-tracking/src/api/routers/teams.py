from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from contracts.groups import GroupProvider
from contracts.storage import StorageAdapter
from contracts.types import Team, TeamCreate, TeamGoogleGroup, TeamUpdate
from src.api.auth import AuthedKey, get_actor, require_scope
from src.api.deps import get_group_provider, get_storage
from src.google_groups import schedule_sync, sync_team
from src.storage.errors import UnknownParentTeamError

router = APIRouter(prefix="/teams", tags=["teams"])


@router.post("", response_model=Team, status_code=status.HTTP_201_CREATED)
def create_team(
    payload: TeamCreate,
    background: BackgroundTasks,
    storage: StorageAdapter = Depends(get_storage),
    groups: GroupProvider | None = Depends(get_group_provider),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("teams:write")),
) -> Team:
    try:
        team = storage.create_team(payload, actor=actor)
    except UnknownParentTeamError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    schedule_sync(background, storage, groups, {team.id}, actor=actor)
    return team


@router.get("", response_model=list[Team])
def list_teams(
    active_only: bool = False,
    storage: StorageAdapter = Depends(get_storage),
    _: AuthedKey = Depends(require_scope("teams:read")),
) -> list[Team]:
    return storage.list_teams(active_only=active_only)


@router.get("/by-slug/{slug}", response_model=Team)
def get_team_by_slug(
    slug: str,
    storage: StorageAdapter = Depends(get_storage),
    _: AuthedKey = Depends(require_scope("teams:read")),
) -> Team:
    team = storage.get_team_by_slug(slug)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="team not found")
    return team


@router.get("/google-groups", response_model=list[TeamGoogleGroup])
def list_team_google_groups(
    storage: StorageAdapter = Depends(get_storage),
    _: AuthedKey = Depends(require_scope("teams:read")),
) -> list[TeamGoogleGroup]:
    return storage.list_team_google_groups()


@router.get("/{team_id}", response_model=Team)
def get_team(
    team_id: UUID,
    storage: StorageAdapter = Depends(get_storage),
    _: AuthedKey = Depends(require_scope("teams:read")),
) -> Team:
    team = storage.get_team(team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="team not found")
    return team


@router.patch("/{team_id}", response_model=Team)
def update_team(
    team_id: UUID,
    payload: TeamUpdate,
    background: BackgroundTasks,
    storage: StorageAdapter = Depends(get_storage),
    groups: GroupProvider | None = Depends(get_group_provider),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("teams:write")),
) -> Team:
    try:
        updated = storage.update_team(team_id, payload, actor=actor)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="team not found")
    schedule_sync(background, storage, groups, {team_id}, actor=actor)
    return updated


@router.get("/{team_id}/google-group", response_model=TeamGoogleGroup)
def get_team_google_group(
    team_id: UUID,
    storage: StorageAdapter = Depends(get_storage),
    _: AuthedKey = Depends(require_scope("teams:read")),
) -> TeamGoogleGroup:
    state = storage.get_team_google_group(team_id)
    if state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="google group not found")
    return state


@router.post("/{team_id}/google-group/sync", response_model=TeamGoogleGroup)
def sync_team_google_group(
    team_id: UUID,
    storage: StorageAdapter = Depends(get_storage),
    groups: GroupProvider | None = Depends(get_group_provider),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("teams:write")),
) -> TeamGoogleGroup:
    if storage.get_team(team_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="team not found")
    if groups is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="google groups not configured",
        )
    state = sync_team(storage, groups, team_id, actor=actor)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="retired team has no google group"
        )
    return state
