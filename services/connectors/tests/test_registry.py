from src.config import Settings
from src.sources.github import GithubSource
from src.sources.google import GOOGLE_SOURCE_IDS, GoogleSource
from src.sources.registry import build_registry


def test_all_google_source_ids_share_one_google_source_instance():
    # Regression: build_registry used to instantiate a fresh GoogleSource per
    # source id, so each got its own independent (and independently
    # rebuilt-per-request) API-client cache.
    registry = build_registry(Settings())

    assert set(GOOGLE_SOURCE_IDS) <= set(registry)
    instances = {id(registry[source_id]) for source_id in GOOGLE_SOURCE_IDS}
    assert len(instances) == 1
    assert isinstance(registry["gdocs"], GoogleSource)


def test_google_source_is_configured_with_settings_values():
    settings = Settings(
        google_credentials_json="fake",
        max_content_chars=42,
        request_timeout_s=9.0,
    )
    registry = build_registry(settings)
    source = registry["gdocs"]

    assert source._credentials_json_b64 == "fake"
    assert source._max_content_chars == 42
    assert source._request_timeout_s == 9.0


def test_github_source_is_registered_and_configured_with_settings_values():
    settings = Settings(
        github_token="ghp_fake",
        max_content_chars=42,
        max_file_bytes=99,
        request_timeout_s=9.0,
    )
    source = build_registry(settings)["github"]

    assert isinstance(source, GithubSource)
    assert source._token == "ghp_fake"
    assert source._max_content_chars == 42
    assert source._max_file_bytes == 99
    assert source._request_timeout_s == 9.0


def test_github_source_without_token_still_registers():
    assert build_registry(Settings())["github"]._token == ""
