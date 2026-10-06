from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict

from contracts.directory import DirectoryClient, DirectoryUnavailable
from contracts.fetcher import FetchError
from contracts.storage import StorageAdapter
from contracts.types import Doc, DocGrantInput, DocIngest, DocUpdate, IngestResult
from contracts.visibility import ActorContext
from src.api.auth import AuthedKey, get_actor, require_scope
from src.api.authz import get_visible_doc_or_404, read_context, write_context
from src.api.deps import get_directory, get_fetchers, get_storage
from src.content import clamp_content, content_hash
from src.fetch.registry import FetcherRegistry
from src.ingest import BadReference, ingest_doc
from src.config import get_settings
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


@router.post("", response_model=IngestResult)
def create_doc(
    payload: DocIngest,
    response: Response,
    storage: StorageAdapter = Depends(get_storage),
    fetchers: FetcherRegistry = Depends(get_fetchers),
    directory: DirectoryClient = Depends(get_directory),
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
    response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return result


@router.get("", response_model=list[Doc])
def list_docs(
    owning_team_id: UUID | None = None,
    owning_person_id: UUID | None = None,
    source_id: str | None = None,
    tag: str | None = None,
    active_only: bool = True,
    storage: StorageAdapter = Depends(get_storage),
    ctx: ActorContext = Depends(read_context),
) -> list[Doc]:
    return storage.list_docs(
        owning_team_id=owning_team_id,
        owning_person_id=owning_person_id,
        source_id=source_id,
        tag=tag.strip().lower() if tag is not None else None,
        active_only=active_only,
        visibility=ctx,
    )


@router.post("/source-access/refresh-due", response_model=SourceAccessRefreshResult)
def refresh_due_source_access(
    limit: int = Query(default=10, ge=1, le=100),
    storage: StorageAdapter = Depends(get_storage),
    fetchers: FetcherRegistry = Depends(get_fetchers),
    directory: DirectoryClient = Depends(get_directory),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("docs:write")),
) -> SourceAccessRefreshResult:
    """Refresh a bounded batch; intended for the documented daily scheduler."""
    settings = get_settings()
    due_before = datetime.now(timezone.utc) - timedelta(
        hours=settings.source_access_refresh_after_hours
    )
    due = [
        doc
        for doc in storage.list_docs(active_only=True)
        if doc.source_id in GOOGLE_SOURCE_IDS and _source_access_due(doc, due_before)
    ][:limit]
    refreshed = 0
    failed = 0
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
            storage.update_doc(
                doc.id,
                {"source_access_attempted_at": datetime.now(timezone.utc)},
                actor=actor,
            )
            failed += 1
        else:
            refreshed += 1
    return SourceAccessRefreshResult(attempted=len(due), refreshed=refreshed, failed=failed)


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
    actor: str = Depends(get_actor),
    wctx: ActorContext = Depends(write_context),
    _: AuthedKey = Depends(require_scope("docs:write")),
) -> Doc:
    get_visible_doc_or_404(doc_id, wctx, storage)
    doc = storage.get_doc(doc_id)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="doc not found")
    try:
        return _refetch_doc_source(
            doc,
            storage=storage,
            fetchers=fetchers,
            directory=directory,
            actor=actor,
            source_access_max_age_hours=get_settings().source_access_max_age_hours,
        )
    except (FetchError, SourceAccessUnavailable) as e:
        storage.update_doc(
            doc.id,
            {"source_access_attempted_at": datetime.now(timezone.utc)},
            actor=actor,
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))


def _refetch_doc_source(
    doc: Doc,
    *,
    storage: StorageAdapter,
    fetchers: FetcherRegistry,
    directory: DirectoryClient,
    actor: str,
    source_access_max_age_hours: int,
) -> Doc:
    result = fetchers.fetch_for(doc.source_id, doc.url)
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
        # from the source, and manual/owner access remains explicit.
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
    assert updated is not None
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
