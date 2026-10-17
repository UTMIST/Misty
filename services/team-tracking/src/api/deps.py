from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from contracts.groups import GroupProvider
from contracts.storage import StorageAdapter
from src.config import Settings, get_settings
from src.providers.google_groups import GoogleGroupsProvider
from src.storage.postgres import PostgresStorageAdapter


@lru_cache(maxsize=1)
def _default_engine() -> Engine:
    return create_engine(get_settings().database_url, future=True, pool_pre_ping=True)


def get_storage() -> StorageAdapter:
    """FastAPI dependency: return the process-wide storage adapter.

    Tests override this to inject InMemoryStorageAdapter via
    app.dependency_overrides[get_storage] = lambda: adapter.
    """
    return PostgresStorageAdapter(_default_engine())


def build_group_provider(settings: Settings) -> GroupProvider | None:
    """The Google Groups provider, or None when it is not configured."""
    if not all(settings.google_groups_fields().values()):
        return None
    return GoogleGroupsProvider(
        customer_id=settings.google_groups_customer_id,
        domain=settings.google_groups_domain,
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret.get_secret_value(),
        refresh_token=settings.google_oauth_refresh_token.get_secret_value(),
    )


@lru_cache(maxsize=1)
def _default_group_provider() -> GroupProvider | None:
    return build_group_provider(get_settings())


def get_group_provider() -> GroupProvider | None:
    """FastAPI dependency: process-wide group provider, None when unconfigured.
    Tests override via app.dependency_overrides[get_group_provider]."""
    return _default_group_provider()
