"""Docs-local authorization: map the request (asserted actor + key scopes) to a
visibility ActorContext, and guard write routes against invisible docs.

The #73 parser (`get_on_behalf_actor`) runs FIRST and its 400/403 propagate —
a bad or unauthorized X-On-Behalf-Of never falls through to SEE_ALL."""

from uuid import UUID

from fastapi import Depends, HTTPException, status

from contracts.directory import DirectoryClient, DirectoryUnavailable
from contracts.storage import StorageAdapter
from contracts.types import Doc
from contracts.visibility import Actor, ActorContext, DENY, SEE_ALL
from platform_auth import AuthedKey
from src.api.auth import get_on_behalf_actor, require_api_key
from src.api.deps import get_directory

DOCS_READ = "docs:read"
DOCS_READ_ALL = "docs:read:all"


def _actor(actor_id: UUID, directory: DirectoryClient) -> Actor:
    try:
        team_ids = directory.get_active_team_ids(actor_id)
    except DirectoryUnavailable:
        team_ids = frozenset()  # partial fail-closed: team-granted docs withheld
    return Actor(person_id=actor_id, team_ids=team_ids)


def read_context(
    actor_id: UUID | None = Depends(get_on_behalf_actor),
    key: AuthedKey = Depends(require_api_key),
    directory: DirectoryClient = Depends(get_directory),
) -> ActorContext:
    if actor_id is not None:
        if not (key.has_scope(DOCS_READ) or key.has_scope(DOCS_READ_ALL)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing scope: {DOCS_READ}",
            )
        return _actor(actor_id, directory)
    if key.has_scope(DOCS_READ_ALL):  # admin wildcard satisfies via has_scope
        return SEE_ALL
    if key.has_scope(DOCS_READ):
        return DENY
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"missing scope: {DOCS_READ}")


def write_context(
    actor_id: UUID | None = Depends(get_on_behalf_actor),
    directory: DirectoryClient = Depends(get_directory),
) -> ActorContext:
    if actor_id is None:
        return SEE_ALL  # a docs:write key is itself the trust boundary
    return _actor(actor_id, directory)


def get_visible_doc_or_404(doc_id: UUID, ctx: ActorContext, storage: StorageAdapter) -> Doc:
    doc = storage.get_doc(doc_id, visibility=ctx)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="doc not found")
    return doc
