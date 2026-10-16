from datetime import date
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from contracts.groups import GroupProvider
from contracts.storage import StorageAdapter
from contracts.types import TeamMembership, TeamMembershipCreate, TeamMembershipUpdate
from src.api.auth import AuthedKey, get_actor, require_scope
from src.api.deps import get_group_provider, get_storage
from src.google_groups import schedule_sync

router = APIRouter(prefix="/memberships", tags=["memberships"])


class EndMembershipPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ended_at: date


@router.post("", response_model=TeamMembership, status_code=status.HTTP_201_CREATED)
def create_membership(
    payload: TeamMembershipCreate,
    background: BackgroundTasks,
    storage: StorageAdapter = Depends(get_storage),
    groups: GroupProvider | None = Depends(get_group_provider),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("memberships:write")),
) -> TeamMembership:
    try:
        created = storage.create_membership(payload, actor=actor)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    schedule_sync(background, storage, groups, {created.team_id}, actor=actor)
    return created


@router.get("", response_model=list[TeamMembership])
def list_memberships(
    team_id: UUID | None = None,
    person_id: UUID | None = None,
    active_only: bool = False,
    as_of: date | None = None,
    is_team_admin: bool | None = None,
    storage: StorageAdapter = Depends(get_storage),
    _: AuthedKey = Depends(require_scope("memberships:read")),
) -> list[TeamMembership]:
    return storage.list_memberships(
        team_id=team_id,
        person_id=person_id,
        active_only=active_only,
        as_of=as_of,
        is_team_admin=is_team_admin,
    )


@router.get("/{membership_id}", response_model=TeamMembership)
def get_membership(
    membership_id: UUID,
    storage: StorageAdapter = Depends(get_storage),
    _: AuthedKey = Depends(require_scope("memberships:read")),
) -> TeamMembership:
    m = storage.get_membership(membership_id)
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="membership not found")
    return m


@router.patch("/{membership_id}", response_model=TeamMembership)
def update_membership(
    membership_id: UUID,
    payload: TeamMembershipUpdate,
    background: BackgroundTasks,
    storage: StorageAdapter = Depends(get_storage),
    groups: GroupProvider | None = Depends(get_group_provider),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("memberships:write")),
) -> TeamMembership:
    try:
        updated = storage.update_membership(membership_id, payload, actor=actor)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="membership not found")
    schedule_sync(background, storage, groups, {updated.team_id}, actor=actor)
    return updated


@router.post("/{membership_id}/end", response_model=TeamMembership)
def end_membership(
    membership_id: UUID,
    payload: EndMembershipPayload,
    background: BackgroundTasks,
    storage: StorageAdapter = Depends(get_storage),
    groups: GroupProvider | None = Depends(get_group_provider),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("memberships:write")),
) -> TeamMembership:
    try:
        ended = storage.end_membership(membership_id, payload.ended_at, actor=actor)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if ended is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="membership not found")
    schedule_sync(background, storage, groups, {ended.team_id}, actor=actor)
    return ended
