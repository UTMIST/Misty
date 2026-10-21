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
    Source,
    SourceGrant,
)
from contracts.visibility import ActorContext, RetrievalContext, SEE_ALL


class DuplicateActiveUrl(Exception):
    """Raised by create_doc when a concurrent insert already created an active
    doc for the same url_normalized and the DB partial-unique index
    (url_normalized WHERE active) rejected this write. Ingest catches it and
    falls back to the idempotent dedup/merge path. The in-memory adapter has no
    such constraint and is single-threaded, so it never raises this."""


class ExistingGoogleDoc(Exception):
    """Discovery's insert found an existing Google identity, active or retired."""

    def __init__(self, doc: Doc):
        super().__init__("Google identity already catalogued")
        self.doc = doc


class StorageAdapter(Protocol):
    """Stable contract between the service and persistence. Concrete adapters
    (Postgres, in-memory) implement these with identical semantics."""

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
    ) -> Doc:
        """Insert a doc, coordinating all Google inserts by supported file identity.

        With skip_existing_google=True, atomically check all matching aliases
        (including retired records) before inserting; raise ExistingGoogleDoc
        without modifying that record if found. Requires a supported Google URL.
        The default preserves explicit manual re-ingest after retirement.
        No provider/network work is performed while coordinating the insertion.
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
        content_snapshot, fetched_at). `values` holds already-resolved columns.
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
        """Atomically replace a doc's entire chunk set; [] clears it.

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
        """Chunks ordered by ordinal, using get_doc's visibility predicate.

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
        """replace_doc_chunks, but only while the chunks still describe the
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

    def get_doc_index_state(self, doc_id: UUID) -> DocIndexState | None:
        """The current chunk set's body/title hashes, model, and size; None when the
        doc has no chunks (or does not exist). Internal indexing state: no
        visibility predicate, never returned to API callers."""
        ...

    def list_docs_for_indexing(
        self, *, stale_only: bool = True, after: UUID | None = None, limit: int = 100
    ) -> list[UUID]:
        """Doc ids whose index may need work, ascending by id, ids > `after`.

        Always includes docs that have chunks but should not (inactive, or no
        stored content), so a failed cleanup is retryable. With stale_only,
        adds active docs with content whose chunks are missing or were built
        from a different body/title hash (or unknown title hash); without it,
        adds every active doc with content (a forced full pass, e.g. after an embedding-model change).
        """
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
