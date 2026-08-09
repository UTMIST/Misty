from datetime import datetime, timezone
from uuid import UUID, uuid4

from contracts.types import ApiKey, Doc, DocContentMeta, DocGrant, Source
from contracts.visibility import ActorContext, SEE_ALL, doc_visible


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryStorageAdapter:
    """In-process adapter for tests. Not thread-safe, not persistent. Enforces
    the same invariants as Postgres (dedup by url_normalized, tag uniqueness)."""

    def __init__(self, seed_sources: list[Source] | None = None) -> None:
        self._docs: dict[UUID, Doc] = {}
        self._tags: dict[UUID, set[str]] = {}
        self._sources: dict[str, Source] = {s.id: s for s in (seed_sources or [])}
        self._api_keys: dict[UUID, ApiKey] = {}
        self._api_key_hashes: dict[UUID, str] = {}
        self._grants: dict[UUID, list[tuple[str, UUID | None, datetime, str]]] = {}
        # doc_id -> (content_text, content_hash, fetched_at)
        self._content: dict[UUID, tuple[str, str, datetime | None]] = {}

    def _hydrate(self, doc: Doc) -> Doc:
        data = doc.model_dump()
        data["tags"] = sorted(self._tags.get(doc.id, set()))
        return Doc(**data)

    def _grant_pairs(self, doc_id: UUID) -> list[tuple[str, UUID | None]]:
        return [(gt, gid) for (gt, gid, _at, _by) in self._grants.get(doc_id, [])]

    def _visible(self, doc: Doc, visibility: ActorContext) -> bool:
        return doc_visible(
            visibility,
            owning_person_id=doc.owning_person_id,
            owning_team_id=doc.owning_team_id,
            grants=self._grant_pairs(doc.id),
        )

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
    ) -> Doc:
        now = _now()
        doc = Doc(
            id=uuid4(),
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

    def list_docs(
        self,
        *,
        owning_team_id=None,
        owning_person_id=None,
        source_id=None,
        tag=None,
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
            if not self._visible(doc, visibility):
                continue
            # grants are intentionally omitted here (left as []) to avoid an
            # N+1 grant lookup per doc; only get_doc hydrates grants.
            out.append(self._hydrate(doc))
        return out

    def update_doc(self, doc_id: UUID, values: dict, *, actor: str) -> Doc | None:
        existing = self._docs.get(doc_id)
        if existing is None:
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
        if doc_id not in self._docs:
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
        if doc_id not in self._docs:
            return False
        rows = self._grants.setdefault(doc_id, [])
        if any(gt == grantee_type and gid == grantee_id for (gt, gid, _a, _b) in rows):
            return True
        rows.append((grantee_type, grantee_id, _now(), actor))
        return True

    def remove_grant(self, doc_id, *, grantee_type, grantee_id) -> bool:
        rows = self._grants.get(doc_id, [])
        kept = [r for r in rows if not (r[0] == grantee_type and r[1] == grantee_id)]
        if len(kept) == len(rows):
            return False
        self._grants[doc_id] = kept
        return True

    def list_grants(self, doc_id) -> list[DocGrant]:
        return [
            DocGrant(
                grantee_type=gt, grantee_id=gid, grantee_label=None, created_at=at, created_by=by
            )
            for (gt, gid, at, by) in self._grants.get(doc_id, [])
        ]

    def upsert_doc_content(
        self, doc_id: UUID, *, content_text: str, content_hash: str, fetched_at: datetime | None
    ) -> None:
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
