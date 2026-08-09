"""In-memory API-key store seeded from config — the DB-free implementation of
this package's ApiKeyStore protocol.

Services with a database back ApiKeyStore with a table; services without one
(llm, connectors) seed this from a CONSUMER_KEYS JSON env var. Swappable for a
persistent store later (#44).
"""

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from platform_auth.secret_guard import reject_secret_wrapper


@dataclass(frozen=True)
class ConsumerKeyRow:
    id: UUID
    name: str
    scopes: list[str]
    active: bool = True
    revoked_at: datetime | None = None


class InMemoryKeyStore:
    def __init__(self) -> None:
        self._hash_by_prefix: dict[str, str] = {}
        self._row_by_prefix: dict[str, ConsumerKeyRow] = {}

    def add(self, *, prefix: str, key_hash: str, name: str, scopes: list[str]) -> None:
        if prefix in self._hash_by_prefix:
            raise ValueError(f"duplicate key prefix in config: {prefix!r}")
        self._hash_by_prefix[prefix] = key_hash
        self._row_by_prefix[prefix] = ConsumerKeyRow(id=uuid4(), name=name, scopes=list(scopes))

    # --- platform_auth.ApiKeyStore protocol ---
    def get_api_key_hash(self, prefix: str) -> str | None:
        return self._hash_by_prefix.get(prefix)

    def get_api_key_by_prefix(self, prefix: str) -> ConsumerKeyRow | None:
        return self._row_by_prefix.get(prefix)

    def touch_api_key_last_used(self, api_key_id: UUID) -> None:
        return None  # stateless — nothing to record


def key_store_from_config(consumer_keys_json: str) -> InMemoryKeyStore:
    # Services hold consumer_keys as SecretStr; callers must unwrap. Guarded
    # before the .strip() below, whose AttributeError would otherwise escape
    # uncaught (it runs outside the try) with no hint about the real mistake.
    reject_secret_wrapper(consumer_keys_json, param="key_store_from_config(consumer_keys_json)")
    store = InMemoryKeyStore()
    raw = (consumer_keys_json or "").strip()
    if not raw:
        return store
    try:
        entries = json.loads(raw)
        if not isinstance(entries, list):
            raise TypeError("'CONSUMER_KEYS' must be a JSON array")
        for entry in entries:
            if not isinstance(entry, dict):
                raise TypeError("each CONSUMER_KEYS entry must be an object")
            scopes = entry.get("scopes", [])
            if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
                raise TypeError(
                    f"'scopes' must be a list of strings for entry {entry.get('name')!r}"
                )
            store.add(
                prefix=entry["prefix"],
                key_hash=entry["key_hash"],
                name=entry["name"],
                scopes=scopes,
            )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid CONSUMER_KEYS config: {exc}") from exc
    return store
