from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from contracts.directory import DirectoryClient
from contracts.storage import StorageAdapter
from src.config import get_settings
from src.directory.http_client import HttpDirectoryClient
from src.fetch.registry import FetcherRegistry, default_registry


@lru_cache(maxsize=1)
def _default_engine() -> Engine:
    return create_engine(get_settings().database_url, future=True, pool_pre_ping=True)


def get_storage() -> StorageAdapter:
    # Imported lazily: src.storage.postgres does not exist until Task 15.
    # Deferring the import keeps this module importable now; tests always
    # override get_storage via app.dependency_overrides.
    from src.storage.postgres import PostgresStorageAdapter

    return PostgresStorageAdapter(_default_engine())


def get_fetchers() -> FetcherRegistry:
    return default_registry()


@lru_cache(maxsize=None)
def _directory_client(base_url: str, api_key: str) -> HttpDirectoryClient:
    # Cached per (base_url, api_key) so the underlying httpx.Client (and its
    # connection pool) is built once and reused across requests, instead of a
    # fresh client+connection per call.
    return HttpDirectoryClient(base_url, api_key)


def get_directory() -> DirectoryClient:
    s = get_settings()
    # SecretStr boundary: HttpDirectoryClient sends the key as a plain header
    # value, so unwrap here rather than pushing SecretStr into the client.
    return _directory_client(s.directory_base_url, s.directory_api_key.get_secret_value())
