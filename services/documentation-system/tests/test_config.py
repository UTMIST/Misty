import pytest

from src.config import DEFAULT_DEV_API_KEY, Settings, get_settings

# The get_settings cache-clearing fixture that used to live here now lives in
# tests/conftest.py as an autouse fixture depending on _no_dotenv, so the cache
# is only ever rebuilt with the developer's real `.env` neutralized.


def test_settings_defaults():
    s = Settings()
    assert s.database_url.startswith("postgresql+psycopg://")
    assert s.directory_base_url.startswith("http")
    # .get_secret_value(), not a bare truthiness check: these fields are
    # SecretStr, and a bare `assert s.api_key` asserts almost nothing about
    # the value actually carried. Kept as a non-emptiness check rather than an
    # equality check against DEFAULT_DEV_API_KEY, because _no_dotenv only
    # neutralizes `.env` — a developer with API_KEY exported in their shell
    # would still see it here.
    assert s.api_key.get_secret_value()
    assert s.directory_api_key.get_secret_value()
    assert s.connectors_api_key.get_secret_value()


# --- docs_env -----------------------------------------------------------------


def test_docs_env_defaults_to_local(monkeypatch):
    monkeypatch.delenv("DOCS_ENV", raising=False)
    assert get_settings().docs_env == "local"


def test_docs_env_reads_from_environment(monkeypatch):
    monkeypatch.setenv("DOCS_ENV", "production")
    assert get_settings().docs_env == "production"


def test_docs_env_rejects_unknown_values(monkeypatch):
    monkeypatch.setenv("DOCS_ENV", "Production")  # capital P — typo
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        get_settings()


# --- production secret guard --------------------------------------------------


def test_verify_secrets_raises_in_production_with_default_api_key(monkeypatch):
    from src.config import DEFAULT_DEV_API_KEY, verify_production_secrets

    monkeypatch.setenv("DOCS_ENV", "production")
    monkeypatch.setenv("API_KEY", DEFAULT_DEV_API_KEY)
    monkeypatch.setenv("DIRECTORY_API_KEY", "a-strong-directory-secret")
    with pytest.raises(RuntimeError) as exc:
        verify_production_secrets()
    assert "API_KEY" in str(exc.value)


def test_verify_secrets_raises_in_production_with_default_directory_api_key(monkeypatch):
    from src.config import DEFAULT_DEV_API_KEY, verify_production_secrets

    monkeypatch.setenv("DOCS_ENV", "production")
    monkeypatch.setenv("API_KEY", "a-strong-api-secret")
    monkeypatch.setenv("DIRECTORY_API_KEY", DEFAULT_DEV_API_KEY)
    with pytest.raises(RuntimeError) as exc:
        verify_production_secrets()
    assert "DIRECTORY_API_KEY" in str(exc.value)


def test_verify_secrets_warns_in_production_with_default_connectors_api_key(monkeypatch, caplog):
    from src.config import DEFAULT_DEV_API_KEY, verify_production_secrets

    monkeypatch.setenv("DOCS_ENV", "production")
    monkeypatch.setenv("API_KEY", "a-strong-api-secret")
    monkeypatch.setenv("DIRECTORY_API_KEY", "a-strong-directory-secret")
    monkeypatch.setenv("CONNECTORS_API_KEY", DEFAULT_DEV_API_KEY)
    with caplog.at_level("WARNING"):
        verify_production_secrets()  # must not raise
    assert any("CONNECTORS_API_KEY" in record.message for record in caplog.records)


def test_verify_secrets_no_connectors_warning_in_local(monkeypatch, caplog):
    from src.config import DEFAULT_DEV_API_KEY, verify_production_secrets

    monkeypatch.setenv("DOCS_ENV", "local")
    monkeypatch.setenv("CONNECTORS_API_KEY", DEFAULT_DEV_API_KEY)
    with caplog.at_level("WARNING"):
        verify_production_secrets()  # must not raise
    assert not any("CONNECTORS_API_KEY" in record.message for record in caplog.records)


def test_verify_secrets_raises_in_staging_with_default_api_key(monkeypatch):
    from src.config import DEFAULT_DEV_API_KEY, verify_production_secrets

    monkeypatch.setenv("DOCS_ENV", "staging")
    monkeypatch.setenv("API_KEY", DEFAULT_DEV_API_KEY)
    with pytest.raises(RuntimeError):
        verify_production_secrets()


def test_verify_secrets_passes_in_local_with_default_secrets(monkeypatch):
    from src.config import DEFAULT_DEV_API_KEY, verify_production_secrets

    monkeypatch.setenv("DOCS_ENV", "local")
    monkeypatch.setenv("API_KEY", DEFAULT_DEV_API_KEY)
    monkeypatch.setenv("DIRECTORY_API_KEY", DEFAULT_DEV_API_KEY)
    verify_production_secrets()  # must not raise


def test_verify_secrets_passes_in_production_with_real_secrets(monkeypatch):
    from src.config import verify_production_secrets

    monkeypatch.setenv("DOCS_ENV", "production")
    monkeypatch.setenv("API_KEY", "a-strong-api-secret")
    monkeypatch.setenv("DIRECTORY_API_KEY", "a-strong-directory-secret")
    monkeypatch.setenv("CONNECTORS_API_KEY", "a-strong-connectors-secret")
    verify_production_secrets()  # must not raise


def test_create_app_raises_in_production_with_default_api_key(monkeypatch):
    from src.api.app import create_app

    from src.config import DEFAULT_DEV_API_KEY

    monkeypatch.setenv("DOCS_ENV", "production")
    monkeypatch.setenv("API_KEY", DEFAULT_DEV_API_KEY)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError):
        create_app()


# --- SecretStr regression guards ----------------------------------------------
#
# api_key, directory_api_key and connectors_api_key are SecretStr. The tests
# below exist so a future author cannot quietly "simplify" those annotations
# back to plain str. Two independent things would break if they did, and
# neither announces itself:
#
#   1. Leakage. A plain-str credential prints in full in any repr, log line,
#      traceback or failing-assertion diff. That is what happened in
#      services/connectors, where a real Google service-account private key was
#      dumped into a terminal and a session transcript by a failing assertion.
#
#   2. Silent loss of the boot guard. A SecretStr never compares equal to a
#      str, so if a future change reverts a field to str but leaves the
#      .get_secret_value() calls in verify_production_secrets — or, worse,
#      keeps SecretStr and drops the .get_secret_value() — the dev-default
#      comparisons evaluate False forever and the service boots to
#      staging/production with the committed dev secret. Nothing raises;
#      nothing logs. The guard tests below are the only thing that notices.


@pytest.mark.parametrize("field", ["api_key", "directory_api_key", "connectors_api_key"])
def test_credentials_never_leak_via_string_conversion(field):
    secret = f"super-secret-{field}-value"
    s = Settings(**{field: secret})

    assert secret not in repr(s)
    assert secret not in str(s)
    assert secret not in str(getattr(s, field))
    assert getattr(s, field).get_secret_value() == secret


def test_secretstr_does_not_compare_equal_to_plain_str():
    # Pins the exact footgun that makes the guards below load-bearing: this is
    # why verify_production_secrets must go through .get_secret_value().
    s = Settings(api_key=DEFAULT_DEV_API_KEY)
    assert s.api_key != DEFAULT_DEV_API_KEY
    assert s.api_key.get_secret_value() == DEFAULT_DEV_API_KEY


def test_guard_still_fires_for_default_api_key_after_secretstr():
    from src.config import verify_production_secrets

    s = Settings(
        docs_env="production",
        api_key=DEFAULT_DEV_API_KEY,
        directory_api_key="a-strong-directory-secret",
        connectors_api_key="a-strong-connectors-secret",
    )
    with pytest.raises(RuntimeError, match="API_KEY"):
        verify_production_secrets(s)


def test_guard_still_fires_for_default_directory_api_key_after_secretstr():
    from src.config import verify_production_secrets

    s = Settings(
        docs_env="production",
        api_key="a-strong-api-secret",
        directory_api_key=DEFAULT_DEV_API_KEY,
        connectors_api_key="a-strong-connectors-secret",
    )
    with pytest.raises(RuntimeError, match="DIRECTORY_API_KEY"):
        verify_production_secrets(s)


def test_guard_still_warns_for_default_connectors_api_key_after_secretstr(caplog):
    # connectors is a soft dependency, so this one is documented as
    # warn-not-raise (see verify_production_secrets) — and the code does what
    # the docstring says. The warning is still the only signal that Google
    # sources are misconfigured, so it must survive the SecretStr conversion.
    from src.config import verify_production_secrets

    s = Settings(
        docs_env="production",
        api_key="a-strong-api-secret",
        directory_api_key="a-strong-directory-secret",
        connectors_api_key=DEFAULT_DEV_API_KEY,
    )
    with caplog.at_level("WARNING"):
        verify_production_secrets(s)  # must not raise
    assert any("CONNECTORS_API_KEY" in r.message for r in caplog.records)


def test_guard_warning_never_prints_the_connectors_key(caplog):
    # The warning names the env var, never the value.
    from src.config import verify_production_secrets

    s = Settings(
        docs_env="production",
        api_key="a-strong-api-secret",
        directory_api_key="a-strong-directory-secret",
        connectors_api_key=DEFAULT_DEV_API_KEY,
    )
    with caplog.at_level("WARNING"):
        verify_production_secrets(s)
    assert not any(DEFAULT_DEV_API_KEY in r.getMessage() for r in caplog.records)


def test_boot_refusal_message_never_prints_the_secret():
    # RuntimeError text is the single most likely place for a credential to
    # reach a deploy log, so assert it names env vars only.
    from src.config import verify_production_secrets

    s = Settings(
        docs_env="production",
        api_key=DEFAULT_DEV_API_KEY,
        directory_api_key=DEFAULT_DEV_API_KEY,
    )
    with pytest.raises(RuntimeError) as exc:
        verify_production_secrets(s)
    assert DEFAULT_DEV_API_KEY not in str(exc.value)
