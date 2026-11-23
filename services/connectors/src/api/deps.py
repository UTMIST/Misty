from functools import lru_cache

from platform_auth import InMemoryKeyStore, key_store_from_config

from src.config import get_settings
from src.sources.base import DiscoverySource, SourceFetcher
from src.sources.google import GoogleSource
from src.sources.registry import build_registry
from contracts.meeting import MeetingWriter
from src.sources.meeting_google import GoogleMeetingWriter


@lru_cache(maxsize=1)
def get_meeting_writer() -> MeetingWriter:
    s = get_settings()
    return GoogleMeetingWriter(
        root_folder_id=s.google_meeting_root_folder_id,
        admin_emails=s.google_meeting_admin_emails,
        client_id=s.google_oauth_client_id.get_secret_value(),
        client_secret=s.google_oauth_client_secret.get_secret_value(),
        refresh_token=s.google_oauth_refresh_token.get_secret_value(),
        timeout=s.request_timeout_s,
    )


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


@lru_cache(maxsize=1)
def get_discovery_source() -> DiscoverySource:
    settings = get_settings()
    return GoogleSource(
        credentials_json_b64=settings.google_credentials_json.get_secret_value(),
        max_content_chars=settings.max_content_chars,
        max_file_bytes=settings.max_file_bytes,
        request_timeout_s=settings.request_timeout_s,
    )


@lru_cache(maxsize=1)
def get_github_linking():
    from fastapi import HTTPException

    from src.github_linking import GitHubLinking
    from src.sources.github_directory import HttpGitHubDirectory
    from src.sources.github_oauth import GitHubOAuth
    from src.sources.github_vault import GitHubVault

    s = get_settings()
    if not s.github_oauth_enabled:
        raise HTTPException(503, "GitHub linking not configured")
    return GitHubLinking(
        GitHubVault(s.github_oauth_vault_path, s.github_oauth_vault_key.get_secret_value()),
        GitHubOAuth(
            client_id=s.github_oauth_client_id.get_secret_value(),
            client_secret=s.github_oauth_client_secret.get_secret_value(),
            callback_url=s.github_oauth_callback_url,
        ),
        HttpGitHubDirectory(s.github_directory_url, s.github_directory_api_key.get_secret_value()),
    )
