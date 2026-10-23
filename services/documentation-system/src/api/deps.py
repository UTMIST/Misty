from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from contracts.directory import DirectoryClient
from contracts.embedder import Embedder
from contracts.retrieval import EmbeddingClient
from contracts.storage import StorageAdapter
from src.config import get_settings
from src.directory.http_client import HttpDirectoryClient
from src.embed.http_client import HttpEmbedder
from src.embedding import HttpEmbeddingClient
from src.fetch.registry import FetcherRegistry, default_registry
from contracts.meeting import MeetingWriter
from src.fetch.meeting import HttpMeetingWriter


def get_meeting_writer() -> MeetingWriter:
    s = get_settings()
    return HttpMeetingWriter(s.connectors_base_url, s.connectors_api_key.get_secret_value())


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
    registry = default_registry()
    registry.meeting_writer = get_meeting_writer()
    return registry


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


@lru_cache(maxsize=None)
def _embedder(base_url: str, api_key: str, model: str, max_request_chars: int) -> HttpEmbedder:
    # Cached per configuration for the same reason as _directory_client: one
    # pooled httpx.Client per process, not one per request.
    return HttpEmbedder(base_url, api_key, model=model, max_request_chars=max_request_chars)


def get_embedder() -> Embedder:
    """Embeds the chunk set search serves: always SEARCH_EMBED_MODEL."""
    s = get_settings()
    return _embedder(
        s.llm_base_url,
        s.llm_api_key.get_secret_value(),
        s.search_embed_model,
        s.embed_max_request_chars,
    )


def get_target_embedder() -> Embedder | None:
    """The re-embed target (#209): EMBED_MODEL when it names a model other than
    SEARCH_EMBED_MODEL, else None. Its set is built beside the served one."""
    s = get_settings()
    if s.embed_model is None or s.embed_model == s.search_embed_model:
        return None
    return _embedder(
        s.llm_base_url,
        s.llm_api_key.get_secret_value(),
        s.embed_model,
        s.embed_max_request_chars,
    )


@lru_cache(maxsize=None)
def _embedding_client(base_url: str, api_key: str, model: str) -> HttpEmbeddingClient:
    return HttpEmbeddingClient(base_url, api_key, model)


def get_embeddings() -> EmbeddingClient:
    settings = get_settings()
    return _embedding_client(
        settings.llm_base_url,
        settings.llm_api_key.get_secret_value(),
        settings.search_embed_model,
    )
