"""Title provenance migration preserves existing chunks and supports rollback."""

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, delete, inspect, insert, select

from src.config import get_settings
from src.content import content_hash
from src.storage.postgres import PostgresStorageAdapter
from src.storage.schema import docs
from tests.chunk_storage_cases import make_chunk, make_doc
from tests.fake_embedder import FAKE_MODEL

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PG_TESTS") != "1" or os.environ.get("RUN_PG_MIGRATION_TESTS") != "1",
    reason="requires disposable Postgres with both migration-test flags enabled",
)


def test_migration_010_preserves_chunks_and_marks_unknown_title_stale():
    engine = create_engine(get_settings().database_url)
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    adapter = PostgresStorageAdapter(engine)
    doc = None
    try:
        doc = make_doc(adapter)
        chunk = make_chunk()
        adapter.upsert_doc_content(
            doc.id,
            content_text=chunk.chunk_text,
            content_hash=chunk.source_content_hash,
            fetched_at=None,
        )
        command.downgrade(cfg, "009")
        old_table = Table("doc_chunks", MetaData(), autoload_with=engine)
        values = chunk.model_dump(exclude={"source_title_hash"})
        with engine.begin() as conn:
            conn.execute(insert(old_table).values(doc_id=doc.id, **values))
            original = conn.execute(select(old_table).where(old_table.c.doc_id == doc.id)).one()

        for _ in range(2):
            command.upgrade(cfg, "head")
            assert adapter.get_doc_content(doc.id) == chunk.chunk_text
            assert adapter.list_doc_chunks(doc.id) == [
                chunk.model_copy(update={"source_title_hash": None})
            ]
            assert doc.id in adapter.list_docs_for_indexing(models=[FAKE_MODEL])
            assert (
                adapter.replace_doc_chunks_if_current(
                    doc.id,
                    chunk.source_content_hash,
                    [chunk],
                    expected_title_hash=content_hash(doc.title),
                )
                is True
            )
            assert doc.id not in adapter.list_docs_for_indexing(models=[FAKE_MODEL])

            command.downgrade(cfg, "009")
            assert "source_title_hash" not in {
                c["name"] for c in inspect(engine).get_columns("doc_chunks")
            }
            with engine.connect() as conn:
                restored = conn.execute(select(old_table).where(old_table.c.doc_id == doc.id)).one()
            # The migration changes only provenance metadata; vectors and all
            # pre-existing columns survive upgrade/downgrade unchanged.
            for name, value in original._mapping.items():
                if name == "embedding":
                    assert list(restored._mapping[name]) == list(value)
                else:
                    assert restored._mapping[name] == value
    finally:
        command.upgrade(cfg, "head")
        if doc is not None:
            with engine.begin() as conn:
                conn.execute(delete(docs).where(docs.c.id == doc.id))
        engine.dispose()
