import json

import pytest

from platform_auth import (
    InMemoryKeyStore,
    generate_key,
    key_store_from_config,
    parse_prefix,
    verify_key,
)

ENVELOPE = "test_"


def test_add_and_lookup_by_prefix():
    plaintext, prefix, key_hash = generate_key(ENVELOPE)
    store = InMemoryKeyStore()
    store.add(prefix=prefix, key_hash=key_hash, name="reviewer", scopes=["chat"])

    assert store.get_api_key_hash(prefix) == key_hash
    row = store.get_api_key_by_prefix(prefix)
    assert row is not None
    assert row.name == "reviewer"
    assert row.scopes == ["chat"]
    assert row.active is True
    assert row.revoked_at is None
    # hash verifies against the plaintext
    assert verify_key(plaintext, store.get_api_key_hash(prefix)) is True


def test_unknown_prefix_returns_none():
    store = InMemoryKeyStore()
    assert store.get_api_key_hash("nope") is None
    assert store.get_api_key_by_prefix("nope") is None


def test_touch_is_noop():
    store = InMemoryKeyStore()
    assert store.touch_api_key_last_used("anything") is None


def test_from_config_empty_is_empty_store():
    assert key_store_from_config("").get_api_key_by_prefix("x") is None
    assert key_store_from_config("   ").get_api_key_by_prefix("x") is None


def test_from_config_parses_entries():
    plaintext, prefix, key_hash = generate_key(ENVELOPE)
    cfg = json.dumps([{"name": "docs-bot", "prefix": prefix, "key_hash": key_hash}])
    store = key_store_from_config(cfg)
    row = store.get_api_key_by_prefix(prefix)
    assert row is not None and row.name == "docs-bot"
    assert row.scopes == []  # default when omitted
    assert parse_prefix(plaintext, ENVELOPE) == prefix


def test_from_config_malformed_json_raises():
    with pytest.raises(RuntimeError, match="CONSUMER_KEYS"):
        key_store_from_config("{not valid json")


def test_from_config_missing_required_field_raises():
    with pytest.raises(RuntimeError, match="CONSUMER_KEYS"):
        key_store_from_config(json.dumps([{"name": "x"}]))


def test_from_config_duplicate_prefix_raises():
    _, prefix, key_hash = generate_key(ENVELOPE)
    cfg = json.dumps(
        [
            {"name": "a", "prefix": prefix, "key_hash": key_hash},
            {"name": "b", "prefix": prefix, "key_hash": key_hash},
        ]
    )
    with pytest.raises(RuntimeError, match="CONSUMER_KEYS"):
        key_store_from_config(cfg)


def test_from_config_non_list_scopes_raises():
    _, prefix, key_hash = generate_key(ENVELOPE)
    cfg = json.dumps([{"name": "a", "prefix": prefix, "key_hash": key_hash, "scopes": "chat"}])
    with pytest.raises(RuntimeError, match="CONSUMER_KEYS"):
        key_store_from_config(cfg)


def test_from_config_non_array_top_level_raises():
    # A JSON object (e.g. `{}`) would otherwise silently iterate as zero entries,
    # yielding an empty store instead of failing fast at boot.
    with pytest.raises(RuntimeError, match="CONSUMER_KEYS"):
        key_store_from_config("{}")


def test_from_config_non_object_entry_raises():
    with pytest.raises(RuntimeError, match="CONSUMER_KEYS"):
        key_store_from_config(json.dumps(["not-an-object"]))
