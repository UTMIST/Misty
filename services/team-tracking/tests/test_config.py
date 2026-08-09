"""Tests for the Settings module (src/config.py)."""

import pytest

from src.config import get_settings

# The get_settings lru_cache is cleared around every test by the autouse
# _clear_settings_cache fixture in tests/conftest.py, which in turn depends on
# _no_dotenv so no Settings instance is ever built while `.env` is still live.


def test_tt_env_defaults_to_local(monkeypatch):
    monkeypatch.delenv("TT_ENV", raising=False)
    assert get_settings().tt_env == "local"


def test_tt_env_reads_from_environment(monkeypatch):
    monkeypatch.setenv("TT_ENV", "production")
    assert get_settings().tt_env == "production"


def test_tt_env_accepts_staging(monkeypatch):
    monkeypatch.setenv("TT_ENV", "staging")
    assert get_settings().tt_env == "staging"


def test_tt_env_rejects_unknown_values(monkeypatch):
    monkeypatch.setenv("TT_ENV", "Production")  # capital P — typo
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        get_settings()


# --- production secret guard ------------------------------------------------


def test_verify_secrets_raises_in_production_with_default_api_key(monkeypatch):
    from src.config import DEFAULT_DEV_API_KEY, verify_production_secrets

    monkeypatch.setenv("TT_ENV", "production")
    monkeypatch.setenv("API_KEY", DEFAULT_DEV_API_KEY)
    with pytest.raises(RuntimeError) as exc:
        verify_production_secrets()
    assert "API_KEY" in str(exc.value)


def test_verify_secrets_raises_in_staging_with_default_api_key(monkeypatch):
    from src.config import DEFAULT_DEV_API_KEY, verify_production_secrets

    monkeypatch.setenv("TT_ENV", "staging")
    monkeypatch.setenv("API_KEY", DEFAULT_DEV_API_KEY)
    with pytest.raises(RuntimeError):
        verify_production_secrets()


def test_verify_secrets_raises_in_production_when_api_key_unset(monkeypatch):
    """Unset API_KEY falls back to the built-in default — must still be caught."""
    from src.config import verify_production_secrets

    monkeypatch.setenv("TT_ENV", "production")
    monkeypatch.delenv("API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        verify_production_secrets()


def test_verify_secrets_passes_in_local_with_default_api_key(monkeypatch):
    from src.config import DEFAULT_DEV_API_KEY, verify_production_secrets

    monkeypatch.setenv("TT_ENV", "local")
    monkeypatch.setenv("API_KEY", DEFAULT_DEV_API_KEY)
    verify_production_secrets()  # must not raise


def test_verify_secrets_passes_in_production_with_real_api_key(monkeypatch):
    from src.config import verify_production_secrets

    monkeypatch.setenv("TT_ENV", "production")
    monkeypatch.setenv("API_KEY", "a-strong-unique-production-secret")
    verify_production_secrets()  # must not raise


def test_verify_secrets_still_fires_after_secretstr_conversion(monkeypatch):
    """The guard must survive api_key becoming a SecretStr.

    SecretStr never compares equal to a str, so writing the check as
    `settings.api_key == DEFAULT_DEV_API_KEY` would evaluate False forever —
    silently disabling the fail-fast guard while every other test still
    passed. This test asserts the guard fires through a Settings object whose
    api_key is genuinely a SecretStr, so that failure mode is caught.
    """
    from pydantic import SecretStr

    from src.config import DEFAULT_DEV_API_KEY, Settings, verify_production_secrets

    s = Settings(tt_env="production", api_key=DEFAULT_DEV_API_KEY)
    assert isinstance(s.api_key, SecretStr)
    assert s.api_key != DEFAULT_DEV_API_KEY  # the trap: no coercion, no equality
    with pytest.raises(RuntimeError) as exc:
        verify_production_secrets(s)
    assert "API_KEY" in str(exc.value)


def test_verify_secrets_passes_with_strong_secretstr_key():
    from src.config import Settings, verify_production_secrets

    s = Settings(tt_env="production", api_key="a-strong-unique-production-secret")
    verify_production_secrets(s)  # must not raise


def test_api_key_never_leaks_via_string_conversion():
    # Regression: api_key used to be a plain str, so a failing assertion diff
    # (or any repr/log/traceback) printed the admin bootstrap key in full —
    # exactly how the connectors service leaked a real Google service-account
    # private key into a terminal and a session transcript. SecretStr must keep
    # that structurally impossible. Do NOT "simplify" the annotation back to
    # str: only verify_production_secrets and src/api/auth.py should ever call
    # .get_secret_value().
    from src.config import Settings

    secret = "super-secret-bootstrap-admin-key"
    s = Settings(api_key=secret)

    assert secret not in repr(s)
    assert secret not in str(s)
    assert secret not in str(s.api_key)
    assert s.api_key.get_secret_value() == secret


def test_create_app_raises_in_production_with_default_api_key(monkeypatch):
    # Import first (module-level `app = create_app()` runs here under the
    # ambient local env), THEN switch to prod and clear the settings cache the
    # import repopulated — otherwise get_settings() returns cached local values.
    from src.api.app import create_app

    from src.config import DEFAULT_DEV_API_KEY

    monkeypatch.setenv("TT_ENV", "production")
    monkeypatch.setenv("API_KEY", DEFAULT_DEV_API_KEY)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError):
        create_app()
