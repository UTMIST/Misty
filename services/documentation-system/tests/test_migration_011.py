"""The runner guard survives adapter restarts; its schema change is reversible."""

import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, inspect, select

from src.config import get_settings
from src.storage.postgres import PostgresStorageAdapter
from src.storage.schema import docs, source_refresh_run
from tests.chunk_storage_cases import make_doc

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PG_TESTS") != "1" or os.environ.get("RUN_PG_MIGRATION_TESTS") != "1",
    reason="requires disposable Postgres with both migration-test flags enabled",
)


def test_migration_011_round_trip_preserves_catalog():
    engine = create_engine(get_settings().database_url)
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    adapter = PostgresStorageAdapter(engine)
    doc = None
    try:
        doc = make_doc(adapter)
        command.downgrade(cfg, "010")
        for _ in range(2):
            command.upgrade(cfg, "head")
            owner = uuid4()
            assert adapter.acquire_source_refresh_run(owner, actor="scheduler")
            assert not PostgresStorageAdapter(engine).acquire_source_refresh_run(
                uuid4(), actor="duplicate"
            )
            with engine.connect() as conn:
                row = conn.execute(select(source_refresh_run)).one()
                assert row.run_id == owner and row.created_by == "scheduler"
                assert row.started_at is not None
            assert adapter.get_doc(doc.id) == doc
            command.downgrade(cfg, "010")
            assert "source_refresh_run" not in inspect(engine).get_table_names()
            with engine.connect() as conn:
                assert (
                    conn.execute(select(docs.c.id).where(docs.c.id == doc.id)).scalar_one()
                    == doc.id
                )
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(delete(source_refresh_run))
            if doc is not None:
                conn.execute(delete(docs).where(docs.c.id == doc.id))
        engine.dispose()
