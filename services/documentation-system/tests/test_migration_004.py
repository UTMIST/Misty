"""Postgres-backed test for migration 004's backfill + partial unique index.

Verifies the CodeRabbit-flagged ordering: the migration must canonicalize
existing duplicate active rows BEFORE creating the partial unique index, or the
index creation would fail on live data.

Downgrades to rev 003, plants two active rows sharing a url_normalized, then
upgrades to head and asserts exactly one stays active (the earliest created_at)
and the unique index exists. Always restores the DB to head.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from src.config import get_settings

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PG_TESTS") != "1", reason="set RUN_PG_TESTS=1 to run Postgres tests"
)


def _alembic_cfg() -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    return cfg


def test_migration_004_backfills_duplicates_then_indexes():
    engine = create_engine(get_settings().database_url, future=True)
    cfg = _alembic_cfg()
    try:
        # Reset to before the unique index so we can plant duplicates.
        command.downgrade(cfg, "003")
        with engine.begin() as conn:
            conn.execute(
                text("TRUNCATE doc_grants, doc_tags, docs, api_keys RESTART IDENTITY CASCADE")
            )
            older = datetime(2026, 1, 1, tzinfo=timezone.utc)
            newer = older + timedelta(days=1)
            # Two ACTIVE rows for the same URL — impossible post-index, the
            # backfill must resolve them.
            keep = conn.execute(
                text(
                    "INSERT INTO docs (url, url_normalized, source_id, active, created_at, "
                    "updated_at, created_by, updated_by) VALUES "
                    "(:u, :n, 'web', true, :c, :c, 'seed', 'seed') RETURNING id"
                ),
                {"u": "https://mig.com", "n": "https://mig.com", "c": older},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO docs (url, url_normalized, source_id, active, created_at, "
                    "updated_at, created_by, updated_by) VALUES "
                    "(:u, :n, 'web', true, :c, :c, 'seed', 'seed')"
                ),
                {"u": "https://mig.com", "n": "https://mig.com", "c": newer},
            )

        # This must succeed: backfill runs before index creation.
        command.upgrade(cfg, "head")

        with engine.connect() as conn:
            active_ids = (
                conn.execute(
                    text("SELECT id FROM docs WHERE url_normalized = :n AND active"),
                    {"n": "https://mig.com"},
                )
                .scalars()
                .all()
            )
            assert active_ids == [keep]  # exactly one, and it's the earliest

            index_exists = conn.execute(
                text("SELECT 1 FROM pg_indexes WHERE indexname = 'uq_docs_url_normalized_active'")
            ).scalar_one_or_none()
            assert index_exists == 1

            # And the index truly enforces uniqueness on active rows now.
            with pytest.raises(Exception):
                with engine.begin() as w:
                    w.execute(
                        text(
                            "INSERT INTO docs (url, url_normalized, source_id, active, "
                            "created_by, updated_by) VALUES "
                            "(:u, :n, 'web', true, 'seed', 'seed')"
                        ),
                        {"u": "https://mig.com", "n": "https://mig.com"},
                    )
    finally:
        # Leave the DB at head for the rest of the suite regardless of outcome.
        with engine.begin() as conn:
            conn.execute(
                text("TRUNCATE doc_grants, doc_tags, docs, api_keys RESTART IDENTITY CASCADE")
            )
        command.upgrade(cfg, "head")
