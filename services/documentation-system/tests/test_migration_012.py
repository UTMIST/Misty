"""Meeting schema and unrelated catalog data survive upgrade/downgrade/upgrade."""

import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, inspect, select

from src.config import get_settings
from src.storage.postgres import PostgresStorageAdapter
from src.storage.schema import docs, meeting_records
from tests.chunk_storage_cases import make_doc
from tests.meeting_record_cases import Directory, Writer, payload
from src.meeting_records import MeetingRecords

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PG_TESTS") != "1" or os.environ.get("RUN_PG_MIGRATION_TESTS") != "1",
    reason="requires disposable Postgres and both migration opt-ins",
)


def test_meeting_schema_round_trip_and_restart():
    engine = create_engine(get_settings().database_url)
    cfg = Config("alembic.ini")
    adapter = PostgresStorageAdapter(engine)
    doc = make_doc(adapter)
    try:
        records = MeetingRecords(adapter, Writer(), Directory(), get_settings())
        r, _ = records.accept(uuid4(), payload(), actor="meeting-key")
        assert PostgresStorageAdapter(engine).get_meeting(r.meeting_id) == r
        command.downgrade(cfg, "-1")
        assert "meeting_records" not in inspect(engine).get_table_names()
        with engine.connect() as conn:
            assert conn.execute(select(docs.c.id).where(docs.c.id == doc.id)).scalar_one() == doc.id
        command.upgrade(cfg, "head")
        assert adapter.get_doc(doc.id) == doc
        assert adapter.get_meeting(r.meeting_id) is None
        assert "purged_at" in {column["name"] for column in inspect(engine).get_columns("docs")}
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(delete(meeting_records))
            conn.execute(delete(docs).where(docs.c.id == doc.id))
        engine.dispose()
