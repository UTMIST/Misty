"""Postgres-backed test for migration 005 (doc_content).

Verifies the table is created with the expected shape, that deleting a doc
cascades away its content row, and that the migration is reversible. Always
restores the DB to head.
"""

import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from src.config import get_settings

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PG_TESTS") != "1", reason="set RUN_PG_TESTS=1 to run Postgres tests"
)


def _alembic_cfg() -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    return cfg


def test_migration_005_creates_doc_content_and_cascades():
    engine = create_engine(get_settings().database_url, future=True)
    cfg = _alembic_cfg()
    try:
        command.upgrade(cfg, "head")
        assert "doc_content" in inspect(engine).get_table_names()

        doc_id = uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO docs (id, url, url_normalized, source_id, title, "
                    "created_by, updated_by) VALUES (:id, :u, :u, 'web', 't', 'test', 'test')"
                ),
                {"id": doc_id, "u": f"https://example.com/{doc_id}"},
            )
            conn.execute(
                text(
                    "INSERT INTO doc_content (doc_id, content_text, content_hash) "
                    "VALUES (:id, 'hello', 'abc')"
                ),
                {"id": doc_id},
            )

        with engine.begin() as conn:
            conn.execute(text("DELETE FROM docs WHERE id = :id"), {"id": doc_id})
            remaining = conn.execute(
                text("SELECT count(*) FROM doc_content WHERE doc_id = :id"), {"id": doc_id}
            ).scalar_one()
        assert remaining == 0, "doc_content row should cascade when its doc is deleted"
    finally:
        command.upgrade(cfg, "head")


def test_migration_005_downgrade_drops_table():
    engine = create_engine(get_settings().database_url, future=True)
    cfg = _alembic_cfg()
    try:
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "004")
        assert "doc_content" not in inspect(engine).get_table_names()
    finally:
        command.upgrade(cfg, "head")
