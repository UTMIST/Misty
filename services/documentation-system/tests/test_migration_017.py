"""Per-model chunk-set migration keeps existing chunks and guards rollback."""

import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, inspect, text

from src.config import get_settings
from src.content import content_hash
from src.storage.postgres import PostgresStorageAdapter
from src.storage.schema import docs, meeting_records
from src.meeting_records import MeetingRecords
from tests.fake_embedder import FakeEmbedder
from tests.meeting_record_cases import Directory, Writer, payload
from tests.chunk_storage_cases import make_chunk, make_doc

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PG_TESTS") != "1" or os.environ.get("RUN_PG_MIGRATION_TESTS") != "1",
    reason="requires disposable Postgres with both migration-test flags enabled",
)


def _primary_key(engine):
    return inspect(engine).get_pk_constraint("doc_chunks")["constrained_columns"]


def _snapshot(engine):
    # Include every staging table affected by recovery or document purging, so
    # widening the chunk key cannot silently reset adjacent lifecycle state.
    with engine.connect() as conn:
        return {
            table: sorted(
                conn.execute(text(f"SELECT row_to_json(t)::text FROM {table} t")).scalars()
            )
            for table in (
                "docs",
                "doc_content",
                "doc_chunks",
                "doc_grants",
                "doc_tags",
                "meeting_records",
                "source_refresh_run",
            )
        }


def test_migration_017_keys_sets_by_model_and_refuses_lossy_downgrade():
    engine = create_engine(get_settings().database_url)
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    adapter = PostgresStorageAdapter(engine)
    doc = None
    meeting_id = uuid4()
    owner = uuid4()
    meeting_doc_id = None
    try:
        command.downgrade(cfg, "016")
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
        adapter.add_tag(doc.id, "migration-preservation")
        adapter.add_grant(doc.id, grantee_type="org", grantee_id=None, actor="migration-test")
        assert adapter.acquire_source_refresh_run(owner, actor="migration-test")
        records = MeetingRecords(
            adapter,
            Writer(prefix=f"migration-{meeting_id}-"),
            Directory(),
            get_settings(),
            embedder=FakeEmbedder(),
        )
        records.accept(meeting_id, payload(), actor="meeting-key")
        records.advance(meeting_id)
        saved = adapter.get_meeting(meeting_id)
        assert saved.persistence_status == "saved" and not saved.index_pending
        meeting_doc_id = saved.doc_id
        adapter.add_tag(meeting_doc_id, "saved-meeting")
        adapter.change_meeting(
            saved,
            {
                "google_deletions": {"late": "pending", "duplicate": "confirmed"},
                "removed_google_document_ids": ["removed-alias"],
            },
            actor="migration-test",
        )
        baseline = _snapshot(engine)

        command.upgrade(cfg, "head")
        assert _primary_key(engine) == ["doc_id", "embedding_model", "ordinal"]
        assert adapter.list_doc_chunks(doc.id) == [chunk]
        assert _snapshot(engine) == baseline
        large = chunk.model_copy(update={"embedding_model": "text-embedding-3-large"})
        assert adapter.replace_doc_chunks_if_current(
            doc.id,
            chunk.source_content_hash,
            [large],
            expected_title_hash=content_hash(doc.title),
        )

        mixed = _snapshot(engine)
        # Two sets cannot fit the old key; the downgrade refuses rather than guess.
        with pytest.raises(RuntimeError, match="more than one embedding model"):
            command.downgrade(cfg, "016")
        assert _primary_key(engine) == ["doc_id", "embedding_model", "ordinal"]

        assert _snapshot(engine) == mixed
        adapter.delete_doc_chunks_except(doc.id, [chunk.embedding_model])
        command.downgrade(cfg, "016")
        assert _primary_key(engine) == ["doc_id", "ordinal"]
        assert adapter.list_doc_chunks(doc.id) == [chunk]
        assert _snapshot(engine) == baseline
        command.upgrade(cfg, "head")
        assert _primary_key(engine) == ["doc_id", "embedding_model", "ordinal"]
        assert _snapshot(engine) == baseline
    finally:
        command.upgrade(cfg, "head")
        adapter.release_source_refresh_run(owner)
        with engine.begin() as conn:
            conn.execute(delete(meeting_records).where(meeting_records.c.meeting_id == meeting_id))
            if meeting_doc_id is not None:
                conn.execute(delete(docs).where(docs.c.id == meeting_doc_id))
        if doc is not None:
            with engine.begin() as conn:
                conn.execute(delete(docs).where(docs.c.id == doc.id))
        engine.dispose()
