from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import (
    Float,
    Numeric,
    and_,
    cast,
    delete,
    false,
    func,
    insert,
    literal,
    literal_column,
    or_,
    select,
    text,
    union_all,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from contracts.retrieval import ChunkCitation, RetrievalUnavailable, SearchHit
from contracts.storage import DuplicateActiveUrl
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
from contracts.visibility import Actor, ActorContext, Channel, DENY, SEE_ALL
from src.content import content_hash
from src.google_identity import GOOGLE_ID_PATTERNS
from src.retrieval import candidate_limit, fuse, terms, validate_search
from src.storage.chunks import validate_chunks_for_content, validate_doc_chunks
from src.storage.schema import (
    api_keys,
    doc_chunks,
    doc_content,
    doc_grants,
    doc_tags,
    docs,
    sources,
)


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

    def is_ready(self) -> bool:
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return False
        return True

    def _visibility_clause(self, ctx: ActorContext):
        """None means 'no filter' (SEE_ALL). Otherwise a boolean SQL expression."""
        if ctx is SEE_ALL:
            return None
        if ctx is DENY:
            return false()
        if isinstance(ctx, Channel):
            return (
                select(doc_grants.c.id)
                .where(
                    doc_grants.c.doc_id == docs.c.id,
                    doc_grants.c.origin == "google_drive",
                    doc_grants.c.expires_at > func.now(),
                    doc_grants.c.grantee_type == "team",
                    doc_grants.c.grantee_id.in_(list(ctx.source_team_ids)),
                )
                .exists()
            )
        actor: Actor = ctx
        team_ids = list(actor.team_ids)
        grant_exists = (
            select(doc_grants.c.id)
            .where(
                doc_grants.c.doc_id == docs.c.id,
                or_(
                    doc_grants.c.origin == "manual",
                    doc_grants.c.expires_at > func.now(),
                ),
                or_(
                    and_(
                        doc_grants.c.origin == "manual",
                        doc_grants.c.grantee_type == "org",
                    ),
                    and_(
                        doc_grants.c.grantee_type == "person",
                        doc_grants.c.grantee_id == actor.person_id,
                    ),
                    and_(
                        doc_grants.c.origin == "manual",
                        doc_grants.c.grantee_type == "team",
                        doc_grants.c.grantee_id.in_(team_ids),
                    ),
                    and_(
                        doc_grants.c.origin != "manual",
                        doc_grants.c.grantee_type == "team",
                        doc_grants.c.grantee_id.in_(list(actor.source_team_ids)),
                    ),
                ),
            )
            .exists()
        )
        return grant_exists

    def search_chunks(self, *, query, embedding, visibility, k, min_cosine) -> list[SearchHit]:
        embedding = validate_search(query, embedding, visibility, k, min_cosine)
        # One statement/snapshot for both paths. Materializing the authorized
        # corpus prevents either top-k branch from ranking invisible rows.
        eligible = (
            select(
                docs.c.id.label("doc_id"),
                docs.c.title,
                docs.c.url,
                docs.c.source_id,
                doc_chunks.c.ordinal,
                doc_chunks.c.start_offset,
                doc_chunks.c.end_offset,
                doc_chunks.c.source_content_hash,
                doc_chunks.c.chunk_text,
                doc_chunks.c.embedding,
            )
            .select_from(doc_chunks.join(docs, docs.c.id == doc_chunks.c.doc_id))
            .where(
                docs.c.active.is_(True),
                self._visibility_clause(visibility),
                doc_chunks.c.embedding_model == embedding.model,
                doc_chunks.c.dimensions == embedding.dimensions,
            )
            .cte("eligible")
            .prefix_with("MATERIALIZED")
        )
        columns = [c for c in eligible.c if c.name != "embedding"]
        tie = (eligible.c.doc_id, eligible.c.ordinal)
        cosine = cast(
            func.round(
                cast(1 - eligible.c.embedding.cosine_distance(embedding.vector), Numeric), 6
            ),
            Float,
        )
        vector = (
            select(*columns, literal("vector").label("branch"), cosine.label("branch_score"))
            .where(
                func.vector_norm(eligible.c.embedding) > 0,
                cosine >= min_cosine,
            )
            .order_by(cosine.desc(), *tie)
            .limit(candidate_limit(k))
            .subquery("vector_hits")
        )
        branches = [select(vector)]
        wanted = sorted(set(terms(query)))
        if wanted:
            # The same explicit tokenization as the offline adapter. Runtime
            # normalization avoids silently differing email/hyphen parsers.
            normalized = func.btrim(
                func.lower(
                    func.regexp_replace(
                        eligible.c.chunk_text.collate("C"), "[^A-Za-z0-9]+", " ", "g"
                    )
                )
            )
            tokens = func.string_to_array(normalized, " ")
            density = cast(
                sum(func.cardinality(func.array_positions(tokens, term)) for term in wanted), Float
            ) / func.greatest(func.cardinality(tokens), 1)
            config = literal_column("'simple'::regconfig")
            matches = func.to_tsvector(config, normalized).op("@@")(
                func.plainto_tsquery(config, " ".join(wanted))
            )
            keyword = (
                select(*columns, literal("keyword").label("branch"), density.label("branch_score"))
                .where(matches)
                .order_by(density.desc(), *tie)
                .limit(candidate_limit(k))
                .subquery("keyword_hits")
            )
            branches.append(select(keyword))
        try:
            with self._engine.connect() as conn:
                merged = union_all(*branches).subquery("candidates")
                rows = (
                    conn.execute(
                        select(merged).order_by(
                            merged.c.branch,
                            merged.c.branch_score.desc(),
                            merged.c.doc_id,
                            merged.c.ordinal,
                        )
                    )
                    .mappings()
                    .all()
                )
        except SQLAlchemyError as exc:
            raise RetrievalUnavailable("retrieval storage unavailable") from exc
        ranked = {"vector": [], "keyword": []}
        for row in rows:
            ranked[row["branch"]].append(
                SearchHit(
                    doc_id=row["doc_id"],
                    title=row["title"],
                    url=row["url"],
                    source_id=row["source_id"],
                    text=row["chunk_text"],
                    score=0,
                    chunk=ChunkCitation(**{name: row[name] for name in ChunkCitation.model_fields}),
                )
            )
        return fuse(ranked["vector"], ranked["keyword"], k)

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
                origin=r.origin,
                source_permission_id=r.source_permission_id,
                source_principal=r.source_principal,
                source_role=r.source_role,
                source_inherited=r.source_inherited,
                source_inherited_from=list(r.source_inherited_from),
                source_expires_at=r.source_expires_at,
                expires_at=r.expires_at,
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
            source_access_synced_at=row.source_access_synced_at,
            source_access_attempted_at=row.source_access_attempted_at,
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

    def get_doc_by_google_file_id(self, file_id: str) -> Doc | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(docs)
                .where(
                    or_(
                        *(
                            func.substring(docs.c.url_normalized, p) == file_id
                            for p in GOOGLE_ID_PATTERNS
                        )
                    )
                )
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
        has_content=None,
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
        if has_content is not None:
            row_exists = (
                select(doc_content.c.doc_id).where(doc_content.c.doc_id == docs.c.id).exists()
            )
            conditions.append(row_exists if has_content else ~row_exists)
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
                    doc_grants.c.origin == "manual",
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
                                origin="manual",
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
                    doc_grants.c.origin == "manual",
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
        with self._engine.begin() as conn:
            # Serialize complete replacements even when no grant rows exist yet.
            exists = conn.execute(
                select(docs.c.id).where(docs.c.id == doc_id).with_for_update()
            ).one_or_none()
            if exists is None:
                return False
            conn.execute(
                delete(doc_grants).where(
                    doc_grants.c.doc_id == doc_id,
                    doc_grants.c.origin == origin,
                )
            )
            for grant in grants:
                conn.execute(
                    insert(doc_grants).values(
                        doc_id=doc_id,
                        grantee_type=grant.grantee_type,
                        grantee_id=grant.grantee_id,
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
                )
            conn.execute(
                update(docs)
                .where(docs.c.id == doc_id)
                .values(
                    source_access_synced_at=synced_at,
                    source_access_attempted_at=synced_at,
                    updated_at=synced_at,
                    updated_by=actor,
                )
            )
            return True

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

    def replace_doc_chunks(self, doc_id: UUID, chunks: list[DocChunk]) -> bool:
        validated = validate_doc_chunks(chunks)
        with self._engine.begin() as conn:
            # Lock the parent even when no chunks exist yet. Concurrent writers
            # serialize here before deleting/inserting, so sets cannot mix.
            existing = conn.execute(
                select(docs.c.id).where(docs.c.id == doc_id).with_for_update()
            ).scalar_one_or_none()
            if existing is None:
                return False
            self._write_chunks(conn, doc_id, validated)
        return True

    def replace_doc_chunks_if_current(
        self,
        doc_id: UUID,
        expected_content_hash: str,
        chunks: list[DocChunk],
        *,
        expected_title_hash: str,
    ) -> bool:
        validated = validate_chunks_for_content(chunks, expected_content_hash, expected_title_hash)
        with self._engine.begin() as conn:
            # Same parent lock as replace_doc_chunks, so chunk writers serialize.
            doc = conn.execute(
                select(docs.c.active, docs.c.title).where(docs.c.id == doc_id).with_for_update()
            ).one_or_none()
            if (
                doc is None
                or not doc.active
                or content_hash(doc.title or "") != expected_title_hash
            ):
                return False
            # Also lock the content row: upsert_doc_content never touches
            # `docs`, so only this lock makes it wait until the check and the
            # write commit together. A newer upsert then lands after us and
            # its own indexing trigger replaces this set.
            current_hash = conn.execute(
                select(doc_content.c.content_hash)
                .where(doc_content.c.doc_id == doc_id)
                .with_for_update()
            ).scalar_one_or_none()
            if current_hash != expected_content_hash:
                return False
            self._write_chunks(conn, doc_id, validated)
        return True

    @staticmethod
    def _write_chunks(conn, doc_id: UUID, validated: list[DocChunk]) -> None:
        conn.execute(delete(doc_chunks).where(doc_chunks.c.doc_id == doc_id))
        if validated:
            conn.execute(
                insert(doc_chunks),
                [{"doc_id": doc_id, **chunk.model_dump()} for chunk in validated],
            )

    def clear_doc_chunks_if_unindexable(self, doc_id: UUID) -> bool:
        with self._engine.begin() as conn:
            # Share the chunk writers' parent lock and recheck eligibility
            # after acquiring it. A newer successful index must survive a
            # cleanup based on an old inactive/contentless snapshot.
            active = conn.execute(
                select(docs.c.active).where(docs.c.id == doc_id).with_for_update()
            ).scalar_one_or_none()
            if active is None:
                return True
            if active:
                content_id = conn.execute(
                    select(doc_content.c.doc_id).where(doc_content.c.doc_id == doc_id)
                ).scalar_one_or_none()
                if content_id is not None:
                    return False
            conn.execute(delete(doc_chunks).where(doc_chunks.c.doc_id == doc_id))
        return True

    def delete_doc_content(self, doc_id: UUID, *, expected: DocContentMeta | None) -> bool:
        with self._engine.begin() as conn:
            # The chunk writers' parent lock: no index write can interleave.
            existing = conn.execute(
                select(docs.c.id).where(docs.c.id == doc_id).with_for_update()
            ).scalar_one_or_none()
            if existing is None:
                return False
            row = conn.execute(
                select(doc_content.c.content_hash, doc_content.c.fetched_at)
                .where(doc_content.c.doc_id == doc_id)
                .with_for_update()
            ).one_or_none()
            current = DocContentMeta(**row._mapping) if row is not None else None
            if current != expected:
                return False
            conn.execute(delete(doc_chunks).where(doc_chunks.c.doc_id == doc_id))
            conn.execute(delete(doc_content).where(doc_content.c.doc_id == doc_id))
        return True

    def get_doc_index_state(self, doc_id: UUID) -> DocIndexState | None:
        # One hash and model per set (validate_doc_chunks), so min() just
        # picks the shared value.
        stmt = select(
            func.min(doc_chunks.c.source_content_hash).label("source_content_hash"),
            func.min(doc_chunks.c.source_title_hash).label("source_title_hash"),
            func.min(doc_chunks.c.embedding_model).label("embedding_model"),
            func.count().label("chunk_count"),
        ).where(doc_chunks.c.doc_id == doc_id)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).one()
        if row.chunk_count == 0:
            return None
        return DocIndexState(**row._mapping)

    def list_docs_for_indexing(
        self, *, stale_only: bool = True, after: UUID | None = None, limit: int = 100
    ) -> list[UUID]:
        indexed = (
            select(
                doc_chunks.c.doc_id,
                func.min(doc_chunks.c.source_content_hash).label("content_hash"),
                func.min(doc_chunks.c.source_title_hash).label("title_hash"),
            )
            .group_by(doc_chunks.c.doc_id)
            .subquery()
        )
        has_content = doc_content.c.doc_id.is_not(None)
        has_chunks = indexed.c.doc_id.is_not(None)
        wanted = and_(docs.c.active.is_(True), has_content)
        if stale_only:
            wanted = and_(
                wanted,
                or_(
                    indexed.c.content_hash.is_(None),
                    indexed.c.content_hash != doc_content.c.content_hash,
                    indexed.c.title_hash.is_distinct_from(
                        func.encode(
                            func.sha256(func.convert_to(func.coalesce(docs.c.title, ""), "UTF8")),
                            "hex",
                        )
                    ),
                ),
            )
        orphaned = and_(has_chunks, or_(docs.c.active.is_(False), ~has_content))
        stmt = (
            select(docs.c.id)
            .select_from(
                docs.outerjoin(doc_content, doc_content.c.doc_id == docs.c.id).outerjoin(
                    indexed, indexed.c.doc_id == docs.c.id
                )
            )
            .where(or_(wanted, orphaned))
            .order_by(docs.c.id)
            .limit(limit)
        )
        if after is not None:
            stmt = stmt.where(docs.c.id > after)
        with self._engine.connect() as conn:
            return list(conn.execute(stmt).scalars())

    def list_doc_chunks(
        self, doc_id: UUID, *, visibility: ActorContext = SEE_ALL
    ) -> list[DocChunk]:
        stmt = (
            select(*(doc_chunks.c[name] for name in DocChunk.model_fields))
            .select_from(doc_chunks.join(docs, docs.c.id == doc_chunks.c.doc_id))
            .where(doc_chunks.c.doc_id == doc_id)
            .order_by(doc_chunks.c.ordinal)
        )
        clause = self._visibility_clause(visibility)
        if clause is not None:
            stmt = stmt.where(clause)
        with self._engine.connect() as conn:
            return [DocChunk(**row) for row in conn.execute(stmt).mappings()]

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
