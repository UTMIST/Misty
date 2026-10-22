from datetime import datetime, timezone
from math import sqrt
from threading import Lock
from uuid import UUID, uuid4

from contracts.retrieval import ChunkCitation, QueryEmbedding, SearchHit
from contracts.storage import DuplicateDocId, ExistingGoogleDoc, PurgedDocument
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
from contracts.visibility import ActorContext, SEE_ALL, doc_visible
from src.content import content_hash
from src.google_identity import google_file_id
from src.retrieval import candidate_limit, fuse, keyword_density, validate_search
from src.storage.chunks import validate_chunks_for_content, validate_doc_chunks
from contracts.meeting import MeetingRecord
from src.storage.meeting import changed, catalog_values


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryStorageAdapter:
    """In-process adapter for tests. Not thread-safe, not persistent. Enforces
    the same invariants as Postgres (dedup by url_normalized, tag uniqueness)."""

    def is_ready(self) -> bool:
        return True

    def __init__(self, seed_sources: list[Source] | None = None) -> None:
        self._doc_creation_lock = Lock()
        self._docs: dict[UUID, Doc] = {}
        self._tags: dict[UUID, set[str]] = {}
        self._sources: dict[str, Source] = {s.id: s for s in (seed_sources or [])}
        self._api_keys: dict[UUID, ApiKey] = {}
        self._api_key_hashes: dict[UUID, str] = {}
        self._grants: dict[UUID, list[DocGrant]] = {}
        # doc_id -> (content_text, content_hash, fetched_at)
        self._content: dict[UUID, tuple[str, str, datetime | None]] = {}
        self._chunks: dict[UUID, list[DocChunk]] = {}
        self._source_refresh_lock = Lock()
        self._source_refresh_run: UUID | None = None
        self._meeting_lock = Lock()
        self._meetings: dict[UUID, MeetingRecord] = {}

    def put_meeting(self, record):
        with self._meeting_lock:
            existing = self._meetings.get(record.meeting_id)
            if existing:
                if existing.payload_hash != record.payload_hash:
                    raise ValueError("meeting payload conflict")
                return existing.model_copy(deep=True), False
            self._meetings[record.meeting_id] = record.model_copy(deep=True)
            return record.model_copy(deep=True), True

    def get_meeting(self, meeting_id):
        record = self._meetings.get(meeting_id)
        return record.model_copy(deep=True) if record else None

    def get_meeting_by_google_id(self, document_id):
        return next(
            (
                r.model_copy(deep=True)
                for r in self._meetings.values()
                if r.google_document_id == document_id
            ),
            None,
        )

    def list_meetings(self):
        return sorted(
            (r.model_copy(deep=True) for r in self._meetings.values()),
            key=lambda r: (r.created_at, r.meeting_id),
        )

    def change_meeting(self, record, values, *, actor, live_at=None):
        with self._meeting_lock:
            current = self._meetings.get(record.meeting_id)
            if current is None or current.version != record.version:
                return None
            if live_at is not None and (
                current.removed_at
                or not current.create_lease_until
                or current.create_lease_until <= live_at
                or current.create_attempt_id != record.create_attempt_id
            ):
                return None
            updated = changed(current, values, actor)
            if updated.google_document_id and any(
                r.meeting_id != updated.meeting_id
                and r.google_document_id == updated.google_document_id
                for r in self._meetings.values()
            ):
                raise ValueError("Google document already linked")
            self._meetings[record.meeting_id] = updated
            return updated.model_copy(deep=True)

    def catalog_meeting(self, record, result, grants, *, expires_at, actor):
        with self._meeting_lock, self._doc_creation_lock:
            current = self._meetings.get(record.meeting_id)
            if (
                not current
                or current.version != record.version
                or current.removed_at
                or not current.google_document_id
            ):
                return False
            values, body, digest, now = catalog_values(current, result, actor)
            doc_id = values.pop("id")
            existing = self.get_doc_by_google_file_id(current.google_document_id)
            if existing:
                if existing.purged_at or not existing.active:
                    raise ValueError("meeting catalog identity retired")
                doc_id = existing.id
                values.update(created_at=existing.created_at, created_by=existing.created_by)
            self._docs[doc_id] = Doc(id=doc_id, **values)
            self._tags[doc_id] = {"meeting"}
            self._grants[doc_id] = []
            self._content.pop(doc_id, None)
            self._chunks.pop(doc_id, None)
            if body:
                self.upsert_doc_content(
                    doc_id, content_text=body, content_hash=digest, fetched_at=now
                )
            self.replace_source_grants(
                doc_id,
                origin="google_drive",
                grants=grants,
                synced_at=now,
                expires_at=expires_at,
                actor=actor,
            )
            self._meetings[current.meeting_id] = changed(
                current,
                {
                    "doc_id": doc_id,
                    "persistence_status": "saved",
                    "last_error": None,
                },
                actor,
            )
            return True

    def purge_doc(self, doc_id, *, actor):
        doc = self._docs.get(doc_id)
        if doc is None:
            return False
        self._docs[doc_id] = doc.model_copy(
            update={
                "title": None,
                "description": None,
                "content_snapshot": None,
                "owning_team_id": None,
                "owning_team_label": None,
                "owning_person_id": None,
                "owning_person_label": None,
                "active": False,
                "purged_at": doc.purged_at or _now(),
                "updated_at": _now(),
                "updated_by": actor,
            }
        )
        self._content.pop(doc_id, None)
        self._chunks.pop(doc_id, None)
        self._grants.pop(doc_id, None)
        self._tags.pop(doc_id, None)
        return True

    def acquire_source_refresh_run(self, run_id: UUID, *, actor: str) -> bool:
        with self._source_refresh_lock:
            if self._source_refresh_run is not None:
                return False
            self._source_refresh_run = run_id
            return True

    def release_source_refresh_run(self, run_id: UUID) -> bool:
        with self._source_refresh_lock:
            if self._source_refresh_run != run_id:
                return False
            self._source_refresh_run = None
            return True

    def _hydrate(self, doc: Doc) -> Doc:
        data = doc.model_dump()
        data["tags"] = sorted(self._tags.get(doc.id, set()))
        return Doc(**data)

    def _grant_pairs(self, doc_id: UUID) -> list[tuple[str, UUID | None, str]]:
        now = _now()
        return [
            (grant.grantee_type, grant.grantee_id, grant.origin)
            for grant in self._grants.get(doc_id, [])
            if grant.origin == "manual" or grant.expires_at is not None and grant.expires_at > now
        ]

    def _visible(self, doc: Doc, visibility: ActorContext) -> bool:
        return doc_visible(
            visibility,
            owning_person_id=doc.owning_person_id,
            owning_team_id=doc.owning_team_id,
            grants=self._grant_pairs(doc.id),
        )

    def search_chunks(self, *, query, embedding, visibility, k, min_cosine) -> list[SearchHit]:
        embedding: QueryEmbedding = validate_search(query, embedding, visibility, k, min_cosine)
        query_norm = sqrt(sum(v * v for v in embedding.vector))
        vector, keyword = [], []
        for doc in self._docs.values():
            if not doc.active or not self._visible(doc, visibility):
                continue
            for chunk in self._chunks.get(doc.id, []):
                if (
                    chunk.embedding_model != embedding.model
                    or chunk.dimensions != embedding.dimensions
                ):
                    continue
                hit = SearchHit(
                    doc_id=doc.id,
                    title=doc.title,
                    url=doc.url,
                    source_id=doc.source_id,
                    text=chunk.chunk_text,
                    chunk=ChunkCitation(
                        **chunk.model_dump(include=set(ChunkCitation.model_fields))
                    ),
                    score=0,
                )
                norm = sqrt(sum(v * v for v in chunk.embedding))
                if norm:
                    cosine = sum(a * b for a, b in zip(embedding.vector, chunk.embedding)) / (
                        query_norm * norm
                    )
                    cosine = round(max(-1.0, min(1.0, cosine)), 6)
                    if cosine >= min_cosine:
                        vector.append((cosine, hit))
                density = keyword_density(chunk.chunk_text, query)
                if density:
                    keyword.append((density, hit))
        limit = candidate_limit(k)

        def ranked(branch):
            branch.sort(key=lambda pair: (-pair[0], pair[1].doc_id, pair[1].chunk.ordinal))
            return [hit for _, hit in branch[:limit]]

        return fuse(ranked(vector), ranked(keyword), k)

    # --- Docs ---

    def create_doc(
        self,
        *,
        url,
        url_normalized,
        source_id,
        title,
        description,
        owning_team_id,
        owning_team_label,
        owning_person_id,
        owning_person_label,
        content_snapshot,
        fetched_at,
        tags,
        actor,
        skip_existing_google=False,
        doc_id=None,
    ) -> Doc:
        file_id = google_file_id(url_normalized)
        if skip_existing_google and file_id is None:
            raise ValueError("Google identity required for discovery insertion")
        with self._doc_creation_lock:
            existing = (
                self.get_doc_by_google_file_id(file_id)
                if file_id
                else self.get_doc_by_normalized_url(url_normalized)
            )
            if existing is not None and existing.purged_at:
                raise PurgedDocument("document was permanently purged")
            if file_id:
                meeting = self.get_meeting_by_google_id(file_id)
                if meeting and meeting.removed_at:
                    raise PurgedDocument("meeting was permanently removed")
            if skip_existing_google:
                existing = self.get_doc_by_google_file_id(file_id)
                if existing is not None:
                    raise ExistingGoogleDoc(existing)
            # Checked after the Google identity check so both adapters raise the same
            # error when a call would fail either way.
            if doc_id is not None and doc_id in self._docs:
                raise DuplicateDocId(doc_id)
            now = _now()
            doc = Doc(
                id=doc_id or uuid4(),
                url=url,
                url_normalized=url_normalized,
                source_id=source_id,
                title=title,
                description=description,
                owning_team_id=owning_team_id,
                owning_team_label=owning_team_label,
                owning_person_id=owning_person_id,
                owning_person_label=owning_person_label,
                content_snapshot=content_snapshot,
                fetched_at=fetched_at,
                source_access_synced_at=None,
                source_access_attempted_at=None,
                active=True,
                tags=[],
                created_at=now,
                updated_at=now,
                created_by=actor,
                updated_by=actor,
            )
            self._docs[doc.id] = doc
            self._tags[doc.id] = set(tags)
            return self._hydrate(doc)

    def get_doc(self, doc_id: UUID, *, visibility: ActorContext = SEE_ALL) -> Doc | None:
        doc = self._docs.get(doc_id)
        if doc is None or not self._visible(doc, visibility):
            return None
        hydrated = self._hydrate(doc)
        return hydrated.model_copy(update={"grants": self.list_grants(doc_id)})

    def get_doc_by_normalized_url(self, url_normalized: str) -> Doc | None:
        # Canonical dedup rule (must match PostgresStorageAdapter and the
        # partial unique index on url_normalized WHERE active): among docs
        # sharing a url_normalized, prefer the active row, then break ties by
        # earliest created_at (id as a final deterministic tiebreak). Preferring
        # the active row is what stops a soft-removed older row from shadowing
        # the live one and causing re-ingest to spawn duplicate active docs
        # (bug #5).
        matches = [d for d in self._docs.values() if d.url_normalized == url_normalized]
        if not matches:
            return None
        winner = min(matches, key=lambda d: (not d.active, d.created_at, d.id))
        return self._hydrate(winner)

    def get_doc_by_google_file_id(self, file_id: str) -> Doc | None:
        matches = [d for d in self._docs.values() if google_file_id(d.url_normalized) == file_id]
        if not matches:
            return None
        return self._hydrate(min(matches, key=lambda d: (not d.active, d.created_at, d.id)))

    def list_docs(
        self,
        *,
        owning_team_id=None,
        owning_person_id=None,
        source_id=None,
        tag=None,
        has_content=None,
        active_only=True,
        visibility: ActorContext = SEE_ALL,
    ) -> list[Doc]:
        out = []
        for doc in self._docs.values():
            if active_only and not doc.active:
                continue
            if owning_team_id is not None and doc.owning_team_id != owning_team_id:
                continue
            if owning_person_id is not None and doc.owning_person_id != owning_person_id:
                continue
            if source_id is not None and doc.source_id != source_id:
                continue
            if tag is not None and tag not in self._tags.get(doc.id, set()):
                continue
            if has_content is not None and (doc.id in self._content) != has_content:
                continue
            if not self._visible(doc, visibility):
                continue
            # grants are intentionally omitted here (left as []) to avoid an
            # N+1 grant lookup per doc; only get_doc hydrates grants.
            out.append(self._hydrate(doc))
        return out

    def update_doc(self, doc_id: UUID, values: dict, *, actor: str) -> Doc | None:
        existing = self._docs.get(doc_id)
        if existing is None or existing.purged_at:
            return None
        data = existing.model_dump()
        data.update(values)
        data["updated_at"] = _now()
        data["updated_by"] = actor
        data["tags"] = []
        updated = Doc(**data)
        self._docs[doc_id] = updated
        return self._hydrate(updated)

    def add_tag(self, doc_id: UUID, tag: str) -> bool:
        if doc_id not in self._docs or self._docs[doc_id].purged_at:
            return False
        self._tags.setdefault(doc_id, set()).add(tag)
        return True

    def remove_tag(self, doc_id: UUID, tag: str) -> bool:
        tags = self._tags.get(doc_id)
        if not tags or tag not in tags:
            return False
        tags.discard(tag)
        return True

    def add_grant(self, doc_id, *, grantee_type, grantee_id, actor) -> bool:
        if doc_id not in self._docs or self._docs[doc_id].purged_at:
            return False
        rows = self._grants.setdefault(doc_id, [])
        if any(
            grant.origin == "manual"
            and grant.grantee_type == grantee_type
            and grant.grantee_id == grantee_id
            for grant in rows
        ):
            return True
        rows.append(
            DocGrant(
                grantee_type=grantee_type,
                grantee_id=grantee_id,
                grantee_label=None,
                created_at=_now(),
                created_by=actor,
            )
        )
        return True

    def remove_grant(self, doc_id, *, grantee_type, grantee_id) -> bool:
        rows = self._grants.get(doc_id, [])
        kept = [
            grant
            for grant in rows
            if not (
                grant.origin == "manual"
                and grant.grantee_type == grantee_type
                and grant.grantee_id == grantee_id
            )
        ]
        if len(kept) == len(rows):
            return False
        self._grants[doc_id] = kept
        return True

    def list_grants(self, doc_id) -> list[DocGrant]:
        return list(self._grants.get(doc_id, []))

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
        existing = self._docs.get(doc_id)
        if existing is None or existing.purged_at:
            return False
        rows = [grant for grant in self._grants.get(doc_id, []) if grant.origin != origin]
        rows.extend(
            DocGrant(
                grantee_type=grant.grantee_type,
                grantee_id=grant.grantee_id,
                grantee_label=None,
                created_at=synced_at,
                created_by=actor,
                origin=origin,
                source_permission_id=grant.source_permission_id,
                source_principal=grant.source_principal,
                source_role=grant.source_role,
                source_inherited=grant.source_inherited,
                source_inherited_from=grant.source_inherited_from,
                source_expires_at=grant.source_expires_at,
                expires_at=min(expires_at, grant.source_expires_at)
                if grant.source_expires_at is not None
                else expires_at,
            )
            for grant in grants
        )
        self._grants[doc_id] = rows
        data = existing.model_dump()
        data.update(
            source_access_synced_at=synced_at,
            source_access_attempted_at=synced_at,
            updated_at=synced_at,
            updated_by=actor,
            tags=[],
            grants=[],
        )
        self._docs[doc_id] = Doc(**data)
        return True

    def upsert_doc_content(
        self, doc_id: UUID, *, content_text: str, content_hash: str, fetched_at: datetime | None
    ) -> None:
        if doc_id in self._docs and self._docs[doc_id].purged_at:
            return
        # No doc-existence check: Postgres enforces this with a FK, but this
        # adapter is a test double and callers always upsert a doc they just
        # created. Content for an unknown doc is simply unreachable via
        # get_doc_content, which returns None when the doc is missing.
        self._content[doc_id] = (content_text, content_hash, fetched_at)

    def get_doc_content(self, doc_id: UUID, *, visibility: ActorContext = SEE_ALL) -> str | None:
        doc = self._docs.get(doc_id)
        if doc is None or not self._visible(doc, visibility):
            return None
        entry = self._content.get(doc_id)
        return entry[0] if entry is not None else None

    def get_doc_content_meta(
        self, doc_id: UUID, *, visibility: ActorContext = SEE_ALL
    ) -> DocContentMeta | None:
        doc = self._docs.get(doc_id)
        if doc is None or not self._visible(doc, visibility):
            return None
        entry = self._content.get(doc_id)
        if entry is None:
            return None
        _text, content_hash, fetched_at = entry
        return DocContentMeta(content_hash=content_hash, fetched_at=fetched_at)

    def replace_doc_chunks(self, doc_id: UUID, chunks: list[DocChunk]) -> bool:
        validated = validate_doc_chunks(chunks)
        if doc_id not in self._docs or self._docs[doc_id].purged_at:
            return False
        self._chunks[doc_id] = validated
        return True

    def list_doc_chunks(
        self, doc_id: UUID, *, visibility: ActorContext = SEE_ALL
    ) -> list[DocChunk]:
        doc = self._docs.get(doc_id)
        if doc is None or not self._visible(doc, visibility):
            return []
        return [chunk.model_copy(deep=True) for chunk in self._chunks.get(doc_id, [])]

    def replace_doc_chunks_if_current(
        self,
        doc_id: UUID,
        expected_content_hash: str,
        chunks: list[DocChunk],
        *,
        expected_title_hash: str,
    ) -> bool:
        validated = validate_chunks_for_content(chunks, expected_content_hash, expected_title_hash)
        doc = self._docs.get(doc_id)
        content = self._content.get(doc_id)
        if doc is None or not doc.active or content is None:
            return False
        if (
            content[1] != expected_content_hash
            or content_hash(doc.title or "") != expected_title_hash
        ):
            return False
        self._chunks[doc_id] = validated
        return True

    def clear_doc_chunks_if_unindexable(self, doc_id: UUID) -> bool:
        doc = self._docs.get(doc_id)
        if doc is not None and doc.active and doc_id in self._content:
            return False
        self._chunks.pop(doc_id, None)
        return True

    def delete_doc_content(self, doc_id: UUID, *, expected: DocContentMeta | None) -> bool:
        if doc_id not in self._docs:
            return False
        if self.get_doc_content_meta(doc_id) != expected:
            return False
        self._content.pop(doc_id, None)
        self._chunks.pop(doc_id, None)
        return True

    def get_doc_index_state(self, doc_id: UUID) -> DocIndexState | None:
        chunks = self._chunks.get(doc_id) if doc_id in self._docs else None
        if not chunks:
            return None
        return DocIndexState(
            source_content_hash=chunks[0].source_content_hash,
            source_title_hash=chunks[0].source_title_hash,
            embedding_model=chunks[0].embedding_model,
            chunk_count=len(chunks),
        )

    def list_docs_for_indexing(
        self, *, stale_only: bool = True, after: UUID | None = None, limit: int = 100
    ) -> list[UUID]:
        out = []
        for doc_id in sorted(self._docs):
            if after is not None and doc_id <= after:
                continue
            content = self._content.get(doc_id)
            chunks = self._chunks.get(doc_id)
            if self._docs[doc_id].active and content is not None:
                due = (
                    not stale_only
                    or not chunks
                    or chunks[0].source_content_hash != content[1]
                    or chunks[0].source_title_hash != content_hash(self._docs[doc_id].title or "")
                )
            else:
                due = bool(chunks)
            if due:
                out.append(doc_id)
        return out[:limit]

    # --- Sources ---

    def list_sources(self, *, active_only: bool = False) -> list[Source]:
        out = list(self._sources.values())
        if active_only:
            out = [s for s in out if s.active]
        return out

    def get_source(self, source_id: str) -> Source | None:
        return self._sources.get(source_id)

    # --- API keys (mirror team-tracking) ---

    def create_api_key(self, *, name, prefix, key_hash, scopes, actor) -> ApiKey:
        if any(k.name == name for k in self._api_keys.values()):
            raise ValueError(f"name already exists: {name}")
        if any(k.prefix == prefix for k in self._api_keys.values()):
            raise ValueError(f"prefix already exists: {prefix}")
        now = _now()
        key = ApiKey(
            id=uuid4(),
            name=name,
            prefix=prefix,
            scopes=scopes,
            active=True,
            revoked_at=None,
            last_used_at=None,
            created_at=now,
            updated_at=now,
            created_by=actor,
            updated_by=actor,
        )
        self._api_keys[key.id] = key
        self._api_key_hashes[key.id] = key_hash
        return key

    def get_api_key_by_prefix(self, prefix: str) -> ApiKey | None:
        for k in self._api_keys.values():
            if k.prefix == prefix:
                return k
        return None

    def get_api_key_hash(self, prefix: str) -> str | None:
        for k in self._api_keys.values():
            if k.prefix == prefix and k.active and k.revoked_at is None:
                return self._api_key_hashes.get(k.id)
        return None

    def list_api_keys(self, *, active_only: bool = False) -> list[ApiKey]:
        keys = list(self._api_keys.values())
        if active_only:
            keys = [k for k in keys if k.active and k.revoked_at is None]
        return keys

    def revoke_api_key(self, api_key_id: UUID, *, actor: str) -> ApiKey | None:
        existing = self._api_keys.get(api_key_id)
        if existing is None:
            return None
        now = _now()
        data = existing.model_dump()
        data.update(active=False, revoked_at=now, updated_at=now, updated_by=actor)
        revoked = ApiKey(**data)
        self._api_keys[api_key_id] = revoked
        return revoked

    def touch_api_key_last_used(self, api_key_id: UUID) -> None:
        existing = self._api_keys.get(api_key_id)
        if existing is None:
            return
        data = existing.model_dump()
        data["last_used_at"] = _now()
        self._api_keys[api_key_id] = ApiKey(**data)
