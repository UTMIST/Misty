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


def test_revoked_key_401():
    store = InMemoryStorageAdapter()
    plaintext, prefix, key_hash = generate_key()
    row = store.create_api_key(
        name="c", prefix=prefix, key_hash=key_hash, scopes=["resolve:discord"], actor="t"
    )
    app = FastAPI()

    @app.get("/probe")
    def probe(_=Depends(require_scope("resolve:discord"))):
        return {"ok": True}

    app.dependency_overrides[get_storage] = lambda: store
    client = TestClient(app)
    assert client.get("/probe", headers={"X-API-Key": plaintext}).status_code == 200
    store.revoke_api_key(row.id, actor="t")
    assert client.get("/probe", headers={"X-API-Key": plaintext}).status_code == 401


def test_no_env_bootstrap_admin_path():
    """The gateway must not honour an env key. See src/api/auth.py.

    platform_auth's bootstrap path hands out ADMIN_SCOPE, which satisfies every
    require_scope check. src/api/auth.py disables it by passing
    get_env_key=lambda: None, and this pins that: nothing that isn't a live row
    in api_keys gets through, including the value other services use as their
    env key. Without the guard, `API_KEY=<anything>` in the gateway's
    environment would be a wildcard credential on the public door.
    """
    client, _ = _client_with_key(["resolve:discord"])
    for candidate in ("dev-api-key-change-me", "", "admin", "gw_notarealkey"):
        assert client.get("/probe", headers={"X-API-Key": candidate}).status_code == 401
