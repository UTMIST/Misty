from src.config import Settings
from src.sources.google import GOOGLE_SOURCE_IDS
from src.sources.registry import build_registry


def test_all_google_source_ids_share_one_google_source_instance():
    # Regression: build_registry used to instantiate a fresh GoogleSource per
    # source id, so each got its own independent (and independently
    # rebuilt-per-request) API-client cache.
    registry = build_registry(Settings())

    assert set(registry) == set(GOOGLE_SOURCE_IDS)
    instances = {id(source) for source in registry.values()}
    assert len(instances) == 1


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
