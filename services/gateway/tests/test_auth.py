from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.api.auth import require_scope
from src.api.deps import get_storage
from src.api.hashing import generate_key
from src.storage.in_memory import InMemoryStorageAdapter


def _client_with_key(scopes):
    store = InMemoryStorageAdapter()
    plaintext, prefix, key_hash = generate_key()
    store.create_api_key(name="c", prefix=prefix, key_hash=key_hash, scopes=scopes, actor="t")
    app = FastAPI()

    @app.get("/probe")
    def probe(_=Depends(require_scope("resolve:discord"))):
        return {"ok": True}

    app.dependency_overrides[get_storage] = lambda: store
    return TestClient(app), plaintext


def test_valid_scope_200():
    client, key = _client_with_key(["resolve:discord"])
    assert client.get("/probe", headers={"X-API-Key": key}).status_code == 200


def test_missing_scope_403_and_no_key_401():
    client, key = _client_with_key(["other:scope"])
    assert client.get("/probe", headers={"X-API-Key": key}).status_code == 403
    assert client.get("/probe").status_code == 401
