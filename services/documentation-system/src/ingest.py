"""Ingest orchestration: the URL-in → catalogued-doc flow. Pure of framework
concerns — takes injected storage/fetchers/directory and an actor string."""

from datetime import datetime, timezone

from contracts.directory import DirectoryClient, DirectoryUnavailable
from contracts.fetcher import FetchError
from contracts.storage import DuplicateActiveUrl, StorageAdapter
from contracts.types import DocIngest, IngestResult
from src.content import clamp_content, content_hash
from src.fetch.registry import FetcherRegistry
from src.source_access import (
    GOOGLE_ACCESS_ORIGIN,
    GOOGLE_SOURCE_IDS,
    SourceAccessUnavailable,
    resolve_google_permissions,
    source_grant_expiry,
)
from src.url_norm import derive_source, normalize_url


class BadReference(Exception):
    """A supplied source_id / owning_*_id is invalid and the directory was
    reachable enough to confirm it. Routers map this to HTTP 400."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _apply_grants(storage, doc_id, grants, *, actor: str):
    for g in grants:
        storage.add_grant(doc_id, grantee_type=g.grantee_type, grantee_id=g.grantee_id, actor=actor)


def _merge_into_existing(
    storage, existing, payload: DocIngest, *, actor: str, warnings: list[str] | None = None
) -> IngestResult:
    """Idempotent dedup path: fold this ingest's tags/grants into the already
    catalogued active doc and return it as created=False."""
    for tag in payload.tags:
        storage.add_tag(existing.id, tag)
    _apply_grants(storage, existing.id, payload.grants, actor=actor)
    refreshed = storage.get_doc(existing.id)
    return IngestResult(
        doc=refreshed,
        created=False,
        warnings=[f"already catalogued (added by {existing.created_by})", *(warnings or [])],
    )


def _refresh_existing_source_access(
    existing,
    *,
    storage: StorageAdapter,
    fetchers: FetcherRegistry,
    directory: DirectoryClient,
    actor: str,
    max_age_hours: int,
) -> list[str]:
    """A repeated ingest is also a source-ACL refresh for Google documents."""
    if existing.source_id not in GOOGLE_SOURCE_IDS:
        return []
    warnings: list[str] = []
    try:
        result = fetchers.fetch_for(existing.source_id, existing.url)
        if result.permissions is None:
            raise SourceAccessUnavailable("connector omitted Google source ACL")
        resolved = resolve_google_permissions(result.permissions, directory)
    except (FetchError, SourceAccessUnavailable) as e:
        storage.update_doc(
            existing.id,
            {"source_access_attempted_at": _now()},
            actor=actor,
        )
        return [f"source access refresh failed ({e}); prior source grants retained"]

    warnings.extend(result.warnings)
    warnings.extend(resolved.warnings)
    synced_at = _now()
    storage.replace_source_grants(
        existing.id,
        origin=GOOGLE_ACCESS_ORIGIN,
        grants=resolved.grants,
        synced_at=synced_at,
        expires_at=source_grant_expiry(synced_at, max_age_hours),
        actor=actor,
    )
    if not resolved.grants:
        warnings.append("source ACL has no mapped audience; fetched content was not stored")
        return warnings

    if result.content:
        text, truncated = clamp_content(result.content)
        if truncated:
            warnings.append("content truncated to size cap; stored text is incomplete")
        storage.upsert_doc_content(
            existing.id,
            content_text=text,
            content_hash=content_hash(text),
            fetched_at=synced_at,
        )
    storage.update_doc(
        existing.id,
        {
            "title": result.title or existing.title,
            "content_snapshot": result.content_snapshot or existing.content_snapshot,
            "fetched_at": synced_at,
        },
        actor=actor,
    )
    return warnings


def ingest_doc(
    payload: DocIngest,
    *,
    storage: StorageAdapter,
    fetchers: FetcherRegistry,
    directory: DirectoryClient,
    actor: str,
    source_access_max_age_hours: int = 48,
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
        refresh_warnings = _refresh_existing_source_access(
            existing,
            storage=storage,
            fetchers=fetchers,
            directory=directory,
            actor=actor,
            max_age_hours=source_access_max_age_hours,
        )
        return _merge_into_existing(
            storage, existing, payload, actor=actor, warnings=refresh_warnings
        )

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
    source_grants = None
    source_access_synced_at = None
    source_access_attempted_at = None
    if source is not None and source.content_fetch_enabled:
        try:
            if source_id in GOOGLE_SOURCE_IDS:
                source_access_attempted_at = _now()
            result = fetchers.fetch_for(source_id, payload.url)
            warnings.extend(result.warnings)
            if source_id in GOOGLE_SOURCE_IDS:
                if result.permissions is None:
                    raise SourceAccessUnavailable("connector omitted Google source ACL")
                resolved = resolve_google_permissions(result.permissions, directory)
                warnings.extend(resolved.warnings)
                source_grants = resolved.grants
                source_access_synced_at = _now()
                if not source_grants:
                    warnings.append(
                        "source ACL has no mapped audience; fetched content was not stored"
                    )
                else:
                    title = payload.title or result.title
                    snapshot = result.content_snapshot
                    content = result.content
                    fetched_at = source_access_synced_at
            else:
                title = payload.title or result.title
                snapshot = result.content_snapshot
                content = result.content
                fetched_at = _now()
        except FetchError as e:
            warnings.append(f"content fetch failed ({e}); title fell back to url")
        except SourceAccessUnavailable as e:
            warnings.append(f"source access sync failed ({e}); fetched content was not stored")
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
    if source_grants is not None and source_access_synced_at is not None:
        storage.replace_source_grants(
            doc.id,
            origin=GOOGLE_ACCESS_ORIGIN,
            grants=source_grants,
            synced_at=source_access_synced_at,
            expires_at=source_grant_expiry(source_access_synced_at, source_access_max_age_hours),
            actor=actor,
        )
    elif source_access_attempted_at is not None:
        storage.update_doc(
            doc.id,
            {"source_access_attempted_at": source_access_attempted_at},
            actor=actor,
        )
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
