from datetime import datetime
from typing import Protocol
from uuid import UUID

from contracts.types import ApiKey, Doc, DocContentMeta, DocGrant, Source
from contracts.visibility import ActorContext, SEE_ALL


class DuplicateActiveUrl(Exception):
    """Raised by create_doc when a concurrent insert already created an active
    doc for the same url_normalized and the DB partial-unique index
    (url_normalized WHERE active) rejected this write. Ingest catches it and
    falls back to the idempotent dedup/merge path. The in-memory adapter has no
    such constraint and is single-threaded, so it never raises this."""


class StorageAdapter(Protocol):
    """Stable contract between the service and persistence. Concrete adapters
    (Postgres, in-memory) implement these with identical semantics."""

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
    ) -> Doc: ...
    def get_doc(self, doc_id: UUID, *, visibility: ActorContext = SEE_ALL) -> Doc | None: ...
    def get_doc_by_normalized_url(self, url_normalized: str) -> Doc | None: ...
    def list_docs(
        self,
        *,
        owning_team_id: UUID | None = None,
        owning_person_id: UUID | None = None,
        source_id: str | None = None,
        tag: str | None = None,
        active_only: bool = True,
        visibility: ActorContext = SEE_ALL,
    ) -> list[Doc]: ...
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
