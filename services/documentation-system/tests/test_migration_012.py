"""Source URL backfill changes display links only and restores exact originals."""

import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, insert, inspect, select, update

from src.config import get_settings
from src.storage.schema import doc_source_url_backfill, doc_tags, docs

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PG_TESTS") != "1" or os.environ.get("RUN_PG_MIGRATION_TESTS") != "1",
    reason="requires disposable Postgres with both migration-test flags enabled",
)


def test_migration_012_round_trip_preserves_identity_and_unrelated_data():
    engine = create_engine(get_settings().database_url)
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    rows, expected = [], {}

    def add(source, url, normalized, replacement=None, active=True):
        doc_id = uuid4()
        rows.append(
            dict(
                id=doc_id,
                source_id=source,
                url=url,
                url_normalized=normalized,
                title="Preserved title",
                active=active,
                created_by="migration-test",
                updated_by="migration-test",
            )
        )
        expected[doc_id] = replacement or url
        return doc_id

    try:
        command.downgrade(cfg, "011")
        for source, path in (
            ("gdocs", "document"),
            ("gsheets", "spreadsheets"),
            ("gslides", "presentation"),
            ("gdrive", None),
        ):
            for suffix, active in (("", True), ("/view?usp=sharing#section", False)):
                file_id = uuid4().hex
                normalized = f"https://drive.google.com/file/d/{file_id}"
                preferred = (
                    f"https://docs.google.com/{path}/d/{file_id}/edit"
                    if path
                    else normalized + "/view"
                )
                add(source, normalized + suffix, normalized, preferred, active)

        # Editor/manual/non-Google links and unrecognizable or conflicting keys stay intact.
        for source, url, normalized in (
            (
                "gdocs",
                "https://docs.google.com/document/d/manual/edit?usp=sharing",
                "https://drive.google.com/file/d/manual",
            ),
            ("web", "https://drive.google.com/file/d/web", "https://drive.google.com/file/d/web"),
            (
                "gdocs",
                "https://drive.google.com.evil.test/file/d/evil",
                "https://drive.google.com/file/d/evil",
            ),
            (
                "gdocs",
                "https://drive.google.com/file/d/wrong",
                "https://drive.google.com/file/d/other",
            ),
            ("gsheets", "https://drive.google.com/file/d/unknown", "https://example.test/unknown"),
            (
                "gdrive",
                "https://docs.google.com/forms/d/form/edit",
                "https://drive.google.com/file/d/form",
            ),
        ):
            add(source, url, normalized)

        with engine.begin() as conn:
            conn.execute(insert(docs), rows)
            conn.execute(insert(doc_tags).values(doc_id=rows[0]["id"], tag="preserved"))
            original = {r.id: dict(r._mapping) for r in conn.execute(select(docs))}
            tags = conn.execute(select(doc_tags)).all()

        for _ in range(2):
            command.upgrade(cfg, "head")
            with engine.connect() as conn:
                upgraded = {r.id: dict(r._mapping) for r in conn.execute(select(docs))}
                assert upgraded == {
                    doc_id: {**row, "url": expected.get(doc_id, row["url"])}
                    for doc_id, row in original.items()
                }
                assert conn.execute(select(doc_tags)).all() == tags
            command.downgrade(cfg, "011")
            assert "doc_source_url_backfill" not in inspect(engine).get_table_names()
            with engine.connect() as conn:
                assert {r.id: dict(r._mapping) for r in conn.execute(select(docs))} == original
                assert conn.execute(select(doc_tags)).all() == tags

        command.upgrade(cfg, "head")
        edited_id, deleted_id = rows[0]["id"], rows[1]["id"]
        new_id = add(
            "gdocs",
            "https://docs.google.com/document/d/new/edit",
            "https://drive.google.com/file/d/new",
        )
        with engine.begin() as conn:
            conn.execute(
                update(docs).where(docs.c.id == edited_id).values(url="https://example.test/edited")
            )
            conn.execute(delete(docs).where(docs.c.id == deleted_id))
            assert (
                conn.execute(
                    select(doc_source_url_backfill).where(
                        doc_source_url_backfill.c.doc_id == deleted_id
                    )
                ).first()
                is None
            )
            conn.execute(insert(docs).values(**rows[-1]))
        command.downgrade(cfg, "011")
        with engine.connect() as conn:
            assert (
                conn.execute(select(docs.c.url).where(docs.c.id == edited_id)).scalar_one()
                == "https://example.test/edited"
            )
            assert (
                conn.execute(select(docs.c.url).where(docs.c.id == new_id)).scalar_one()
                == rows[-1]["url"]
            )
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(delete(docs).where(docs.c.id.in_([row["id"] for row in rows])))
        engine.dispose()
