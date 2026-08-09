import pytest
from fastapi.testclient import TestClient

from conftest import build_seed_sources
from src.api.app import create_app
from src.api.deps import get_directory, get_fetchers, get_storage
from src.storage.in_memory import InMemoryStorageAdapter


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    from src.config import get_settings

    get_settings.cache_clear()
    adapter = InMemoryStorageAdapter(seed_sources=build_seed_sources())
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: adapter
    app.dependency_overrides[get_fetchers] = lambda: object()
    app.dependency_overrides[get_directory] = lambda: object()
    with TestClient(app) as c:
        yield c


def test_openapi_served(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert "/docs" in paths
    assert "/sources" in paths
