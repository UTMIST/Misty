from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from contracts.storage import StorageAdapter
from src.config import get_settings


@lru_cache(maxsize=1)
def _default_engine() -> Engine:
    return create_engine(get_settings().database_url, future=True, pool_pre_ping=True)


def get_storage() -> StorageAdapter:
    from src.storage.postgres import PostgresStorageAdapter

    return PostgresStorageAdapter(_default_engine())


def get_directory():
    from src.directory.http_client import HttpDirectoryClient

    s = get_settings()
    return HttpDirectoryClient(s.directory_base_url, s.directory_api_key)
