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


def test_directory_key_is_redacted_in_repr():
    s = _settings(directory_api_key=SecretStr("super-secret-value"))
    assert "super-secret-value" not in repr(s)
    assert "super-secret-value" not in str(s.directory_api_key)
    assert s.directory_api_key.get_secret_value() == "super-secret-value"
