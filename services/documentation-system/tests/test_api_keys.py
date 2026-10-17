"""Tests for GET /api-keys/self — key self-introspection.

The endpoint exists so a caller can ask an authorization question and get an
authorization answer. Without it, a tool can only infer its own access from
filtered data — and `read_context` answers a key that lacks `docs:read:all`
with `200 []`, not 403 (see tests/test_api_visibility.py and src/backfill.py).
"""

import pytest
from fastapi.testclient import TestClient

from conftest import build_seed_sources
from src.api.app import create_app
from src.api.deps import get_storage
from src.api.hashing import generate_key
from src.storage.in_memory import InMemoryStorageAdapter


@pytest.fixture
def adapter():
    return InMemoryStorageAdapter(seed_sources=build_seed_sources())


@pytest.fixture
def client(monkeypatch, adapter):
    monkeypatch.setenv("API_KEY", "env-bootstrap-key-value")
    from src.config import get_settings

    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: adapter
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _issue(adapter, name: str, scopes: list[str]) -> str:
    plaintext, prefix, key_hash = generate_key()
    adapter.create_api_key(name=name, prefix=prefix, key_hash=key_hash, scopes=scopes, actor="test")
    return plaintext


def test_self_returns_key_metadata(client, adapter):
    plaintext = _issue(adapter, "content-backfill", ["docs:read", "docs:read:all"])
    resp = client.get("/api-keys/self", headers={"X-API-Key": plaintext})
    assert resp.status_code == 200
    assert resp.json() == {
        "name": "content-backfill",
        "scopes": ["docs:read", "docs:read:all"],  # sorted
    }


def test_self_returns_sorted_scopes(client, adapter):
    plaintext = _issue(adapter, "bot", ["docs:write", "act-as-user", "docs:read"])
    resp = client.get("/api-keys/self", headers={"X-API-Key": plaintext})
    assert resp.json()["scopes"] == ["act-as-user", "docs:read", "docs:write"]


def test_self_reports_the_admin_wildcard_literally(client, adapter):
    """`admin` is reported as itself, not expanded: has_scope's wildcard is the
    caller's rule to apply (src/backfill.py does), not this endpoint's."""
    plaintext = _issue(adapter, "root", ["admin"])
    resp = client.get("/api-keys/self", headers={"X-API-Key": plaintext})
    assert resp.json()["scopes"] == ["admin"]


def test_self_works_for_env_bootstrap_key(client):
    resp = client.get("/api-keys/self", headers={"X-API-Key": "env-bootstrap-key-value"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "env-bootstrap"
    assert "admin" in body["scopes"]


def test_self_401_without_key(client):
    assert client.get("/api-keys/self").status_code == 401


def test_self_401_for_an_unknown_key(client):
    resp = client.get("/api-keys/self", headers={"X-API-Key": "doc_nope_nope"})
    assert resp.status_code == 401


def test_self_requires_no_specific_scope(client, adapter):
    """A key with zero scopes can still read its own grant — a caller must be
    able to learn it is under-scoped, which is the whole point."""
    plaintext = _issue(adapter, "scopeless", [])
    resp = client.get("/api-keys/self", headers={"X-API-Key": plaintext})
    assert resp.status_code == 200
    assert resp.json() == {"name": "scopeless", "scopes": []}


def test_self_never_reveals_another_key(client, adapter):
    _issue(adapter, "other", ["admin"])
    plaintext = _issue(adapter, "mine", ["docs:read"])
    resp = client.get("/api-keys/self", headers={"X-API-Key": plaintext})
    assert resp.json() == {"name": "mine", "scopes": ["docs:read"]}


def test_self_response_declared_in_openapi_schema(client):
    """The endpoint has a typed response model, so downstream code-gen sees the shape."""
    schema = client.get("/openapi.json").json()
    self_op = schema["paths"]["/api-keys/self"]["get"]
    ref = self_op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/SelfKey")
    assert set(schema["components"]["schemas"]["SelfKey"]["properties"]) == {"name", "scopes"}
