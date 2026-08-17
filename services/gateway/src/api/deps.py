from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from contracts.directory import DirectoryClient
from contracts.storage import StorageAdapter
from src.config import get_settings


@lru_cache(maxsize=1)
def _default_engine() -> Engine:
    return create_engine(get_settings().database_url, future=True, pool_pre_ping=True)


@lru_cache(maxsize=1)
def _default_directory() -> DirectoryClient:
    from src.directory.http_client import HttpDirectoryClient

    s = get_settings()
    # .get_secret_value() at the boundary: HttpDirectoryClient puts this
    # straight into an outbound header, which needs the raw str. This is one of
    # the two sanctioned unwrap sites (the other is verify_production_secrets);
    # everywhere else the field stays wrapped.
    return HttpDirectoryClient(s.directory_base_url, s.directory_api_key.get_secret_value())


def get_storage() -> StorageAdapter:
    from src.storage.postgres import PostgresStorageAdapter

    return PostgresStorageAdapter(_default_engine())


def get_directory() -> DirectoryClient:
    # Cached, not per-request: the client owns a connection pool (see
    # HttpDirectoryClient), which is worthless if it is rebuilt every request.
    return _default_directory()
