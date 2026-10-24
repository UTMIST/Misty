"""Initial indexing work round-trips without deleting existing records or text."""

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


def test_meeting_index_round_trip_preserves_records_and_catalog():
    engine = create_engine(get_settings().database_url)
    cfg = Config("alembic.ini")
    adapter = PostgresStorageAdapter(engine)
    unrelated = make_doc(adapter)
    meeting = None
    try:
        records = MeetingRecords(
            adapter,
            Writer(prefix=f"migration_{uuid4()}_"),
            Directory(),
            get_settings(),
            embedder=FakeEmbedder(),
        )
        meeting, _ = records.accept(uuid4(), payload(), actor="meeting-key")
        records.advance(meeting.meeting_id)
        saved = adapter.get_meeting(meeting.meeting_id)
        assert saved.persistence_status == "saved" and not saved.index_pending
        doc = adapter.get_doc(saved.doc_id)
        content = adapter.get_doc_content(saved.doc_id)
        for _ in range(2):
            command.downgrade(cfg, "012")
            columns = {column["name"] for column in inspect(engine).get_columns("meeting_records")}
            assert "index_pending" not in columns
            command.upgrade(cfg, "head")
            recovered = PostgresStorageAdapter(engine).get_meeting(meeting.meeting_id)
            assert recovered.index_pending
            assert recovered.model_dump(exclude={"index_pending"}) == saved.model_dump(
                exclude={"index_pending"}
            )
            assert adapter.get_doc(saved.doc_id) == doc
            assert adapter.get_doc_content(saved.doc_id) == content
            assert adapter.get_doc(unrelated.id) == unrelated
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            ids = [unrelated.id]
            if meeting is not None:
                current = adapter.get_meeting(meeting.meeting_id)
                if current and current.doc_id:
                    ids.append(current.doc_id)
                conn.execute(
                    delete(meeting_records).where(
                        meeting_records.c.meeting_id == meeting.meeting_id
                    )
                )
            conn.execute(delete(docs).where(docs.c.id.in_(ids)))
        engine.dispose()
