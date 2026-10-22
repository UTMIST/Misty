"""Identity fences backfill removal tombstones without changing retained data."""

import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, inspect

from src.config import get_settings
from src.meeting_records import MeetingRecords
from src.storage.postgres import PostgresStorageAdapter
from src.storage.schema import docs, meeting_records
from tests.chunk_storage_cases import make_doc
from tests.fake_embedder import FakeEmbedder
from tests.meeting_record_cases import Directory, Writer, payload

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PG_TESTS") != "1" or os.environ.get("RUN_PG_MIGRATION_TESTS") != "1",
    reason="requires disposable Postgres and both migration opt-ins",
)


def test_removed_identities_round_trip_preserves_pending_meetings_and_catalog():
    engine = create_engine(get_settings().database_url)
    cfg = Config("alembic.ini")
    adapter = PostgresStorageAdapter(engine)
    unrelated = make_doc(adapter)
    ids = [uuid4(), uuid4()]
    catalog_id = None
    try:
        records = MeetingRecords(
            adapter,
            Writer(prefix=f"migration_{uuid4()}_"),
            Directory(),
            get_settings(),
            embedder=FakeEmbedder(),
        )
        pending, _ = records.accept(ids[0], payload(), actor="meeting-key")
        records.accept(ids[1], payload(), actor="meeting-key")
        records.advance(ids[1])
        removed = records.remove(ids[1], actor="admin")
        catalog_id = removed.doc_id
        assert removed.removed_google_document_ids == [removed.google_document_id]
        for _ in range(2):
            command.downgrade(cfg, "014")
            columns = {column["name"] for column in inspect(engine).get_columns("meeting_records")}
            assert "removed_google_document_ids" not in columns
            command.upgrade(cfg, "head")
            restored = PostgresStorageAdapter(engine).get_meeting(ids[1])
            assert restored.removed_google_document_ids == [removed.google_document_id]
            assert restored.next_reconcile_at is not None
            fields = {"next_reconcile_at", "google_deletions"}
            assert restored.model_dump(exclude=fields) == removed.model_dump(exclude=fields)
            assert adapter.get_meeting(ids[0]) == pending
            assert adapter.get_doc(unrelated.id) == unrelated
            assert adapter.get_doc(catalog_id).purged_at
            assert not adapter.get_doc_content(catalog_id)
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(delete(meeting_records).where(meeting_records.c.meeting_id.in_(ids)))
            conn.execute(delete(docs).where(docs.c.id.in_([unrelated.id, catalog_id])))
        engine.dispose()
