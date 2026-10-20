import pytest
from fastapi.testclient import TestClient

from conftest import build_seed_providers, build_seed_role_kinds
from src.api.app import create_app
from src.api.deps import get_storage
from src.storage.in_memory import InMemoryStorageAdapter

AUTH = {"X-API-Key": "test-key"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    from src.config import get_settings

    get_settings.cache_clear()
    adapter = InMemoryStorageAdapter(
        seed_role_kinds=build_seed_role_kinds(),
        seed_providers=build_seed_providers(),
    )
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: adapter
    with TestClient(app) as c:
        yield c


def test_list_providers(client):
    resp = client.get("/providers", headers=AUTH)
    assert resp.status_code == 200
    assert {p["id"] for p in resp.json()} == {
        "discord",
        "github",
        "uoft_email",
        "email",
    }


def test_get_provider(client):
    resp = client.get("/providers/discord", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["label"] == "Discord"


def test_get_provider_not_found(client):
    resp = client.get("/providers/nope", headers=AUTH)
    assert resp.status_code == 404


def test_providers_require_api_key(client):
    assert client.get("/providers").status_code == 401


def test_providers_read_denied_without_scope(client):
    from src.api.hashing import generate_key

    adapter = client.app.dependency_overrides[get_storage]()
    plaintext, prefix, key_hash = generate_key()
    adapter.create_api_key(
        name="people-only",
        prefix=prefix,
        key_hash=key_hash,
        scopes=["people:read"],
        actor="admin",
    )
    resp = client.get("/providers", headers={"X-API-Key": plaintext})
    assert resp.status_code == 403
    assert "providers:read" in resp.json()["detail"]
