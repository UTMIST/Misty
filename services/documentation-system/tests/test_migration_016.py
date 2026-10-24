"""Deletion intents survive storage reloads; rollback preserves other meeting data."""

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
from tests.meeting_record_cases import Directory, Writer, payload

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PG_TESTS") != "1" or os.environ.get("RUN_PG_MIGRATION_TESTS") != "1",
    reason="requires disposable Postgres and both migration opt-ins",
)


def test_deletion_intents_round_trip_preserves_payload_and_unrelated_catalog():
    engine = create_engine(get_settings().database_url)
    cfg = Config("alembic.ini")
    adapter = PostgresStorageAdapter(engine)
    unrelated = make_doc(adapter)
    meeting_id = uuid4()
    try:
        records = MeetingRecords(adapter, Writer(), Directory(), get_settings())
        pending, _ = records.accept(meeting_id, payload(), actor="meeting-key")
        progress = adapter.change_meeting(
            pending,
            {"google_deletions": {"late": "pending", "duplicate": "confirmed"}},
            actor="test",
        )
        assert PostgresStorageAdapter(engine).get_meeting(meeting_id) == progress
        command.downgrade(cfg, "015")
        assert "google_deletions" not in {
            column["name"] for column in inspect(engine).get_columns("meeting_records")
        }
        command.upgrade(cfg, "head")
        restored = adapter.get_meeting(meeting_id)
        assert restored.google_deletions == {}
        assert restored.model_dump(exclude={"google_deletions"}) == progress.model_dump(
            exclude={"google_deletions"}
        )
        assert restored.pending_payload == pending.pending_payload
        assert adapter.get_doc(unrelated.id) == unrelated
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(delete(meeting_records).where(meeting_records.c.meeting_id == meeting_id))
            conn.execute(delete(docs).where(docs.c.id == unrelated.id))
        engine.dispose()
