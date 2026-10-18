import json
import re
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.responses import Response

from contracts.directory import DirectoryClient, DirectoryUnavailable
from contracts.retrieval import (
    EmbeddingClient,
    EmbeddingError,
    RetrievalUnavailable,
    SearchRequest,
    SearchResponse,
)
from contracts.storage import StorageAdapter
from contracts.visibility import Actor, Channel
from platform_auth import AuthedKey
from src.api.auth import get_on_behalf_actor, require_api_key
from src.api.deps import get_directory, get_embeddings, get_storage
from src.config import get_settings


class _SearchRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def validated(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # FastAPI's default error response echoes invalid Unicode input
                # and fails to encode it. Keep the detail shape without input.
                errors = [{k: error[k] for k in ("type", "loc", "msg")} for error in exc.errors()]
                return Response(
                    json.dumps({"detail": errors}), status_code=422, media_type="application/json"
                )

        return validated


router = APIRouter(tags=["search"], route_class=_SearchRoute)


@router.post("/search", response_model=SearchResponse)
def search(
    payload: SearchRequest,
    person_id: UUID | None = Depends(get_on_behalf_actor),
    guild_id: str | None = Header(default=None, alias="X-Guild-ID"),
    channel_id: str | None = Header(default=None, alias="X-Channel-ID"),
    key: AuthedKey = Depends(require_api_key),
    storage: StorageAdapter = Depends(get_storage),
    directory: DirectoryClient = Depends(get_directory),
    embeddings: EmbeddingClient = Depends(get_embeddings),
) -> SearchResponse:
    if not (key.has_scope("docs:read") or key.has_scope("docs:read:all")):
        raise HTTPException(403, "missing scope: docs:read")
    is_channel = guild_id is not None or channel_id is not None
    if (person_id is not None) == is_channel or (
        is_channel and (guild_id is None or channel_id is None)
    ):
        raise HTTPException(400, "provide exactly one person or channel context")
    if is_channel:
        if "act-as-channel" not in key.scopes:
            raise HTTPException(403, "missing scope: act-as-channel")
        if not all(re.fullmatch(r"[0-9]{1,20}", value) for value in (guild_id, channel_id)):
            raise HTTPException(400, "channel IDs must be decimal snowflakes")
    # Resolve scope only after body validation and scope/context checks. Unlike
    # direct reads, retrieval never returns a partially resolved person scope.
    try:
        if is_channel:
            visibility = Channel(directory.get_channel_source_team_ids(guild_id, channel_id))
        else:
            visibility = Actor(
                person_id,
                directory.get_active_team_ids(person_id),
                directory.get_source_access_team_ids(person_id),
            )
    except DirectoryUnavailable:
        raise HTTPException(503, "retrieval scope unavailable") from None
    if isinstance(visibility, Channel) and not visibility.source_team_ids:
        return SearchResponse(results=[])
    try:
        embedding = embeddings.embed_query(payload.query)
    except EmbeddingError as exc:
        raise HTTPException(exc.status_code, "query embedding unavailable") from None
    try:
        results = storage.search_chunks(
            query=payload.query,
            embedding=embedding,
            visibility=visibility,
            k=payload.k,
            min_cosine=get_settings().search_min_cosine,
        )
    except RetrievalUnavailable:
        raise HTTPException(503, "retrieval storage unavailable") from None
    return SearchResponse(results=results)
