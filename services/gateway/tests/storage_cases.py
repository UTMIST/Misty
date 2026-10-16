from uuid import uuid4

import pytest


class StorageCases:
    def test_readiness(self, adapter):
        assert adapter.is_ready() is True

    def test_key_lifecycle(self, adapter):
        key = adapter.create_api_key(
            name="consumer",
            prefix="prefix",
            key_hash="HASH",
            scopes=["resolve:discord"],
            actor="cli",
        )
        assert adapter.get_api_key_hash("prefix") == "HASH"
        assert adapter.get_api_key_by_prefix("prefix").scopes == ["resolve:discord"]
        assert [k.id for k in adapter.list_api_keys(active_only=True)] == [key.id]
        adapter.touch_api_key_last_used(key.id)
        assert adapter.get_api_key_by_prefix("prefix").last_used_at is not None

        revoked = adapter.revoke_api_key(key.id, actor="operator")
        assert revoked.active is False
        assert revoked.revoked_at is not None
        assert adapter.get_api_key_hash("prefix") is None
        assert adapter.list_api_keys(active_only=True) == []
        assert [k.id for k in adapter.list_api_keys()] == [key.id]
        repeated = adapter.revoke_api_key(key.id, actor="another-operator")
        assert repeated.revoked_at == revoked.revoked_at

    @pytest.mark.parametrize("field", ["name", "prefix"])
    def test_unique_name_and_prefix(self, adapter, field):
        values = dict(name="consumer", prefix="prefix", key_hash="HASH", scopes=[], actor="cli")
        adapter.create_api_key(**values)
        other = values | {"name": "other", "prefix": "other"}
        other[field] = values[field]
        with pytest.raises(ValueError):
            adapter.create_api_key(**other)

    def test_unknown_key(self, adapter):
        assert adapter.get_api_key_hash("unknown") is None
        assert adapter.get_api_key_by_prefix("unknown") is None
        assert adapter.revoke_api_key(uuid4(), actor="cli") is None
        adapter.touch_api_key_last_used(uuid4())
