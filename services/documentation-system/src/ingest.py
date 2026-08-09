"""Ingest orchestration: the URL-in → catalogued-doc flow. Pure of framework
concerns — takes injected storage/fetchers/directory and an actor string."""

from datetime import datetime, timezone

from contracts.directory import DirectoryClient, DirectoryUnavailable
from contracts.fetcher import FetchError
from contracts.storage import DuplicateActiveUrl, StorageAdapter
from contracts.types import DocIngest, IngestResult
from src.content import clamp_content, content_hash
from src.fetch.registry import FetcherRegistry
from src.url_norm import derive_source, normalize_url


class BadReference(Exception):
    """A supplied source_id / owning_*_id is invalid and the directory was
    reachable enough to confirm it. Routers map this to HTTP 400."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _apply_grants(storage, doc_id, grants, *, actor: str):
    for g in grants:
        storage.add_grant(doc_id, grantee_type=g.grantee_type, grantee_id=g.grantee_id, actor=actor)


def _merge_into_existing(storage, existing, payload: DocIngest, *, actor: str) -> IngestResult:
    """Idempotent dedup path: fold this ingest's tags/grants into the already
    catalogued active doc and return it as created=False."""
    for tag in payload.tags:
        storage.add_tag(existing.id, tag)
    _apply_grants(storage, existing.id, payload.grants, actor=actor)
    refreshed = storage.get_doc(existing.id)
    return IngestResult(
        doc=refreshed,
        created=False,
        warnings=[f"already catalogued (added by {existing.created_by})"],
    )


def ingest_doc(
    payload: DocIngest,
    *,
    storage: StorageAdapter,
    fetchers: FetcherRegistry,
    directory: DirectoryClient,
    actor: str,
) -> IngestResult:
    warnings: list[str] = []
    url_normalized = normalize_url(payload.url)

    # 1. Dedup — idempotent re-ingest merges any new tags.
    # NOTE: intentionally NOT visibility-gated. docs:write is the trust
    # boundary for ingest, and on-behalf-of ingest isn't used in practice.
    # An on-behalf-of ingest colliding with a doc the actor can't see is a
    # known, accepted limitation (revisit if on-behalf-of ingest is ever
    # introduced).
    existing = storage.get_doc_by_normalized_url(url_normalized)
    if existing is not None and existing.active:
        return _merge_into_existing(storage, existing, payload, actor=actor)

    # 2. Determine source — caller-supplied wins, else derive.
    if payload.source_id is not None:
        source = storage.get_source(payload.source_id)
        if source is None:
            raise BadReference(f"source_id not found: {payload.source_id}")
        source_id = source.id
    else:
        source_id = derive_source(payload.url, storage.list_sources())
        source = storage.get_source(source_id)

    # 3. Fetch, best-effort.
    title = payload.title
    snapshot = None
    content = None
    fetched_at = None
    if source is not None and source.content_fetch_enabled:
        try:
            result = fetchers.fetch_for(source_id, payload.url)
            title = payload.title or result.title
            snapshot = result.content_snapshot
            content = result.content
            fetched_at = _now()
            warnings.extend(result.warnings)
        except FetchError as e:
            warnings.append(f"content fetch failed ({e}); title fell back to url")
    elif source is not None and source.requires_auth:
        warnings.append(f"source '{source_id}' requires auth; no snapshot fetched")
    if title is None:
        title = payload.url

    # 4. Resolve ownership labels (validate when reachable, degrade when not).
    team_label = _resolve_label(
        directory.get_team_label, payload.owning_team_id, "owning_team_id", warnings
    )
    person_label = _resolve_label(
        directory.get_person_label, payload.owning_person_id, "owning_person_id", warnings
    )

    # 5. Persist. Between the dedup read (step 1) and this insert, a concurrent
    # ingest of the same URL can create the active row first; the DB partial
    # unique index (url_normalized WHERE active) makes create_doc raise
    # DuplicateActiveUrl for the loser (bug #11). Treat that as an idempotent
    # dedup: re-read the winning active row and merge tags/grants into it.
    try:
        doc = storage.create_doc(
            url=payload.url,
            url_normalized=url_normalized,
            source_id=source_id,
            title=title,
            description=payload.description,
            owning_team_id=payload.owning_team_id,
            owning_team_label=team_label,
            owning_person_id=payload.owning_person_id,
            owning_person_label=person_label,
            content_snapshot=snapshot,
            fetched_at=fetched_at,
            tags=payload.tags,
            actor=actor,
        )
    except DuplicateActiveUrl:
        existing = storage.get_doc_by_normalized_url(url_normalized)
        if existing is not None and existing.active:
            return _merge_into_existing(storage, existing, payload, actor=actor)
        raise
    _apply_grants(storage, doc.id, payload.grants, actor=actor)
    # Truthiness, not `is not None`: an empty extraction is None per
    # FetchResult's contract, but a connector returning "" must not create a
    # doc_content row holding nothing.
    if content:
        content, truncated = clamp_content(content)
        if truncated:
            warnings.append("content truncated to size cap; stored text is incomplete")
        storage.upsert_doc_content(
            doc.id,
            content_text=content,
            content_hash=content_hash(content),
            fetched_at=fetched_at,
        )
    doc = storage.get_doc(doc.id)  # re-hydrate with grants for the response
    return IngestResult(doc=doc, created=True, warnings=warnings)


def _resolve_label(lookup, entity_id, field_name: str, warnings: list[str]) -> str | None:
    """Return a display label for entity_id. Raises BadReference if the
    directory is reachable but the id is unknown; appends a warning and returns
    None if the directory is unavailable. Returns None when entity_id is None."""
    if entity_id is None:
        return None
    try:
        label = lookup(entity_id)
    except DirectoryUnavailable:
        warnings.append(f"directory unavailable; {field_name} label deferred")
        return None
    if label is None:
        raise BadReference(f"{field_name} not found: {entity_id}")
    return label
