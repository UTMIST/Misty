import json
import logging
import re
import time
from functools import wraps
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException
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
from platform_auth.telemetry import span
from src.api.auth import get_on_behalf_actor, require_api_key
from src.api.deps import get_directory, get_embeddings, get_storage
from src.config import get_settings


def _observe(request: Request, outcome: str) -> None:
    request.state.search_outcome = outcome
    if call := getattr(request.state, "search_call", None):
        call.outcome(outcome)
    if get_settings().search_outcome_audit_enabled:
        request.state.audit_extra = {
            **getattr(request.state, "audit_extra", {}),
            "search_outcome": outcome,
        }


def _early_outcome(request, outcome):
    if getattr(request.state, "search_outcome", None):
        return
    with span("catalog.search", service="documentation-system") as call:
        call.outcome(outcome)
        _observe(request, outcome)


def _trace_search(handler):
    @wraps(handler)
    def traced(*args, **kwargs):
        request = kwargs["request"]
        # The framework has authenticated the principal before accepting the
        # incoming trace from another service.
        with span("catalog.search", service="documentation-system") as call:
            request.state.search_call = call
            try:
                return handler(*args, **kwargs)
            except StarletteHTTPException as exc:
                if not getattr(request.state, "search_outcome", None):
                    _observe(request, "denied" if exc.status_code == 403 else "invalid_request")
                raise
            except Exception:
                _observe(request, "internal_error")
                raise

    return traced


class _SearchRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def validated(request):
            try:
                return await handler(request)
            except StarletteHTTPException as exc:
                # FastAPI also uses Starlette's base exception for unreadable bodies.
                fallback = {400: "invalid_request", 401: "denied", 403: "denied"}.get(
                    exc.status_code
                )
                if fallback:
                    _early_outcome(request, fallback)
                raise
            except RequestValidationError as exc:
                _early_outcome(request, "invalid_request")
                # FastAPI's default error response echoes invalid Unicode input
                # and fails to encode it. Keep the detail shape without input.
                errors = [{k: error[k] for k in ("type", "loc", "msg")} for error in exc.errors()]
                return Response(
                    json.dumps({"detail": errors}), status_code=422, media_type="application/json"
                )
            except Exception as exc:
                _early_outcome(request, "internal_error")
                # Save the call sites. Raise outside the handler so framework
                # traceback formatting cannot include the original values.
                failure = RuntimeError("catalog search failed").with_traceback(exc.__traceback__)
            raise failure

        return validated


router = APIRouter(tags=["search"], route_class=_SearchRoute)
logger = logging.getLogger(__name__)

# Retrieval excludes chunks embedded by any model but the query's (#209), so a
# doc indexed only under another model drops out of results. That is expected
# never to happen outside a premature SEARCH_EMBED_MODEL switch; check at most
# this often per process and warn, without a coverage query on every search.
MISMATCH_CHECK_SECONDS = 60.0
_next_mismatch_check = 0.0


def _warn_on_excluded_docs(storage: StorageAdapter, model: str) -> None:
    global _next_mismatch_check
    now = time.monotonic()
    if now < _next_mismatch_check:
        return
    _next_mismatch_check = now + MISMATCH_CHECK_SECONDS
    try:
        coverage = storage.get_index_coverage([model])
    except Exception as e:  # diagnostics must never fail a search
        logger.warning("embedding-model coverage check failed: %s", type(e).__name__)
        return
    missing = next(c.missing_docs for c in coverage.models if c.model == model)
    if missing:
        logger.warning(
            "%d docs are indexed only under other embedding models and are excluded from "
            "search under %s; run POST /docs/index (see GET /docs/index/status)",
            missing,
            model,
        )


@router.post("/search", response_model=SearchResponse)
@_trace_search
def search(
    payload: SearchRequest,
    request: Request,
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
        _observe(request, "directory_unavailable")
        raise HTTPException(503, "retrieval scope unavailable") from None
    if isinstance(visibility, Channel) and not visibility.source_team_ids:
        _observe(request, "no_channel_teams")
        return SearchResponse(results=[])
    try:
        embedding = embeddings.embed_query(payload.query)
    except EmbeddingError as exc:
        _observe(request, "embedding_error")
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
        _observe(request, "storage_unavailable")
        raise HTTPException(503, "retrieval storage unavailable") from None
    _warn_on_excluded_docs(storage, embedding.model)
    response = SearchResponse(results=results)
    _observe(request, "results" if results else "no_hits")
    return response
