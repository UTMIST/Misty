from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, delete, false, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from contracts.storage import DuplicateActiveUrl
from contracts.types import ApiKey, Doc, DocContentMeta, DocGrant, Source
from contracts.visibility import Actor, ActorContext, DENY, SEE_ALL
from src.storage.schema import api_keys, doc_content, doc_grants, doc_tags, docs, sources


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _source_row_to_model(row) -> Source:
    return Source(
        id=row.id,
        label=row.label,
        url_patterns=list(row.url_patterns),
        requires_auth=row.requires_auth,
        has_api=row.has_api,
        content_fetch_enabled=row.content_fetch_enabled,
        active=row.active,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
        updated_by=row.updated_by,
    )


def _api_key_row_to_model(row) -> ApiKey:
    return ApiKey(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        scopes=list(row.scopes),
        active=row.active,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
        updated_by=row.updated_by,
    )


class PostgresStorageAdapter:
    """Postgres-backed StorageAdapter using SQLAlchemy Core. Returns Pydantic
    models, never raw rows. Tags are stored in doc_tags and hydrated on read."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _visibility_clause(self, ctx: ActorContext):
        """None means 'no filter' (SEE_ALL). Otherwise a boolean SQL expression."""
        if ctx is SEE_ALL:
            return None
        if ctx is DENY:
            return false()
        actor: Actor = ctx
        team_ids = list(actor.team_ids)
        grant_exists = (
            select(doc_grants.c.id)
            .where(
                doc_grants.c.doc_id == docs.c.id,
                or_(
                    doc_grants.c.grantee_type == "org",
                    and_(
                        doc_grants.c.grantee_type == "person",
                        doc_grants.c.grantee_id == actor.person_id,
                    ),
                    and_(
                        doc_grants.c.grantee_type == "team",
                        doc_grants.c.grantee_id.in_(team_ids),
                    ),
                ),
            )
            .exists()
        )
        owner = or_(
            docs.c.owning_person_id == actor.person_id,
            docs.c.owning_team_id.in_(team_ids),
        )
        return or_(owner, grant_exists)

    def _grants_for(self, conn, doc_id: UUID) -> list[DocGrant]:
        rows = conn.execute(
            select(doc_grants)
            .where(doc_grants.c.doc_id == doc_id)
            .order_by(doc_grants.c.created_at)
        ).all()
        return [
            DocGrant(
                grantee_type=r.grantee_type,
                grantee_id=r.grantee_id,
                grantee_label=None,
                created_at=r.created_at,
                created_by=r.created_by,
            )
            for r in rows
        ]

    def _tags_for(self, conn, doc_id: UUID) -> list[str]:
        rows = conn.execute(
            select(doc_tags.c.tag).where(doc_tags.c.doc_id == doc_id).order_by(doc_tags.c.tag)
        ).all()
        return [r.tag for r in rows]

    def _row_to_doc(self, conn, row, tags: list[str] | None = None) -> Doc:
        # tags may be supplied by a batched caller (list_docs) to avoid an N+1
        # per-doc SELECT; when None, hydrate this doc's tags on its own.
        return Doc(
            id=row.id,
            url=row.url,
            url_normalized=row.url_normalized,
            title=row.title,
            source_id=row.source_id,
            description=row.description,
            owning_team_id=row.owning_team_id,
            owning_team_label=row.owning_team_label,
            owning_person_id=row.owning_person_id,
            owning_person_label=row.owning_person_label,
            content_snapshot=row.content_snapshot,
            fetched_at=row.fetched_at,
            active=row.active,
            tags=self._tags_for(conn, row.id) if tags is None else tags,
            created_at=row.created_at,
            updated_at=row.updated_at,
            created_by=row.created_by,
            updated_by=row.updated_by,
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
        with self._engine.begin() as conn:
            # New docs are always active, so they fall under the partial unique
            # index on url_normalized WHERE active. A concurrent ingest that
            # already inserted an active row for this URL makes this a no-op:
            # on_conflict_do_nothing suppresses the IntegrityError (which would
            # otherwise poison the transaction) and RETURNING yields no row.
            # Signal that to the caller so ingest can fall back to dedup/merge.
            row = conn.execute(
                pg_insert(docs)
                .values(
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
                    created_by=actor,
                    updated_by=actor,
                )
                .on_conflict_do_nothing(
                    index_elements=[docs.c.url_normalized],
                    index_where=text("active"),
                )
                .returning(docs)
            ).one_or_none()
            if row is None:
                raise DuplicateActiveUrl(url_normalized)
            for tag in set(tags):
                conn.execute(insert(doc_tags).values(doc_id=row.id, tag=tag))
            return self._row_to_doc(conn, row)

    def get_doc(self, doc_id: UUID, *, visibility: ActorContext = SEE_ALL) -> Doc | None:
        clause = self._visibility_clause(visibility)
        stmt = select(docs).where(docs.c.id == doc_id)
        if clause is not None:
            stmt = stmt.where(clause)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).one_or_none()
            if row is None:
                return None
            doc = self._row_to_doc(conn, row)
            return doc.model_copy(update={"grants": self._grants_for(conn, row.id)})

    def get_doc_by_normalized_url(self, url_normalized: str) -> Doc | None:
        with self._engine.connect() as conn:
            # Canonical dedup rule: among rows sharing a url_normalized, prefer
            # the active row, then break ties by earliest created_at (id as a
            # final deterministic tiebreak). This keeps a single stable "live"
            # row for a URL even when older rows have been soft-removed
            # (active=False, row retained). Without the `active` preference,
            # once the earliest row is soft-removed every re-ingest rediscovers
            # that dead row, skips the merge, and inserts another active
            # duplicate (bug #5). The in-memory adapter and the partial unique
            # index (url_normalized WHERE active) enforce this same rule.
            row = conn.execute(
                select(docs)
                .where(docs.c.url_normalized == url_normalized)
                .order_by(docs.c.active.desc(), docs.c.created_at, docs.c.id)
                .limit(1)
            ).first()
            return self._row_to_doc(conn, row) if row else None

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
        stmt = select(docs)
        conditions = []
        if active_only:
            conditions.append(docs.c.active.is_(True))
        if owning_team_id is not None:
            conditions.append(docs.c.owning_team_id == owning_team_id)
        if owning_person_id is not None:
            conditions.append(docs.c.owning_person_id == owning_person_id)
        if source_id is not None:
            conditions.append(docs.c.source_id == source_id)
        clause = self._visibility_clause(visibility)
        if clause is not None:
            conditions.append(clause)
        if tag is not None:
            stmt = stmt.where(docs.c.id.in_(select(doc_tags.c.doc_id).where(doc_tags.c.tag == tag)))
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(docs.c.created_at)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
            # Hydrate all tags in one batched query (grouped in memory) instead
            # of a per-doc SELECT — same precedent as the batched grant lookup.
            # grants are intentionally omitted here (left as []) to avoid an
            # N+1 grant lookup per doc; only get_doc hydrates grants.
            ids = [r.id for r in rows]
            tags_by_doc: dict[UUID, list[str]] = {}
            if ids:
                tag_rows = conn.execute(
                    select(doc_tags.c.doc_id, doc_tags.c.tag)
                    .where(doc_tags.c.doc_id.in_(ids))
                    .order_by(doc_tags.c.tag)
                ).all()
                for tr in tag_rows:
                    tags_by_doc.setdefault(tr.doc_id, []).append(tr.tag)
            return [self._row_to_doc(conn, r, tags_by_doc.get(r.id, [])) for r in rows]

    def update_doc(self, doc_id: UUID, values: dict, *, actor: str) -> Doc | None:
        patch = dict(values)
        patch["updated_at"] = _now()
        patch["updated_by"] = actor
        with self._engine.begin() as conn:
            row = conn.execute(
                update(docs).where(docs.c.id == doc_id).values(**patch).returning(docs)
            ).one_or_none()
            return self._row_to_doc(conn, row) if row else None

    def add_tag(self, doc_id: UUID, tag: str) -> bool:
        with self._engine.begin() as conn:
            exists = conn.execute(select(docs.c.id).where(docs.c.id == doc_id)).one_or_none()
            if exists is None:
                return False
            already = conn.execute(
                select(doc_tags.c.tag).where(doc_tags.c.doc_id == doc_id, doc_tags.c.tag == tag)
            ).one_or_none()
            if already is None:
                conn.execute(insert(doc_tags).values(doc_id=doc_id, tag=tag))
            return True

    def remove_tag(self, doc_id: UUID, tag: str) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                delete(doc_tags).where(doc_tags.c.doc_id == doc_id, doc_tags.c.tag == tag)
            )
            return result.rowcount > 0

    def add_grant(self, doc_id, *, grantee_type, grantee_id, actor) -> bool:
        with self._engine.begin() as conn:
            exists = conn.execute(select(docs.c.id).where(docs.c.id == doc_id)).one_or_none()
            if exists is None:
                return False
            already = conn.execute(
                select(doc_grants.c.id).where(
                    doc_grants.c.doc_id == doc_id,
                    doc_grants.c.grantee_type == grantee_type,
                    doc_grants.c.grantee_id.is_(None)
                    if grantee_id is None
                    else doc_grants.c.grantee_id == grantee_id,
                )
            ).one_or_none()
            if already is None:
                # Select-then-insert has a race under concurrency: two callers
                # can both pass the existence check above and one hits the
                # unique index. Use a SAVEPOINT so a duplicate-insert error
                # only rolls back the insert, not the whole transaction, and
                # is treated as idempotent success.
                try:
                    with conn.begin_nested():
                        conn.execute(
                            insert(doc_grants).values(
                                doc_id=doc_id,
                                grantee_type=grantee_type,
                                grantee_id=grantee_id,
                                created_by=actor,
                            )
                        )
                except IntegrityError:
                    pass  # concurrent insert of the same grant — idempotent success
            return True

    def remove_grant(self, doc_id, *, grantee_type, grantee_id) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                delete(doc_grants).where(
                    doc_grants.c.doc_id == doc_id,
                    doc_grants.c.grantee_type == grantee_type,
                    doc_grants.c.grantee_id.is_(None)
                    if grantee_id is None
                    else doc_grants.c.grantee_id == grantee_id,
                )
            )
            return result.rowcount > 0

    def list_grants(self, doc_id) -> list[DocGrant]:
        with self._engine.connect() as conn:
            return self._grants_for(conn, doc_id)

    def upsert_doc_content(
        self, doc_id: UUID, *, content_text: str, content_hash: str, fetched_at: datetime | None
    ) -> None:
        now = _now()
        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(doc_content)
                .values(
                    doc_id=doc_id,
                    content_text=content_text,
                    content_hash=content_hash,
                    fetched_at=fetched_at,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=[doc_content.c.doc_id],
                    set_={
                        "content_text": content_text,
                        "content_hash": content_hash,
                        "fetched_at": fetched_at,
                        "updated_at": now,
                    },
                )
            )

    def get_doc_content(self, doc_id: UUID, *, visibility: ActorContext = SEE_ALL) -> str | None:
        # Joined to `docs` because _visibility_clause builds its predicate from
        # docs columns (owning_*) and a correlated EXISTS over doc_grants.
        clause = self._visibility_clause(visibility)
        stmt = (
            select(doc_content.c.content_text)
            .select_from(doc_content.join(docs, docs.c.id == doc_content.c.doc_id))
            .where(doc_content.c.doc_id == doc_id)
        )
        if clause is not None:
            stmt = stmt.where(clause)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).one_or_none()
            return row.content_text if row is not None else None

    def get_doc_content_meta(
        self, doc_id: UUID, *, visibility: ActorContext = SEE_ALL
    ) -> DocContentMeta | None:
        # Joined to `docs` because _visibility_clause builds its predicate from
        # docs columns (owning_*) and a correlated EXISTS over doc_grants.
        clause = self._visibility_clause(visibility)
        stmt = (
            select(doc_content.c.content_hash, doc_content.c.fetched_at)
            .select_from(doc_content.join(docs, docs.c.id == doc_content.c.doc_id))
            .where(doc_content.c.doc_id == doc_id)
        )
        if clause is not None:
            stmt = stmt.where(clause)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).one_or_none()
            if row is None:
                return None
            return DocContentMeta(content_hash=row.content_hash, fetched_at=row.fetched_at)

    # --- Sources ---

    def list_sources(self, *, active_only: bool = False) -> list[Source]:
        stmt = select(sources)
        if active_only:
            stmt = stmt.where(sources.c.active.is_(True))
        with self._engine.connect() as conn:
            return [_source_row_to_model(r) for r in conn.execute(stmt).all()]

    def get_source(self, source_id: str) -> Source | None:
        with self._engine.connect() as conn:
            row = conn.execute(select(sources).where(sources.c.id == source_id)).one_or_none()
        return _source_row_to_model(row) if row else None

    # --- API keys (identical to team-tracking) ---

    def create_api_key(self, *, name, prefix, key_hash, scopes, actor) -> ApiKey:
        try:
            with self._engine.begin() as conn:
                row = conn.execute(
                    insert(api_keys)
                    .values(
                        name=name,
                        prefix=prefix,
                        key_hash=key_hash,
                        scopes=scopes,
                        created_by=actor,
                        updated_by=actor,
                    )
                    .returning(api_keys)
                ).one()
        except IntegrityError as e:
            raise ValueError(f"name or prefix already exists: {name!r} / {prefix!r}") from e
        return _api_key_row_to_model(row)

    def get_api_key_by_prefix(self, prefix: str) -> ApiKey | None:
        with self._engine.connect() as conn:
            row = conn.execute(select(api_keys).where(api_keys.c.prefix == prefix)).one_or_none()
        return _api_key_row_to_model(row) if row else None

    def get_api_key_hash(self, prefix: str) -> str | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(api_keys.c.key_hash).where(
                    api_keys.c.prefix == prefix,
                    api_keys.c.active.is_(True),
                    api_keys.c.revoked_at.is_(None),
                )
            ).one_or_none()
        return row.key_hash if row else None

    def list_api_keys(self, *, active_only: bool = False) -> list[ApiKey]:
        stmt = select(api_keys)
        if active_only:
            stmt = stmt.where(api_keys.c.active.is_(True), api_keys.c.revoked_at.is_(None))
        with self._engine.connect() as conn:
            return [_api_key_row_to_model(r) for r in conn.execute(stmt).all()]

    def revoke_api_key(self, api_key_id: UUID, *, actor: str) -> ApiKey | None:
        now = _now()
        with self._engine.begin() as conn:
            row = conn.execute(
                update(api_keys)
                .where(api_keys.c.id == api_key_id)
                .values(active=False, revoked_at=now, updated_at=now, updated_by=actor)
                .returning(api_keys)
            ).one_or_none()
        return _api_key_row_to_model(row) if row else None

    def touch_api_key_last_used(self, api_key_id: UUID) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    update(api_keys).where(api_keys.c.id == api_key_id).values(last_used_at=_now())
                )
        except Exception:
            pass
