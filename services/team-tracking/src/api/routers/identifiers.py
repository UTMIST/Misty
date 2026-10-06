from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, field_validator

from contracts.groups import GroupProvider
from contracts.storage import StorageAdapter
from contracts.types import (
    Person,
    PersonIdentifier,
    PersonIdentifierCreate,
    PersonIdentifierUpdate,
)
from src.api.auth import AuthedKey, get_actor, require_scope
from src.api.deps import get_group_provider, get_storage
from src.google_groups import current_team_ids, schedule_sync

router = APIRouter(prefix="/people", tags=["identifiers"])


class AddEmailIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str

    @field_validator("email")
    @classmethod
    def _email_not_blank(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("email must not be blank")
        return v


# Declared before the /{person_id}/... routes (literal-first convention).
@router.get("/by-identifier/{provider}/{external_id}", response_model=Person)
def reverse_lookup(
    provider: str,
    external_id: str,
    storage: StorageAdapter = Depends(get_storage),
    _: AuthedKey = Depends(require_scope("identifiers:read")),
) -> Person:
    person = storage.get_person_by_identifier(provider, external_id)
    if person is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="identifier not found")
    return person


@router.get("/{person_id}/identifiers", response_model=list[PersonIdentifier])
def list_identifiers(
    person_id: UUID,
    storage: StorageAdapter = Depends(get_storage),
    _: AuthedKey = Depends(require_scope("identifiers:read")),
) -> list[PersonIdentifier]:
    if storage.get_person(person_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="person not found")
    return storage.list_person_identifiers(person_id)


@router.post(
    "/{person_id}/emails",
    response_model=PersonIdentifier,
    status_code=status.HTTP_201_CREATED,
)
def add_person_email(
    person_id: UUID,
    payload: AddEmailIn,
    background: BackgroundTasks,
    storage: StorageAdapter = Depends(get_storage),
    groups: GroupProvider | None = Depends(get_group_provider),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("identifiers:write")),
) -> PersonIdentifier:
    if storage.get_person(person_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="person not found")
    try:
        added = storage.add_person_email(person_id, payload.email, actor=actor)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    schedule_sync(background, storage, groups, current_team_ids(storage, person_id), actor=actor)
    return added


@router.post(
    "/{person_id}/identifiers",
    response_model=PersonIdentifier,
    status_code=status.HTTP_201_CREATED,
)
def link_identifier(
    person_id: UUID,
    payload: PersonIdentifierCreate,
    storage: StorageAdapter = Depends(get_storage),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("identifiers:write")),
) -> PersonIdentifier:
    if storage.get_person(person_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="person not found")
    if storage.get_provider(payload.provider) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown provider: {payload.provider}",
        )
    try:
        return storage.create_person_identifier(person_id, payload, actor=actor)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))


@router.patch("/{person_id}/identifiers/{provider}", response_model=PersonIdentifier)
def update_identifier(
    person_id: UUID,
    provider: str,
    payload: PersonIdentifierUpdate,
    storage: StorageAdapter = Depends(get_storage),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("identifiers:write")),
) -> PersonIdentifier:
    try:
        updated = storage.update_person_identifier(person_id, provider, payload, actor=actor)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="identifier not found")
    return updated


@router.delete("/{person_id}/identifiers/{provider}", status_code=status.HTTP_204_NO_CONTENT)
def delete_identifier(
    person_id: UUID,
    provider: str,
    storage: StorageAdapter = Depends(get_storage),
    _: AuthedKey = Depends(require_scope("identifiers:write")),
) -> Response:
    try:
        removed = storage.delete_person_identifier(person_id, provider)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="identifier not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
