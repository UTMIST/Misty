"""Postgres-backed round-trip coverage for source document access migration 007."""

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from src.config import get_settings

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PG_TESTS") != "1", reason="set RUN_PG_TESTS=1 to run Postgres tests"
)


def _alembic_cfg() -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    return cfg


def test_migration_007_round_trip():
    engine = create_engine(get_settings().database_url, future=True)
    cfg = _alembic_cfg()
    try:
        command.upgrade(cfg, "head")
        inspector = inspect(engine)
        doc_columns = {column["name"] for column in inspector.get_columns("docs")}
        grant_columns = {column["name"] for column in inspector.get_columns("doc_grants")}
        grant_indexes = {index["name"] for index in inspector.get_indexes("doc_grants")}
        assert "source_access_synced_at" in doc_columns
        assert "source_access_attempted_at" in doc_columns
        assert {
            "origin",
            "source_permission_id",
            "source_principal",
            "source_role",
            "source_inherited",
            "source_expires_at",
            "source_inherited_from",
            "expires_at",
        } <= grant_columns
        assert "uq_doc_grants_source_permission" in grant_indexes

        command.downgrade(cfg, "006")
        inspector = inspect(engine)
        assert "source_access_synced_at" not in {
            column["name"] for column in inspector.get_columns("docs")
        }
        assert "source_access_attempted_at" not in {
            column["name"] for column in inspector.get_columns("docs")
        }
        assert "origin" not in {column["name"] for column in inspector.get_columns("doc_grants")}

        command.upgrade(cfg, "head")
        assert "origin" in {column["name"] for column in inspect(engine).get_columns("doc_grants")}
    finally:
        command.upgrade(cfg, "head")
