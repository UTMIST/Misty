from datetime import datetime, timezone
from uuid import UUID, uuid4

from contracts.types import ApiKey


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryStorageAdapter:
    """In-process adapter for tests. Not persistent, not thread-safe."""

    def __init__(self) -> None:
        self._api_keys: dict[UUID, ApiKey] = {}
        self._hashes: dict[UUID, str] = {}

    def create_api_key(
        self, *, name: str, prefix: str, key_hash: str, scopes: list[str], actor: str
    ) -> ApiKey:
        if any(k.name == name for k in self._api_keys.values()):
            raise ValueError(f"api key name already exists: {name}")
        if any(k.prefix == prefix for k in self._api_keys.values()):
            raise ValueError(f"api key prefix already exists: {prefix}")
        key = ApiKey(id=uuid4(), name=name, prefix=prefix, scopes=list(scopes), active=True)
        self._api_keys[key.id] = key
        self._hashes[key.id] = key_hash
        return key

    def _by_prefix(self, prefix: str) -> ApiKey | None:
        return next((k for k in self._api_keys.values() if k.prefix == prefix), None)

    def get_api_key_by_prefix(self, prefix: str) -> ApiKey | None:
        return self._by_prefix(prefix)

    def get_api_key_hash(self, prefix: str) -> str | None:
        row = self._by_prefix(prefix)
        return self._hashes.get(row.id) if row else None

    def list_api_keys(self, *, active_only: bool = False) -> list[ApiKey]:
        keys = list(self._api_keys.values())
        if active_only:
            keys = [k for k in keys if k.active and k.revoked_at is None]
        return keys

    def revoke_api_key(self, api_key_id: UUID, *, actor: str) -> ApiKey | None:
        key = self._api_keys.get(api_key_id)
        if key is None:
            return None
        updated = key.model_copy(update={"active": False, "revoked_at": _now()})
        self._api_keys[api_key_id] = updated
        return updated

    def touch_api_key_last_used(self, api_key_id: UUID) -> None:
        key = self._api_keys.get(api_key_id)
        if key is not None:
            self._api_keys[api_key_id] = key.model_copy(update={"last_used_at": _now()})
