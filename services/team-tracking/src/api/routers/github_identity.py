from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response

from contracts.github_identity import GitHubIdentityWrite, VerifiedGitHubIdentity
from contracts.storage import StorageAdapter
from src.api.auth import AuthedKey, get_actor, require_scope
from src.api.deps import get_storage

router = APIRouter(prefix="/people/{person_id}/github", tags=["github-identity"])


@router.get("", response_model=VerifiedGitHubIdentity)
def get_identity(
    person_id: UUID,
    storage: StorageAdapter = Depends(get_storage),
    _: AuthedKey = Depends(require_scope("github-identities:read")),
):
    result = storage.get_verified_github(person_id)
    if not result:
        raise HTTPException(404, "verified GitHub identity not found")
    return result


@router.put("", response_model=VerifiedGitHubIdentity)
def set_identity(
    person_id: UUID,
    body: GitHubIdentityWrite,
    storage: StorageAdapter = Depends(get_storage),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("github-identities:verify")),
):
    try:
        return storage.set_verified_github(
            person_id, body.discord_id, body.github_id, body.login, actor=actor
        )
    except ValueError as e:
        raise HTTPException(409, str(e)) from None


@router.delete("", status_code=204)
def unlink(
    person_id: UUID,
    storage: StorageAdapter = Depends(get_storage),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("github-identities:verify")),
):
    storage.unlink_verified_github(person_id, actor=actor)
    return Response(status_code=204)
