from functools import lru_cache

from platform_auth import InMemoryKeyStore, key_store_from_config

from src.config import get_settings
from src.providers.base import LLMProvider
from src.providers.registry import get_provider


@lru_cache(maxsize=1)
def _key_store() -> InMemoryKeyStore:
    # Unwrap at the boundary: key_store_from_config takes a plain str and does
    # `(consumer_keys_json or "").strip()` — a SecretStr has no .strip(), so the
    # SecretStr stops here rather than leaking into the store.
    return key_store_from_config(get_settings().consumer_keys.get_secret_value())


def get_key_store() -> InMemoryKeyStore:
    """FastAPI dependency: process-wide key store. Tests override via
    app.dependency_overrides[get_key_store] = lambda: store."""
    return _key_store()


@lru_cache(maxsize=1)
def _provider() -> LLMProvider:
    return get_provider(get_settings())


def get_llm() -> LLMProvider:
    """FastAPI dependency: process-wide LLM provider. Tests override via
    app.dependency_overrides[get_llm] = lambda: fake."""
    return _provider()
