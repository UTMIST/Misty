from src.storage.in_memory import InMemoryStorageAdapter


def test_create_get_verify_revoke_roundtrip():
    a = InMemoryStorageAdapter()
    key = a.create_api_key(name="gh-action", prefix="abcd1234", key_hash="HASH",
                           scopes=["resolve:discord"], actor="cli")
    assert key.name == "gh-action" and key.active is True
    assert a.get_api_key_hash("abcd1234") == "HASH"
    row = a.get_api_key_by_prefix("abcd1234")
    assert row.scopes == ["resolve:discord"]
    assert [k.name for k in a.list_api_keys()] == ["gh-action"]
    a.touch_api_key_last_used(key.id)
    revoked = a.revoke_api_key(key.id, actor="cli")
    assert revoked.active is False and revoked.revoked_at is not None
    # revoked key: hash still returns, but active=false (auth layer rejects)
    assert a.get_api_key_by_prefix("abcd1234").active is False


def test_unknown_prefix_returns_none():
    a = InMemoryStorageAdapter()
    assert a.get_api_key_hash("nope") is None
    assert a.get_api_key_by_prefix("nope") is None
    assert a.revoke_api_key(__import__("uuid").uuid4(), actor="cli") is None
