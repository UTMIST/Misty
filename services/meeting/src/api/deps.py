from functools import lru_cache

from src.config import get_settings
from src.key_store import InMemoryKeyStore, key_store_from_config


@lru_cache(maxsize=1)
def _key_store() -> InMemoryKeyStore:
    # key_store_from_config parses raw JSON, so unwrap here: the SecretStr
    # boundary stops at this edge and the store itself keeps taking a str.
    return key_store_from_config(get_settings().consumer_keys.get_secret_value())


def get_key_store() -> InMemoryKeyStore:
    """FastAPI dependency: process-wide key store. Tests override via
    app.dependency_overrides[get_key_store] = lambda: store."""
    return _key_store()
