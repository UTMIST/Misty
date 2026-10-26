import pytest
from pydantic import SecretStr

from src.config import DEFAULT_DEV_API_KEY, Settings, verify_production_secrets


def _settings(**overrides) -> Settings:
    # _env_file=None so a developer's local .env can't leak into these
    # assertions; every field under test is passed explicitly.
    base = {
        "gateway_env": "production",
        "directory_api_key": SecretStr("a-real-issued-key"),
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_no_inbound_api_key_field():
    """The gateway must not carry an env-bootstrap key at all.

    Re-adding an `api_key` field is the first half of re-enabling the wildcard
    admin path this service deliberately does without; src/api/auth.py is the
    second half, pinned by tests/test_auth.py.
    """
    assert "api_key" not in Settings.model_fields


def test_local_tolerates_the_dev_default():
    verify_production_secrets(
        _settings(gateway_env="local", directory_api_key=SecretStr(DEFAULT_DEV_API_KEY))
    )


@pytest.mark.parametrize("env", ["staging", "production"])
def test_non_local_refuses_the_dev_default(env):
    """The guard fires — and keeps firing once directory_api_key is a SecretStr.

    This is the test platform_auth's secret_guard docstring asks every service
    to have. A SecretStr never compares equal to a str, so writing the check as
    `settings.directory_api_key == DEFAULT_DEV_API_KEY` (without
    .get_secret_value()) makes it False forever: the service boots happily in
    production with a publicly-known key and nothing else notices.
    """
    with pytest.raises(RuntimeError, match="DIRECTORY_API_KEY"):
        verify_production_secrets(
            _settings(gateway_env=env, directory_api_key=SecretStr(DEFAULT_DEV_API_KEY))
        )


def test_non_local_accepts_a_real_secret():
    verify_production_secrets(_settings())


@pytest.mark.parametrize("secret", ["", "  ", f" {DEFAULT_DEV_API_KEY} "])
def test_non_local_refuses_blank_or_padded_default(secret):
    with pytest.raises(RuntimeError, match="DIRECTORY_API_KEY"):
        verify_production_secrets(_settings(directory_api_key=SecretStr(secret)))


def test_directory_key_is_redacted_in_repr():
    s = _settings(directory_api_key=SecretStr("super-secret-value"))
    assert "super-secret-value" not in repr(s)
    assert "super-secret-value" not in str(s.directory_api_key)
    assert s.directory_api_key.get_secret_value() == "super-secret-value"


@pytest.mark.parametrize("value", ["", " ", DEFAULT_DEV_API_KEY])
def test_enabled_email_login_needs_real_verification_key(value):
    with pytest.raises(RuntimeError, match="VERIFICATION_API_KEY"):
        verify_production_secrets(_settings(email_login_enabled=True, verification_api_key=value))


def test_verification_key_is_redacted():
    settings = _settings(verification_api_key="test-private-key")
    assert "test-private-key" not in repr(settings)
    assert settings.verification_api_key.get_secret_value() == "test-private-key"


@pytest.mark.parametrize(
    "overrides",
    [
        {"login_status_per_minute": 0},
        {"edge_requests_per_minute": 0},
        {"login_status_per_minute": 600, "edge_requests_per_minute": 600},
        {"login_status_per_minute": 10001, "edge_requests_per_minute": 20000},
    ],
)
def test_status_capacity_requires_bounded_limits_and_edge_headroom(overrides):
    with pytest.raises(ValueError):
        _settings(**overrides)


def test_invalid_capacity_does_not_print_configured_credentials():
    with pytest.raises(ValueError) as exc:
        Settings(_env_file=None, directory_api_key="Z", edge_requests_per_minute=120)
    assert "input_value=" not in str(exc.value)
    assert "Z" not in str(exc.value)
