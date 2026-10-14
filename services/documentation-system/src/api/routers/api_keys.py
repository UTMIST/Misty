"""Router for API-key introspection.

Currently exposes exactly one endpoint: GET /api-keys/self.
Callers use it to learn what scopes their OWN key has, so a tool can refuse
up front instead of misreading an authorization result as data. The
`doc-backfill` CLI is the first consumer: a key without `docs:read:all` gets
`DENY` from `read_context`, which lists as `200 []` — indistinguishable from
an empty catalog (see src/api/authz.py).

Mirrors team-tracking's `GET /api-keys/self`.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.api.auth import AuthedKey, require_api_key

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


class SelfKey(BaseModel):
    """Response body for GET /api-keys/self."""

    name: str
    scopes: list[str]


@router.get("/self", response_model=SelfKey)
def get_self_key(key: AuthedKey = Depends(require_api_key)) -> SelfKey:
    """Return the calling key's own name and scopes.

    No scope beyond a valid API key is required — a key may always read its
    own grant. It never reveals another key's scopes, and nothing secret: the
    caller already holds the key this describes.
    """
    return SelfKey(name=key.name, scopes=sorted(key.scopes))
