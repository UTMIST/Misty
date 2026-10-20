"""Exercise registry cleanup against rows with enforced foreign keys."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

from src.url_norm import derive_source
from tests.conftest import build_seed_sources


def _load_migration(filename):
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / filename
    spec = spec_from_file_location(filename.removesuffix(".py"), path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration("009_remove_retired_source.py")
seed = _load_migration("002_seed_sources.py")


def test_cleanup_preserves_documents_and_retires_url_patterns(monkeypatch):
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.connection.create_function("now", 0, lambda: "2026-10-18")
        conn.execute(text("PRAGMA foreign_keys = ON"))
        conn.execute(text("CREATE TABLE sources (id TEXT PRIMARY KEY)"))
        conn.execute(
            text(
                "CREATE TABLE docs (id TEXT PRIMARY KEY, source_id TEXT REFERENCES sources(id), "
                "url TEXT, title TEXT, active BOOLEAN, updated_at TEXT, updated_by TEXT)"
            )
        )
        conn.execute(
            text("INSERT INTO sources VALUES (:id)"),
            [{"id": sid} for sid in [migration.RETIRED_SOURCE_ID, "web", "youtube"]],
        )
        retired = next(s for s in seed.SOURCES if s[0] == migration.RETIRED_SOURCE_ID)
        urls = [f"https://{host}/page" for host in retired[2]]
        for i, url in enumerate(urls):
            conn.execute(
                text(
                    "INSERT INTO docs VALUES (:id, :source, :url, 'preserved', :active, 'old', 'actor')"
                ),
                dict(id=str(i), source=retired[0], url=url, active=bool(i)),
            )
        conn.execute(
            text("INSERT INTO docs VALUES ('other', 'youtube', 'url', 'other', 1, 'old', 'actor')")
        )
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
        migration.upgrade()
        assert conn.execute(text("SELECT id FROM sources ORDER BY id")).scalars().all() == [
            "web",
            "youtube",
        ]
        rows = conn.execute(text("SELECT * FROM docs WHERE id <> 'other' ORDER BY id")).mappings()
        for i, row in enumerate(rows):
            assert row["source_id"] == "web"
            assert row["url"] == urls[i]
            assert row["title"] == "preserved"
            assert row["active"] == bool(i)
            assert row["updated_by"] == "migration_009"
        assert conn.execute(
            text("SELECT source_id, updated_by FROM docs WHERE id = 'other'")
        ).one() == ("youtube", "actor")
        for url in urls:
            assert derive_source(url, build_seed_sources()) == "web"
    engine.dispose()


def test_postgres_registry_round_trip():
    import os

    import pytest
    from alembic import command
    from alembic.config import Config

    from src.config import get_settings

    if os.environ.get("RUN_PG_TESTS") != "1" or os.environ.get("RUN_PG_MIGRATION_TESTS") != "1":
        pytest.skip("requires a disposable Postgres cluster and both migration-test opt-ins")

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    engine = create_engine(get_settings().database_url)
    try:
        command.downgrade(cfg, "008")
        with engine.connect() as conn:
            restored = conn.execute(
                text("SELECT url_patterns, content_fetch_enabled FROM sources WHERE id = :id"),
                {"id": migration.RETIRED_SOURCE_ID},
            ).one()
            assert restored.url_patterns == next(
                s[2] for s in seed.SOURCES if s[0] == migration.RETIRED_SOURCE_ID
            )
            assert restored.content_fetch_enabled is False
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT id FROM sources WHERE id = :id"),
                    {"id": migration.RETIRED_SOURCE_ID},
                ).scalar_one_or_none()
                is None
            )
    finally:
        command.upgrade(cfg, "head")
        engine.dispose()


def test_retired_urls_ingest_as_web(monkeypatch):
    from fastapi.testclient import TestClient

    from contracts.fetcher import FetchResult
    from src.api.app import create_app
    from src.api.deps import get_fetchers, get_storage
    from src.config import get_settings
    from src.storage.in_memory import InMemoryStorageAdapter

    calls = []

    class FakeFetchers:
        def fetch_for(self, source_id, url):
            calls.append((source_id, url))
            return FetchResult(title="Web link", content="body", content_snapshot="body")

    monkeypatch.setenv("API_KEY", "test-key")
    get_settings.cache_clear()
    adapter = InMemoryStorageAdapter(seed_sources=build_seed_sources())
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: adapter
    app.dependency_overrides[get_fetchers] = lambda: FakeFetchers()
    retired = next(s for s in seed.SOURCES if s[0] == migration.RETIRED_SOURCE_ID)
    with TestClient(app) as client:
        for host in retired[2]:
            url = f"https://{host}/page"
            response = client.post("/docs", json={"url": url}, headers={"X-API-Key": "test-key"})
            assert response.status_code == 201
            assert response.json()["doc"]["source_id"] == "web"
            assert response.json()["doc"]["title"] == "Web link"
            assert calls[-1] == ("web", url)
