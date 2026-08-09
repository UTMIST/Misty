from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from contracts.types import Source
from src.api.app import create_app
from src.api.deps import get_directory, get_fetchers, get_storage
from src.storage.in_memory import InMemoryStorageAdapter

AUTH = {"X-API-Key": "test-key"}


def _sources():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Source(
            id="web",
            label="Web",
            url_patterns=[],
            requires_auth=False,
            has_api=False,
            content_fetch_enabled=True,
            created_at=now,
            updated_at=now,
            created_by="system",
            updated_by="system",
        )
    ]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    from src.config import get_settings

    get_settings.cache_clear()
    adapter = InMemoryStorageAdapter(seed_sources=_sources())
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: adapter
    app.dependency_overrides[get_fetchers] = lambda: object()
    app.dependency_overrides[get_directory] = lambda: object()
    with TestClient(app) as c:
        yield c


def test_list_sources(client):
    resp = client.get("/sources", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()[0]["id"] == "web"


def test_get_source(client):
    assert client.get("/sources/web", headers=AUTH).status_code == 200
    assert client.get("/sources/nope", headers=AUTH).status_code == 404
