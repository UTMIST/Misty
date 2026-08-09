from functools import lru_cache

from platform_auth import InMemoryKeyStore, key_store_from_config

from src.config import get_settings
from src.sources.base import SourceFetcher
from src.sources.registry import build_registry


@lru_cache(maxsize=1)
def _key_store() -> InMemoryKeyStore:
    return key_store_from_config(get_settings().consumer_keys.get_secret_value())


def get_key_store() -> InMemoryKeyStore:
    """FastAPI dependency: process-wide key store. Tests override via
    app.dependency_overrides[get_key_store] = lambda: store."""
    return _key_store()


@lru_cache(maxsize=1)
def _source_registry() -> dict[str, SourceFetcher]:
    return build_registry(get_settings())


def get_source_registry() -> dict[str, SourceFetcher]:
    """FastAPI dependency: process-wide source registry. Tests override via
    app.dependency_overrides[get_source_registry] = lambda: {"gdocs": fake}."""
    return _source_registry()
