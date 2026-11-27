"""Forwarded-email submission tables and unrelated catalog data survive migration 019 round-trip."""

from datetime import datetime, timezone
import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, inspect, select

from contracts.email import EmailSubmission
from src.config import get_settings
from src.storage.postgres import PostgresStorageAdapter
from src.storage.schema import (
    docs,
    email_inbox_state,
    email_outbound_sends,
    email_poll_run,
    email_processed_messages,
    email_submissions,
)
from tests.chunk_storage_cases import make_doc

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PG_TESTS") != "1" or os.environ.get("RUN_PG_MIGRATION_TESTS") != "1",
    reason="requires disposable Postgres and both migration opt-ins",
)


def test_migration_019_round_trip_preserves_catalog():
    engine = create_engine(get_settings().database_url)
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    adapter = PostgresStorageAdapter(engine)
    doc = make_doc(adapter)
    now = datetime(2026, 11, 27, 4, 0, tzinfo=timezone.utc)
    try:
        command.downgrade(cfg, "018")
        for tbl in (
            "email_poll_run",
            "email_inbox_state",
            "email_submissions",
            "email_processed_messages",
            "email_outbound_sends",
        ):
            assert tbl not in inspect(engine).get_table_names()

        command.upgrade(cfg, "head")
        sub = EmailSubmission(
            submission_id=uuid4(),
            mailbox_identity="misty@utmist.ca",
            gmail_message_id="msg_rt",
            gmail_thread_id="thr_rt",
            outer_message_id="<rt@utmist.ca>",
            submitter_person_id=uuid4(),
            submitter_label="Tester",
            verified_sender_email="tester@utmist.ca",
            auth_mechanism="dmarc_pass",
            received_at=now,
            subject="Fwd: Round trip",
            created_at=now,
            updated_at=now,
            created_by="test",
            updated_by="test",
        )
        stored, created = adapter.put_email_submission(sub)
        assert created is True
        assert PostgresStorageAdapter(engine).get_email_submission(stored.submission_id) == stored
        assert adapter.get_doc(doc.id) == doc

        command.downgrade(cfg, "018")
        assert "email_submissions" not in inspect(engine).get_table_names()
        with engine.connect() as conn:
            assert conn.execute(select(docs.c.id).where(docs.c.id == doc.id)).scalar_one() == doc.id
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(delete(email_outbound_sends))
            conn.execute(delete(email_processed_messages))
            conn.execute(delete(email_submissions))
            conn.execute(delete(email_inbox_state))
            conn.execute(delete(email_poll_run))
            conn.execute(delete(docs).where(docs.c.id == doc.id))
        engine.dispose()
