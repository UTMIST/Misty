from fastapi.testclient import TestClient

from src.api.app import create_app


def test_health_needs_no_key():
    with TestClient(create_app()) as c:
        assert c.get("/health").status_code == 200


def test_key_store_is_seeded_from_consumer_keys(monkeypatch):
    monkeypatch.setenv(
        "CONSUMER_KEYS",
        '[{"name":"documentation-system","prefix":"abc12345","key_hash":"h","scopes":["fetch"]}]',
    )
    from src.api.deps import get_key_store

    store = get_key_store()
    assert store.get_api_key_hash("abc12345") == "h"
    assert store.get_api_key_by_prefix("abc12345").scopes == ["fetch"]
