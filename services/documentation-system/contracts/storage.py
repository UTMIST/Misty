from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from contracts.retrieval import QueryEmbedding, SearchHit
from contracts.types import (
    ApiKey,
    Doc,
    DocChunk,
    DocContentMeta,
    DocGrant,
    DocIndexState,
    IndexCoverage,
    Source,
    SourceGrant,
)
from contracts.visibility import ActorContext, RetrievalContext, SEE_ALL
from contracts.meeting import MeetingRecord
from contracts.fetcher import FetchResult


class DuplicateActiveUrl(Exception):
    """Raised by create_doc when a concurrent insert already created an active
    doc for the same url_normalized and the DB partial-unique index
    (url_normalized WHERE active) rejected this write. Ingest catches it and
    falls back to the idempotent dedup/merge path. The in-memory adapter has no
    such constraint and is single-threaded, so it never raises this."""


class DuplicateDocId(Exception):
    """Raised by create_doc when the supplied doc_id is already catalogued.

    Only reachable when a caller supplies `doc_id`; a generated one cannot collide.
    Both adapters reject it rather than overwrite, because the alternatives diverge:
    the in-memory store would replace the existing document while its content, chunks
    and grants stayed attached to that id, and Postgres would raise a primary-key
    violation. Retired documents keep their rows, so a retired id is taken too.
    """

    def __init__(self, doc_id):
        super().__init__("doc id already catalogued")
        self.doc_id = doc_id


class PurgedDocument(ValueError):
    """A permanent removal tombstone forbids creating another catalog copy."""


class ExistingGoogleDoc(Exception):
    """Discovery's insert found an existing Google identity, active or retired."""

    def __init__(self, doc: Doc):
        super().__init__("Google identity already catalogued")
        self.doc = doc


class StorageAdapter(Protocol):
    """Stable contract between the service and persistence. Concrete adapters
    (Postgres, in-memory) implement these with identical semantics."""

    def put_meeting(self, record: MeetingRecord) -> tuple[MeetingRecord, bool]:
        """Insert atomically; identical payload_hash returns existing + False.

        Different hash raises ValueError. Never modifies an existing payload.
        """
        ...

    def get_meeting(self, meeting_id: UUID) -> MeetingRecord | None: ...
    def get_meeting_by_google_id(self, document_id: str) -> MeetingRecord | None:
        """Match a canonical Google ID or a recorded removed duplicate identity."""
        ...

    def list_meetings(self) -> list[MeetingRecord]:
        """Detached records in created_at/meeting_id order; internal use only."""
        ...

    def change_meeting(
        self, record: MeetingRecord, values: dict, *, actor: str, live_at: datetime | None = None
    ) -> MeetingRecord | None:
        """Atomic compare-and-swap on version; None on conflict/missing.

        Canonical ID is immutable. Removed records never reopen; stale results
        cannot clear a removal marker. Every mutation increments version.
        A confirmed google_deleted_at timestamp is also immutable.
        Per-file google_deletions intents cannot be forgotten or confirmations
        reversed. Canonical adoption must reject any deletion-marked identity.
        With live_at, also require the same attempt, an unexpired lease and no
        removal marker in the atomic write predicate.
        """
        ...

    def catalog_meeting(
        self,
        record: MeetingRecord,
        result: FetchResult,
        grants: list[SourceGrant],
        *,
        expires_at: datetime,
        actor: str,
    ) -> bool:
        """Commit Doc, content, source grants, saved state and index_pending=True
        in one transaction.

        Verify record.version, canonical ID and absence of removal under the
        same lock. False on conflict. No network calls inside the transaction.
        """
        ...

    def purge_doc(self, doc_id: UUID, *, actor: str) -> bool:
        """Atomically erase content/snapshot/chunks/grants/tags/ownership/title,
        mark inactive + purged_at. Idempotent, False only for missing docs.
        All future writes except purge must refuse a purged doc.
        """
        ...

    def purge_meeting_catalog(
        self, meeting_id: UUID, google_document_ids: list[str], *, actor: str
    ) -> MeetingRecord:
        """For an already marked removal, atomically fence the supplied Google
        identities plus the canonical ID and every google_deletions identity
        (pending or confirmed), and purge ALL matching catalog aliases and the
        linked doc_id. Serialize with ordinary Google insertion so an
        earlier insert is purged and a later insert refuses the tombstone.
        Persist the union of identities for future ingestion and late cleanup.
        Raise ValueError for an unknown or unmarked meeting. Return latest record.
        """
        ...

    def acquire_source_refresh_run(self, run_id: UUID, *, actor: str) -> bool:
        """Atomically claim the single runner slot; False if occupied, even by run_id.

        Ownership has no expiry: process death or ambiguous HTTP completion must
        not admit another runner. Release explicitly after confirming work stopped.
        """
        ...

    def release_source_refresh_run(self, run_id: UUID) -> bool:
        """Release only a matching owner; False leaves the current owner unchanged."""
        ...

    def get_doc_by_google_file_id(self, file_id: str) -> Doc | None:
        """Match supported Google URL aliases, including legacy/manual rows.

        Prefer active, then earliest created_at/id. Includes retired rows so
        discovery can honor operator retirement. No visibility filter: trusted
        ingest only, like get_doc_by_normalized_url. Never grants access.
        """
        ...

    def is_ready(self) -> bool:
        """Return whether the backing store can answer a trivial query."""
        ...

    def search_chunks(
        self,
        *,
        query: str,
        embedding: QueryEmbedding,
        visibility: RetrievalContext,
        k: int,
        min_cosine: float,
    ) -> list[SearchHit]:
        """Hybrid RRF search; no default/unfiltered visibility is allowed.

        Both branches filter current grants, active documents, and the query's
        resolved model/dimensions before their max(50,5*k) candidate limits.
        Cosine is rounded to six decimals; keyword rank is term density.
        A chunk qualifies at the absolute cosine floor OR by matching all
        ASCII alphanumeric query terms. RRF constant 60; ties use doc id and
        ordinal. Return at most k citations, possibly none. Invalid arguments
        raise ValueError before I/O; storage errors raise RetrievalUnavailable.
        """
        ...

    # Docs
    def create_doc(
        self,
        *,
        url: str,
        url_normalized: str,
        source_id: str,
        title: str | None,
        description: str | None,
        owning_team_id: UUID | None,
        owning_team_label: str | None,
        owning_person_id: UUID | None,
        owning_person_label: str | None,
        content_snapshot: str | None,
        fetched_at,
        tags: list[str],
        actor: str,
        skip_existing_google: bool = False,
        doc_id: UUID | None = None,
    ) -> Doc:
        """Insert a doc, coordinating all Google inserts by supported file identity.

        With skip_existing_google=True, atomically check all matching aliases
        (including retired records) before inserting; raise ExistingGoogleDoc
        without modifying that record if found. Requires a supported Google URL.
        The default preserves explicit manual re-ingest after retirement.
        No provider/network work is performed while coordinating the insertion.

        `doc_id` defaults to a generated UUID. Pass one only where the identity must be
        reproducible across runs: retrieval breaks ranking ties with the doc id, so a
        generated id makes the order of equally scored documents vary between runs. The
        evaluation harness (src/eval/) depends on that order being stable. A supplied id
        that is already catalogued, active or retired, raises DuplicateDocId; it is
        never allowed to overwrite the existing document.
        """
        ...

    def get_doc(self, doc_id: UUID, *, visibility: ActorContext = SEE_ALL) -> Doc | None: ...
    def get_doc_by_normalized_url(self, url_normalized: str) -> Doc | None: ...
    def list_docs(
        self,
        *,
        owning_team_id: UUID | None = None,
        owning_person_id: UUID | None = None,
        source_id: str | None = None,
        tag: str | None = None,
        has_content: bool | None = None,
        active_only: bool = True,
        visibility: ActorContext = SEE_ALL,
    ) -> list[Doc]:
        """Docs matching every supplied filter, oldest first, grants omitted.

        `has_content` selects on the presence of a `doc_content` ROW: None (the
        default) does not filter, False returns only docs with no content row,
        True only docs with one."""
        ...

    def update_doc(self, doc_id: UUID, values: dict, *, actor: str) -> Doc | None:
        """Patch scalar columns (title, description, active, owning_* ids/labels,
        content_snapshot, fetched_at, source_id). `values` holds already-resolved
        columns; callers validate source_id against the source registry.
        Returns None if no such doc."""
        ...

    def add_tag(self, doc_id: UUID, tag: str) -> bool:
        """Idempotently add a tag. True if the doc exists, False otherwise."""
        ...

    def remove_tag(self, doc_id: UUID, tag: str) -> bool:
        """Remove a tag. True if a row was deleted, False otherwise."""
        ...

    def add_grant(
        self, doc_id: UUID, *, grantee_type: str, grantee_id: UUID | None, actor: str
    ) -> bool:
        """Idempotently add a grant. False if the doc does not exist."""
        ...

    def remove_grant(self, doc_id: UUID, *, grantee_type: str, grantee_id: UUID | None) -> bool:
        """Remove a grant. True if a row was deleted."""
        ...

    def list_grants(self, doc_id: UUID) -> list[DocGrant]:
        """Grants for a doc, grantee_label unset (resolved at the API layer)."""
        ...

    def replace_source_grants(
        self,
        doc_id: UUID,
        *,
        origin: str,
        grants: list[SourceGrant],
        synced_at: datetime,
        expires_at: datetime,
        actor: str,
    ) -> bool:
        """Atomically replace one source's derived grants and mark sync time.

        An empty list is a successful revocation. Manual grants are untouched.
        Returns False only when the document does not exist.
        """
        ...

    def upsert_doc_content(
        self, doc_id: UUID, *, content_text: str, content_hash: str, fetched_at: datetime | None
    ) -> None:
        """Insert or replace the full extracted text for a doc. `content_hash`
        is the sha256 hex of `content_text`, stored for cheap change detection
        — it detects *content change*, not freshness. A refetch that returns
        no content is never passed to this method, so the prior row (text,
        hash, and fetched_at) is left untouched; a consumer deciding whether
        to re-embed should compare `doc_content.fetched_at`, not content_hash,
        to know how current the row is."""
        ...

    def get_doc_content(self, doc_id: UUID, *, visibility: ActorContext = SEE_ALL) -> str | None:
        """Full extracted text for a doc, or None if the doc has no content OR
        the actor cannot see it. Full content is at least as sensitive as the
        snapshot, so this enforces the same visibility predicate as get_doc —
        an invisible doc is indistinguishable from one with no content."""
        ...

    def get_doc_content_meta(
        self, doc_id: UUID, *, visibility: ActorContext = SEE_ALL
    ) -> DocContentMeta | None:
        """Hash + capture time for a doc's stored content, or None if the doc has
        no content OR the actor cannot see it. Gated by the same visibility
        predicate as get_doc_content — metadata about content is content.
        `content_hash` detects CHANGE, not freshness: compare `fetched_at` to know
        how current the row is."""
        ...

    def replace_doc_chunks(self, doc_id: UUID, chunks: list[DocChunk]) -> bool:
        """Atomically replace all of a doc's chunks, every model's set, with
        one set; [] clears them.

        Validate before accessing storage: ordinals must be exactly 0..n-1
        (input order does not matter), with one body/title hash and embedding model
        per set. Each chunk must satisfy DocChunk's validation, including its
        MAX_CHUNK_CHARS text limit. Invalid input raises ValueError without
        changing prior rows.
        False for a missing doc, True otherwise; repeating a write creates no
        duplicates. Concurrent replacements cannot produce a mixed set.

        This stores supplied provenance; it does not check current doc_content,
        trigger indexing, or implement retirement/model-mismatch policy.
        """
        ...

    def list_doc_chunks(
        self, doc_id: UUID, *, visibility: ActorContext = SEE_ALL
    ) -> list[DocChunk]:
        """Chunks of every model's set, ordered by model then ordinal, using
        get_doc's visibility predicate.

        Missing/invisible docs and docs without chunks all return []. Returned
        values are immutable, with tuple embeddings, and the returned list is
        detached from persisted data.
        Like get_doc_content, this does not filter on the doc's active flag.
        """
        ...

    def replace_doc_chunks_if_current(
        self,
        doc_id: UUID,
        expected_content_hash: str,
        chunks: list[DocChunk],
        *,
        expected_title_hash: str,
    ) -> bool:
        """Replace the doc's set for the chunks' embedding model, leaving other
        models' sets in place ([] clears every set: there is nothing to embed).
        Only while the chunks still describe the
        doc's current content: the doc exists and is active, and its stored
        doc_content hash equals `expected_content_hash`, and SHA-256 of its
        title (None treated as empty) equals `expected_title_hash`. Checked
        and written in one transaction, so an older body/title embedding
        cannot replace a newer set. Every chunk must carry both expected
        hashes (ValueError otherwise, before storage is touched). False,
        writing nothing, when the condition does not hold.
        """
        ...

    def clear_doc_chunks_if_unindexable(self, doc_id: UUID) -> bool:
        """Clear chunks only if the doc is still inactive or has no content.

        Check and clear atomically with respect to chunk writers and changes
        to the doc's active flag. False, without writing, if the doc is active
        with stored content; True when cleared or the doc no longer exists.
        A delayed cleanup must never erase a newer successful index.
        """
        ...

    def delete_doc_content(self, doc_id: UUID, *, expected: DocContentMeta | None) -> bool:
        """Delete a doc's stored text and its chunks together, in one
        transaction; the catalog row (and its snapshot) stays. For a source
        confirmed gone: with no content there is nothing to re-index, so the
        removal sticks until a later successful fetch stores text again.
        `expected` is the content metadata read before fetching (None if
        absent). Compare both hash and fetched_at under the write lock so a
        delayed failure cannot delete a newer successful fetch, even when it
        stored identical text. False without writing if metadata changed or
        the doc does not exist; idempotent when expected is None."""
        ...

    def get_doc_index_states(self, doc_id: UUID) -> dict[str, DocIndexState]:
        """Each stored chunk set's body/title hashes, model, and size, keyed by
        embedding model; {} when the doc has no chunks (or does not exist).
        Internal indexing state: no visibility predicate, never returned to API
        callers."""
        ...

    def delete_doc_chunks_except(self, doc_id: UUID, keep_models: Sequence[str]) -> bool:
        """Delete the doc's chunk sets for every model not in `keep_models`
        (sets left behind by a finished or abandoned re-embed). Serialized with
        the other chunk writers. True when anything was deleted."""
        ...

    def list_docs_for_indexing(
        self,
        *,
        models: Sequence[str],
        stale_only: bool = True,
        after: UUID | None = None,
        limit: int = 100,
    ) -> list[UUID]:
        """Doc ids whose index may need work, ascending by id, ids > `after`.

        `models` are the resolved ids indexing maintains: the one search
        serves, plus a re-embed target while one is configured.
        Always includes docs that have chunks but should not (inactive, or no
        stored content), so a failed cleanup is retryable. With stale_only,
        adds active docs with content that lack a set for one of `models`,
        hold one built from a different body/title hash (or unknown title
        hash), or hold a set for a model outside `models`, so re-running the
        listing resumes an interrupted re-embed. A doc whose text is
        whitespace only (empty after str.strip()) is listed only while it has
        chunks: indexing it clears them and records nothing, so it would
        otherwise stay pending forever. Without stale_only, adds
        every active doc with content (a forced full pass).
        """
        ...

    def get_index_coverage(self, models: Sequence[str]) -> IndexCoverage:
        """Per-model chunk coverage across the catalog, for every stored model
        plus each of `models` (reported even with no chunks). Counts sets of
        any doc; `missing_docs` counts only active docs with content."""
        ...

    # Sources
    def list_sources(self, *, active_only: bool = False) -> list[Source]: ...
    def get_source(self, source_id: str) -> Source | None: ...

    # API keys (Level 2 security) — identical semantics to team-tracking
    def create_api_key(
        self, *, name: str, prefix: str, key_hash: str, scopes: list[str], actor: str
    ) -> ApiKey: ...
    def get_api_key_by_prefix(self, prefix: str) -> ApiKey | None: ...
    def get_api_key_hash(self, prefix: str) -> str | None: ...
    def list_api_keys(self, *, active_only: bool = False) -> list[ApiKey]: ...
    def revoke_api_key(self, api_key_id: UUID, *, actor: str) -> ApiKey | None: ...
    def touch_api_key_last_used(self, api_key_id: UUID) -> None: ...
