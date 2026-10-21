"""Per-model chunk-set migration keeps existing chunks and guards rollback."""

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, inspect

from src.config import get_settings
from src.content import content_hash
from src.storage.postgres import PostgresStorageAdapter
from src.storage.schema import docs
from tests.chunk_storage_cases import make_chunk, make_doc

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PG_TESTS") != "1" or os.environ.get("RUN_PG_MIGRATION_TESTS") != "1",
    reason="requires disposable Postgres with both migration-test flags enabled",
)


def _primary_key(engine):
    return inspect(engine).get_pk_constraint("doc_chunks")["constrained_columns"]


def test_migration_011_keys_sets_by_model_and_refuses_lossy_downgrade():
    engine = create_engine(get_settings().database_url)
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    adapter = PostgresStorageAdapter(engine)
    doc = None
    try:
        command.downgrade(cfg, "010")
        assert _primary_key(engine) == ["doc_id", "ordinal"]
        doc = make_doc(adapter)
        chunk = make_chunk()
        adapter.upsert_doc_content(
            doc.id,
            content_text=chunk.chunk_text,
            content_hash=chunk.source_content_hash,
            fetched_at=None,
        )
        adapter.replace_doc_chunks(doc.id, [chunk])

        command.upgrade(cfg, "head")
        assert _primary_key(engine) == ["doc_id", "embedding_model", "ordinal"]
        assert adapter.list_doc_chunks(doc.id) == [chunk]
        large = chunk.model_copy(update={"embedding_model": "text-embedding-3-large"})
        assert adapter.replace_doc_chunks_if_current(
            doc.id,
            chunk.source_content_hash,
            [large],
            expected_title_hash=content_hash(doc.title),
        )

        # Two sets cannot fit the old key; the downgrade refuses rather than guess.
        with pytest.raises(RuntimeError, match="more than one embedding model"):
            command.downgrade(cfg, "010")
        assert _primary_key(engine) == ["doc_id", "embedding_model", "ordinal"]

        adapter.delete_doc_chunks_except(doc.id, [chunk.embedding_model])
        command.downgrade(cfg, "010")
        assert _primary_key(engine) == ["doc_id", "ordinal"]
        assert adapter.list_doc_chunks(doc.id) == [chunk]
    finally:
        command.upgrade(cfg, "head")
        if doc is not None:
            with engine.begin() as conn:
                conn.execute(delete(docs).where(docs.c.id == doc.id))
        engine.dispose()
