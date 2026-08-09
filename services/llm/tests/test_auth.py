import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from platform_auth import InMemoryKeyStore

from src.api.auth import get_actor
from src.api.deps import get_key_store
from src.api.hashing import generate_key


@pytest.fixture
def env_key(monkeypatch):
    monkeypatch.setenv("API_KEY", "env-bootstrap-key-value")
    from src.config import get_settings

    get_settings.cache_clear()
    yield "env-bootstrap-key-value"


@pytest.fixture
def store():
    return InMemoryKeyStore()


@pytest.fixture
def client(env_key, store):
    app = FastAPI()

    @app.get("/_probe")
    def probe(actor: str = Depends(get_actor)):
        return {"actor": actor}

    app.dependency_overrides[get_key_store] = lambda: store
    with TestClient(app) as c:
        yield c


def test_missing_key_returns_401(client):
    assert client.get("/_probe").status_code == 401


def test_empty_key_returns_401(client):
    assert client.get("/_probe", headers={"X-API-Key": ""}).status_code == 401


def test_wrong_key_returns_401(client):
    assert client.get("/_probe", headers={"X-API-Key": "wrong-value--x"}).status_code == 401


def test_malformed_prefix_returns_401(client):
    assert client.get("/_probe", headers={"X-API-Key": "llm_garbage"}).status_code == 401


def test_env_bootstrap_key_works(client, env_key):
    resp = client.get("/_probe", headers={"X-API-Key": env_key})
    assert resp.status_code == 200
    assert resp.json()["actor"] == "env-bootstrap"


def test_consumer_key_works_and_surfaces_identity(client, store):
    plaintext, prefix, key_hash = generate_key()
    store.add(prefix=prefix, key_hash=key_hash, name="reviewer-summaries", scopes=["chat"])
    resp = client.get("/_probe", headers={"X-API-Key": plaintext})
    assert resp.status_code == 200
    assert resp.json()["actor"] == "reviewer-summaries"


def test_tampered_consumer_key_returns_401(client, store):
    plaintext, prefix, key_hash = generate_key()
    store.add(prefix=prefix, key_hash=key_hash, name="reviewer-summaries", scopes=["chat"])
    tampered = plaintext[:-4] + "XXXX"
    assert client.get("/_probe", headers={"X-API-Key": tampered}).status_code == 401
