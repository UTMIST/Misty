import pytest

from src.config import (
    DEFAULT_DEV_API_KEY,
    DEFAULT_DEV_HMAC_SECRET,
    Settings,
    verify_production_secrets,
)


def _prod(**overrides) -> Settings:
    base = dict(
        vf_env="staging",
        api_key="a-strong-api-key",
        code_hmac_secret="a-strong-hmac-secret",
        database_url="postgresql+psycopg://user:pass@prod-host:5432/verification",
        email_backend="gmail",
        gmail_sender="noreply@utmist.ca",
        gmail_credentials_json="base64creds",
    )
    base.update(overrides)
    return Settings(**base)


def test_local_env_always_passes():
    # Dev defaults are acceptable locally — the guard is a no-op.
    verify_production_secrets(Settings(vf_env="local"))


def test_prod_passes_with_strong_config():
    verify_production_secrets(_prod())  # must not raise


def test_prod_rejects_dev_database_url():
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        verify_production_secrets(
            _prod(
                database_url="postgresql+psycopg://verification:dev_password@localhost:5434/verification"
            )
        )


def test_prod_rejects_fake_email_backend():
    with pytest.raises(RuntimeError, match="EMAIL_BACKEND=fake"):
        verify_production_secrets(_prod(email_backend="fake"))


def test_prod_resend_backend_passes_with_key_and_from():
    verify_production_secrets(
        _prod(email_backend="resend", resend_api_key="re_live_key", email_from="x@utmist.ca")
    )


def test_prod_rejects_resend_without_api_key():
    # Also guards the SecretStr emptiness check. pydantic 2.x's SecretStr
    # defines __len__ (not __bool__), so a bare `not settings.resend_api_key`
    # happens to still work today; this test is what would catch it if that
    # implementation detail ever changed.
    with pytest.raises(RuntimeError, match="RESEND_API_KEY"):
        verify_production_secrets(
            _prod(email_backend="resend", resend_api_key="", email_from="x@utmist.ca")
        )


def test_prod_rejects_resend_without_from():
    with pytest.raises(RuntimeError, match="EMAIL_FROM"):
        verify_production_secrets(
            _prod(email_backend="resend", resend_api_key="re_live_key", email_from="")
        )


def test_prod_rejects_dev_api_key():
    # Also guards the SecretStr equality trap: SecretStr never == str, so a bare
    # `settings.api_key == DEFAULT_DEV_API_KEY` would silently never fire.
    with pytest.raises(RuntimeError, match="API_KEY"):
        verify_production_secrets(_prod(api_key=DEFAULT_DEV_API_KEY))


def test_prod_rejects_dev_hmac_secret():
    with pytest.raises(RuntimeError, match="CODE_HMAC_SECRET"):
        verify_production_secrets(_prod(code_hmac_secret=DEFAULT_DEV_HMAC_SECRET))


def test_prod_rejects_gmail_backend_without_credentials():
    with pytest.raises(RuntimeError, match="GMAIL_CREDENTIALS_JSON"):
        verify_production_secrets(_prod(gmail_credentials_json=""))


def test_defaults_are_local_and_dev():
    s = Settings()
    assert s.vf_env == "local"
    assert s.api_key.get_secret_value() == DEFAULT_DEV_API_KEY
    assert s.code_hmac_secret.get_secret_value() == DEFAULT_DEV_HMAC_SECRET
    assert s.resend_api_key.get_secret_value() == ""
    assert s.gmail_credentials_json.get_secret_value() == ""


def test_credentials_never_leak_via_string_conversion():
    # Regression guard — do NOT "simplify" these annotations back to str.
    # gmail_credentials_json holds a base64 Google service-account private key;
    # as a plain str it printed in full on any repr/log/traceback, and the
    # identical field in services/connectors once had a failing assertion diff
    # dump a real key into a terminal and a saved session transcript. SecretStr
    # must keep that structurally impossible: repr/str render "**********" and
    # only .get_secret_value() returns the value.
    creds = "super-secret-service-account-json-b64"
    hmac_secret = "super-secret-hmac-value"
    s = _prod(gmail_credentials_json=creds, code_hmac_secret=hmac_secret)

    for secret, field in ((creds, s.gmail_credentials_json), (hmac_secret, s.code_hmac_secret)):
        assert secret not in repr(s)
        assert secret not in str(s)
        assert secret not in str(field)
        assert secret not in repr(field)
        assert field.get_secret_value() == secret
