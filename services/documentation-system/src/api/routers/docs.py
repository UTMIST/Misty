from datetime import datetime, timedelta, timezone
from typing import get_args
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.directory import DirectoryClient, DirectoryUnavailable
from contracts.embedder import Embedder
from contracts.fetcher import FetchError, SourceGone
from contracts.storage import StorageAdapter
from contracts.types import (
    Doc,
    DocGrantInput,
    DocIngest,
    DocUpdate,
    IndexModelCoverage,
    IngestResult,
)
from contracts.visibility import ActorContext
from src.api.auth import AuthedKey, get_actor, require_scope
from src.api.authz import get_visible_doc_or_404, read_context, write_context
from src.api.deps import (
    get_directory,
    get_embedder,
    get_fetchers,
    get_storage,
    get_target_embedder,
)
from src.content import clamp_content, content_hash
from src.fetch.registry import FetcherRegistry
from src.indexing import IndexStatus, index_doc, index_embedders, index_warning
from src.ingest import BadReference, ingest_doc
from src.config import get_settings
from src.meeting_fetch import fetch_catalog_content
from src.source_access import (
    GOOGLE_ACCESS_ORIGIN,
    GOOGLE_SOURCE_IDS,
    SourceAccessUnavailable,
    resolve_google_permissions,
    source_grant_expiry,
)

router = APIRouter(prefix="/docs", tags=["docs"])


class TagBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tag: str


class SourceAccessRefreshResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempted: int
    refreshed: int
    failed: int
    # Refreshed docs whose new content could not be indexed; POST /docs/index
    # retries them.
    index_failed: int = 0


class IndexRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Index exactly these docs; otherwise page through the docs that need work.
    doc_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=100)
    # Listing mode: only docs whose chunks are missing, stale, or orphaned.
    # False lists every doc with content (pair with `force` to re-embed all).
    stale_only: bool = True
    # Re-embed even when the content hash is unchanged (#209 model change).
    force: bool = False
    # Listing-mode cursor: pass the previous response's `next_after`.
    after: UUID | None = None
    limit: int = Field(default=25, ge=1, le=100)

    @model_validator(mode="after")
    def _one_mode(self) -> "IndexRequest":
        if self.doc_ids is not None and self.after is not None:
            raise ValueError("pass doc_ids or after, not both")
        return self


class IndexDocResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id: UUID
    status: IndexStatus
    chunk_count: int
    detail: str | None = None


class IndexRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempted: int
    indexed: int
    unchanged: int
    cleared: int
    skipped: int
    failed: int
    results: list[IndexDocResult]
    # Listing mode: more docs may follow; send this as `after`. None when done.
    next_after: UUID | None = None


def _source_access_due(doc: Doc, due_before: datetime) -> bool:
    last_attempt = max(
        (
            value
            for value in (doc.source_access_synced_at, doc.source_access_attempted_at)
            if value is not None
        ),
        default=None,
    )
    return last_attempt is None or last_attempt <= due_before


class IndexStatusResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Resolved model ids: the set search reads, and the re-embed target if any.
    serving_model: str
    target_model: str | None
    # Active docs with stored content.
    indexable_docs: int
    # Docs a stale-only POST /docs/index run would list right now.
    pending_docs: int
    models: list[IndexModelCoverage]


@router.post("", response_model=IngestResult)
def create_doc(
    payload: DocIngest,
    response: Response,
    storage: StorageAdapter = Depends(get_storage),
    fetchers: FetcherRegistry = Depends(get_fetchers),
    directory: DirectoryClient = Depends(get_directory),
    embedder: Embedder = Depends(get_embedder),
    target: Embedder | None = Depends(get_target_embedder),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("docs:write")),
) -> IngestResult:
    try:
        result = ingest_doc(
            payload,
            storage=storage,
            fetchers=fetchers,
            directory=directory,
            actor=actor,
            source_access_max_age_hours=get_settings().source_access_max_age_hours,
        )
    except BadReference as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    # After the catalog write, never inside it: an indexing failure is a
    # warning and the doc stays catalogued. Runs on the dedup path too (a
    # repeated Google ingest may have stored new text); unchanged content is
    # a no-op without a provider call.
    warning = index_warning(
        index_doc(result.doc.id, storage=storage, embedder=embedder, target=target)
    )
    if warning is not None:
        result.warnings.append(warning)
    response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return result


@router.post("/index", response_model=IndexRunResult)
def index_docs(
    payload: IndexRequest,
    storage: StorageAdapter = Depends(get_storage),
    embedder: Embedder = Depends(get_embedder),
    target: Embedder | None = Depends(get_target_embedder),
    _: AuthedKey = Depends(require_scope("docs:index")),
) -> IndexRunResult:
    """Admin (re-)indexing of stored content: the retry path for indexing
    failures and the entry point for re-embedding (#209). Synchronous and
    bounded; a caller loops on `next_after` until it is null."""
    next_after = None
    if payload.doc_ids is not None:
        doc_ids = list(dict.fromkeys(payload.doc_ids))
        missing = [doc_id for doc_id in doc_ids if storage.get_doc(doc_id) is None]
        if missing:
            # Checked for every id before indexing any, so a typo costs nothing.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"docs not found: {', '.join(map(str, missing))}",
            )
    else:
        doc_ids = storage.list_docs_for_indexing(
            models=[e.model for e in index_embedders(embedder, target)],
            stale_only=payload.stale_only,
            after=payload.after,
            limit=payload.limit,
        )
        if len(doc_ids) == payload.limit:
            next_after = doc_ids[-1]
    results = []
    for doc_id in doc_ids:
        outcome = index_doc(
            doc_id, storage=storage, embedder=embedder, target=target, force=payload.force
        )
        results.append(
            IndexDocResult(
                doc_id=doc_id,
                status=outcome.status,
                chunk_count=outcome.chunk_count,
                detail=outcome.detail,
            )
        )
    counts = {name: sum(r.status == name for r in results) for name in get_args(IndexStatus)}
    return IndexRunResult(attempted=len(results), results=results, next_after=next_after, **counts)


@router.get("/index/status", response_model=IndexStatusResult)
def index_status(
    storage: StorageAdapter = Depends(get_storage),
    embedder: Embedder = Depends(get_embedder),
    target: Embedder | None = Depends(get_target_embedder),
    _: AuthedKey = Depends(require_scope("docs:index")),
) -> IndexStatusResult:
    """Chunk coverage per embedding model and the docs POST /docs/index would
    still list: a re-embed (#209) is ready to cut over at zero pending."""
    models = [e.model for e in index_embedders(embedder, target)]
    coverage = storage.get_index_coverage(models)
    pending = 0
    after = None
    while page := storage.list_docs_for_indexing(models=models, after=after, limit=500):
        pending += len(page)
        after = page[-1]
    return IndexStatusResult(
        serving_model=embedder.model,
        target_model=models[1] if len(models) > 1 else None,
        indexable_docs=coverage.indexable_docs,
        pending_docs=pending,
        models=coverage.models,
    )


@router.get("", response_model=list[Doc])
def list_docs(
    owning_team_id: UUID | None = None,
    owning_person_id: UUID | None = None,
    source_id: str | None = None,
    tag: str | None = None,
    has_content: bool | None = None,
    active_only: bool = True,
    storage: StorageAdapter = Depends(get_storage),
    ctx: ActorContext = Depends(read_context),
) -> list[Doc]:
    return storage.list_docs(
        owning_team_id=owning_team_id,
        owning_person_id=owning_person_id,
        source_id=source_id,
        tag=tag.strip().lower() if tag is not None else None,
        has_content=has_content,
        active_only=active_only,
        visibility=ctx,
    )


@router.post("/source-access/refresh-runs/{run_id}")
def acquire_source_refresh_run(
    run_id: UUID,
    storage: StorageAdapter = Depends(get_storage),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("docs:write")),
) -> dict[str, UUID]:
    if not storage.acquire_source_refresh_run(run_id, actor=actor):
        raise HTTPException(status_code=409, detail="source refresh run already active")
    return {"run_id": run_id}


@router.delete("/source-access/refresh-runs/{run_id}")
def release_source_refresh_run(
    run_id: UUID,
    storage: StorageAdapter = Depends(get_storage),
    _: AuthedKey = Depends(require_scope("docs:write")),
) -> dict[str, UUID]:
    if not storage.release_source_refresh_run(run_id):
        raise HTTPException(status_code=409, detail="source refresh run owner does not match")
    return {"run_id": run_id}


@router.post("/source-access/refresh-due", response_model=SourceAccessRefreshResult)
def refresh_due_source_access(
    limit: int = Query(default=10, ge=1, le=100),
    storage: StorageAdapter = Depends(get_storage),
    fetchers: FetcherRegistry = Depends(get_fetchers),
    directory: DirectoryClient = Depends(get_directory),
    embedder: Embedder = Depends(get_embedder),
    target: Embedder | None = Depends(get_target_embedder),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("docs:write")),
) -> SourceAccessRefreshResult:
    """Refresh a bounded batch; intended for the documented hourly scheduler."""
    settings = get_settings()
    due_before = datetime.now(timezone.utc) - timedelta(
        hours=settings.source_access_refresh_after_hours
    )
    fetch_enabled = {source.id for source in storage.list_sources() if source.content_fetch_enabled}
    due = [
        doc
        for doc in storage.list_docs(active_only=True)
        if doc.source_id in GOOGLE_SOURCE_IDS
        and doc.source_id in fetch_enabled
        and _source_access_due(doc, due_before)
    ][:limit]
    refreshed = 0
    failed = 0
    index_failed = 0
    for doc in due:
        try:
            _refetch_doc_source(
                doc,
                storage=storage,
                fetchers=fetchers,
                directory=directory,
                actor=actor,
                source_access_max_age_hours=settings.source_access_max_age_hours,
            )
        except (FetchError, SourceAccessUnavailable):
            _record_failed_refetch(doc, storage=storage, actor=actor)
            failed += 1
        else:
            refreshed += 1
            outcome = index_doc(doc.id, storage=storage, embedder=embedder, target=target)
            index_failed += outcome.status == "failed"
    return SourceAccessRefreshResult(
        attempted=len(due), refreshed=refreshed, failed=failed, index_failed=index_failed
    )


@router.get("/{doc_id}", response_model=Doc)
def get_doc(
    doc_id: UUID,
    storage: StorageAdapter = Depends(get_storage),
    directory: DirectoryClient = Depends(get_directory),
    ctx: ActorContext = Depends(read_context),
) -> Doc:
    doc = storage.get_doc(doc_id, visibility=ctx)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="doc not found")
    return _backfill_labels(doc, storage, directory)


@router.patch("/{doc_id}", response_model=Doc)
def update_doc(
    doc_id: UUID,
    payload: DocUpdate,
    storage: StorageAdapter = Depends(get_storage),
    directory: DirectoryClient = Depends(get_directory),
    embedder: Embedder = Depends(get_embedder),
    target: Embedder | None = Depends(get_target_embedder),
    actor: str = Depends(get_actor),
    wctx: ActorContext = Depends(write_context),
    _: AuthedKey = Depends(require_scope("docs:write")),
) -> Doc:
    get_visible_doc_or_404(doc_id, wctx, storage)
    values = payload.model_dump(exclude_unset=True)
    # Re-resolve labels when an owner id changes and the directory is reachable.
    # A genuinely unknown id (directory reachable, record not found) is a 400,
    # matching POST's BadReference behavior. A down directory degrades to a
    # null label instead of failing the request.
    if "owning_team_id" in values:
        values["owning_team_label"] = _resolve_owner_label(
            directory.get_team_label, values["owning_team_id"], "owning_team_id not found"
        )
    if "owning_person_id" in values:
        values["owning_person_label"] = _resolve_owner_label(
            directory.get_person_label, values["owning_person_id"], "owning_person_id not found"
        )
    updated = storage.update_doc(doc_id, values, actor=actor)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="doc not found")
    if "active" in values or "title" in values:
        # Retiring clears the doc's chunks; reactivating rebuilds them. A
        # failure is logged and left for POST /docs/index (a retired doc that
        # still has chunks is listed as needing work). Title-only updates
        # also refresh the context embedded alongside the unchanged body.
        index_doc(doc_id, storage=storage, embedder=embedder, target=target)
    return updated


@router.post("/{doc_id}/tags", response_model=Doc)
def add_tag(
    doc_id: UUID,
    body: TagBody,
    storage: StorageAdapter = Depends(get_storage),
    wctx: ActorContext = Depends(write_context),
    _: AuthedKey = Depends(require_scope("docs:write")),
) -> Doc:
    get_visible_doc_or_404(doc_id, wctx, storage)
    if not storage.add_tag(doc_id, body.tag.strip().lower()):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="doc not found")
    return storage.get_doc(doc_id)


@router.delete("/{doc_id}/tags/{tag}", response_model=Doc)
def remove_tag(
    doc_id: UUID,
    tag: str,
    storage: StorageAdapter = Depends(get_storage),
    wctx: ActorContext = Depends(write_context),
    _: AuthedKey = Depends(require_scope("docs:write")),
) -> Doc:
    # get_visible_doc_or_404 already 404s if the doc is missing/invisible, so
    # the extra existence-check fetch it used to do here is redundant.
    get_visible_doc_or_404(doc_id, wctx, storage)
    storage.remove_tag(doc_id, tag.strip().lower())
    return storage.get_doc(doc_id)


@router.post("/{doc_id}/grants", response_model=Doc)
def add_grant(
    doc_id: UUID,
    body: DocGrantInput,
    storage: StorageAdapter = Depends(get_storage),
    wctx: ActorContext = Depends(write_context),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("docs:write")),
) -> Doc:
    get_visible_doc_or_404(doc_id, wctx, storage)  # 404 if actor can't see it
    storage.add_grant(
        doc_id, grantee_type=body.grantee_type, grantee_id=body.grantee_id, actor=actor
    )
    return storage.get_doc(doc_id)


@router.delete("/{doc_id}/grants", response_model=Doc)
def remove_grant(
    doc_id: UUID,
    body: DocGrantInput,
    storage: StorageAdapter = Depends(get_storage),
    wctx: ActorContext = Depends(write_context),
    _: AuthedKey = Depends(require_scope("docs:write")),
) -> Doc:
    get_visible_doc_or_404(doc_id, wctx, storage)
    storage.remove_grant(doc_id, grantee_type=body.grantee_type, grantee_id=body.grantee_id)
    return storage.get_doc(doc_id)


@router.post("/{doc_id}/refetch", response_model=Doc)
def refetch(
    doc_id: UUID,
    storage: StorageAdapter = Depends(get_storage),
    fetchers: FetcherRegistry = Depends(get_fetchers),
    directory: DirectoryClient = Depends(get_directory),
    embedder: Embedder = Depends(get_embedder),
    target: Embedder | None = Depends(get_target_embedder),
    actor: str = Depends(get_actor),
    wctx: ActorContext = Depends(write_context),
    _: AuthedKey = Depends(require_scope("docs:write")),
) -> Doc:
    get_visible_doc_or_404(doc_id, wctx, storage)
    doc = storage.get_doc(doc_id)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="doc not found")
    try:
        _refetch_doc_source(
            doc,
            storage=storage,
            fetchers=fetchers,
            directory=directory,
            actor=actor,
            source_access_max_age_hours=get_settings().source_access_max_age_hours,
        )
    except (FetchError, SourceAccessUnavailable) as e:
        _record_failed_refetch(doc, storage=storage, actor=actor)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    # A failure is logged and left for POST /docs/index: the doc's chunks no
    # longer match its content hash, so it is listed as stale.
    index_doc(doc_id, storage=storage, embedder=embedder, target=target)
    # Reconciliation may have revoked the caller's cached source grant.
    return get_visible_doc_or_404(doc_id, wctx, storage)


def _record_failed_refetch(doc: Doc, *, storage: StorageAdapter, actor: str):
    storage.update_doc(
        doc.id,
        {"source_access_attempted_at": datetime.now(timezone.utc)},
        actor=actor,
    )


def _refetch_doc_source(
    doc: Doc,
    *,
    storage: StorageAdapter,
    fetchers: FetcherRegistry,
    directory: DirectoryClient,
    actor: str,
    source_access_max_age_hours: int,
) -> Doc:
    if doc.purged_at:
        raise FetchError("document was permanently purged")
    expected_content = storage.get_doc_content_meta(doc.id)
    try:
        result = fetch_catalog_content(doc.source_id, doc.url, storage=storage, fetchers=fetchers)
    except SourceGone:
        # Remove text and chunks together only if no newer fetch stored text
        # while this request was waiting for the connector.
        storage.delete_doc_content(doc.id, expected=expected_content)
        raise
    now = datetime.now(timezone.utc)
    if doc.source_id in GOOGLE_SOURCE_IDS:
        if result.permissions is None:
            raise SourceAccessUnavailable("connector omitted Google source ACL")
        resolved = resolve_google_permissions(result.permissions, directory)
        storage.replace_source_grants(
            doc.id,
            origin=GOOGLE_ACCESS_ORIGIN,
            grants=resolved.grants,
            synced_at=now,
            expires_at=source_grant_expiry(now, source_access_max_age_hours),
            actor=actor,
        )
        # Successful zero-grant reconciliation is a revocation. Keep prior
        # bytes at rest but do not refresh them; visibility no longer derives
        # from the source, and manual grants remain independent.
        if not resolved.grants:
            refreshed = storage.get_doc(doc.id)
            assert refreshed is not None
            return refreshed
    # A content-less refetch (empty body: page deleted, paywalled, connector
    # regressed to title-only) deliberately leaves the existing doc_content
    # row AND the existing snapshot untouched rather than wiping them — stale
    # text beats silently emptying the RAG substrate. This means content_hash
    # can legitimately lag behind docs.fetched_at; doc_content.fetched_at, not
    # content_hash, is the freshness signal for this row.
    # Both guards test truthiness, not `is not None`: FetchResult's contract
    # says an empty extraction is None, but a connector that returns "" anyway
    # must not be able to blank stored content through this path.
    if result.content:
        text, _truncated = clamp_content(result.content)
        storage.upsert_doc_content(
            doc.id,
            content_text=text,
            content_hash=content_hash(text),
            fetched_at=now,
        )
    updated = storage.update_doc(
        doc.id,
        {
            "title": result.title or doc.title,
            "content_snapshot": result.content_snapshot or doc.content_snapshot,
            "fetched_at": now,
        },
        actor=actor,
    )
    if updated is None:
        raise FetchError("document is no longer available for refresh")
    return updated


def _safe_label(lookup, entity_id):
    if entity_id is None:
        return None
    try:
        return lookup(entity_id)
    except DirectoryUnavailable:
        return None


def _resolve_owner_label(lookup, entity_id, not_found_detail: str):
    """Resolve an owner id's label for PATCH, distinguishing a down directory
    (degrade to null label) from a genuinely unknown id (400, like POST)."""
    if entity_id is None:
        return None
    try:
        label = lookup(entity_id)
    except DirectoryUnavailable:
        return None
    if label is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=not_found_detail)
    return label


def _backfill_labels(doc: Doc, storage: StorageAdapter, directory: DirectoryClient) -> Doc:
    """Resolve any null owner/team labels on read (directory was down at ingest)."""
    values: dict = {}
    if doc.owning_team_id is not None and doc.owning_team_label is None:
        label = _safe_label(directory.get_team_label, doc.owning_team_id)
        if label is not None:
            values["owning_team_label"] = label
    if doc.owning_person_id is not None and doc.owning_person_label is None:
        label = _safe_label(directory.get_person_label, doc.owning_person_id)
        if label is not None:
            values["owning_person_label"] = label
    if values:
        updated = storage.update_doc(doc.id, values, actor="label-backfill")
        # update_doc's Doc reconstruction always yields grants=[]; the input
        # doc is already hydrated with grants, so carry them forward.
        return updated.model_copy(update={"grants": doc.grants})
    return doc
