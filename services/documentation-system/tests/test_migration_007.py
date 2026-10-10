"""Real pgvector schema, database constraints, and reversible upgrade coverage."""

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, inspect, insert, select, text
from sqlalchemy.exc import DBAPIError

from src.config import get_settings
from src.storage.postgres import PostgresStorageAdapter
from src.storage.schema import doc_chunks, docs
from tests.chunk_storage_cases import make_chunk, make_doc

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PG_TESTS") != "1", reason="set RUN_PG_TESTS=1 to run Postgres tests"
)


@pytest.fixture
def engine():
    engine = create_engine(get_settings().database_url)
    yield engine
    engine.dispose()


def _alembic_cfg():
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    return cfg


def test_migration_007_round_trip_preserves_existing_catalog_and_content(engine):
    cfg = _alembic_cfg()
    adapter = PostgresStorageAdapter(engine)
    doc = None
    try:
        command.downgrade(cfg, "006")
        doc = make_doc(adapter)
        chunk = make_chunk()
        adapter.upsert_doc_content(
            doc.id,
            content_text=chunk.chunk_text,
            content_hash=chunk.source_content_hash,
            fetched_at=None,
        )
        # A provider may have already enabled vector. Upgrade must accept it.
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION vector"))
        command.upgrade(cfg, "head")
        assert adapter.get_doc_content(doc.id) == chunk.chunk_text
        adapter.replace_doc_chunks(doc.id, [chunk])
        assert adapter.list_doc_chunks(doc.id) == [chunk]

        command.downgrade(cfg, "006")
        assert "doc_chunks" not in inspect(engine).get_table_names()
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
                ).scalar_one_or_none()
                is None
            )
        assert adapter.get_doc_content(doc.id) == chunk.chunk_text

        command.upgrade(cfg, "head")
        assert adapter.list_doc_chunks(doc.id) == []
        assert adapter.get_doc(doc.id) is not None
    finally:
        command.upgrade(cfg, "head")
        if doc is not None:
            with engine.begin() as conn:
                conn.execute(delete(docs).where(docs.c.id == doc.id))


def test_migration_007_vector_keyword_storage_and_cascade(engine):
    adapter = PostgresStorageAdapter(engine)
    doc = make_doc(adapter)
    try:
        chunk = make_chunk(text="CPSIF grants for UTMIST 😀")
        adapter.replace_doc_chunks(doc.id, [chunk])
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT vector_dims(embedding) AS width, "
                    "embedding <=> embedding AS distance, "
                    "search_vector @@ plainto_tsquery('simple', 'CPSIF UTMIST') AS matches "
                    "FROM doc_chunks WHERE doc_id = :id"
                ),
                {"id": doc.id},
            ).one()
            assert row.width == 1536
            assert row.distance == pytest.approx(0, abs=1e-6)
            assert row.matches is True
            indexes = (
                conn.execute(text("SELECT indexdef FROM pg_indexes WHERE tablename = 'doc_chunks'"))
                .scalars()
                .all()
            )
            assert any("USING gin (search_vector)" in definition for definition in indexes)
            assert not any("USING hnsw" in d or "USING ivfflat" in d for d in indexes)

        # Generated keyword metadata must follow the current chunk text.
        adapter.replace_doc_chunks(doc.id, [make_chunk(text="EigenAI")])
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text(
                        "SELECT search_vector @@ plainto_tsquery('simple', 'CPSIF') "
                        "FROM doc_chunks WHERE doc_id = :id"
                    ),
                    {"id": doc.id},
                ).scalar_one()
                is False
            )
    finally:
        with engine.begin() as conn:
            conn.execute(delete(docs).where(docs.c.id == doc.id))
    with engine.connect() as conn:
        assert conn.execute(select(doc_chunks).where(doc_chunks.c.doc_id == doc.id)).first() is None


@pytest.mark.parametrize(
    "changes",
    [
        {"ordinal": -1},
        {"end_offset": 1},
        {"dimensions": 3072},
        {"embedding_model": " "},
        {"source_content_hash": "invalid"},
    ],
)
def test_migration_007_constraints_backstop_raw_inserts(engine, changes):
    doc = make_doc(PostgresStorageAdapter(engine))
    try:
        with pytest.raises(DBAPIError):
            with engine.begin() as conn:
                conn.execute(
                    insert(doc_chunks).values(
                        doc_id=doc.id, **{**make_chunk().model_dump(), **changes}
                    )
                )
    finally:
        with engine.begin() as conn:
            conn.execute(delete(docs).where(docs.c.id == doc.id))


def test_migration_007_vector_width_and_document_fk_are_enforced(engine):
    doc = make_doc(PostgresStorageAdapter(engine))
    values = {"doc_id": doc.id, **make_chunk().model_dump()}
    try:
        # Bypass the Python VECTOR bind validator to test the actual SQL type.
        with pytest.raises(DBAPIError):
            with engine.begin() as conn:
                conn.execute(
                    insert(doc_chunks).values(**{**values, "embedding": text("'[1,2]'::vector")})
                )
        with engine.begin() as conn:
            conn.execute(delete(docs).where(docs.c.id == doc.id))
        with pytest.raises(DBAPIError):
            with engine.begin() as conn:
                conn.execute(insert(doc_chunks).values(**values))
    finally:
        with engine.begin() as conn:
            conn.execute(delete(docs).where(docs.c.id == doc.id))
