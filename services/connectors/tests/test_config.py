import pytest

from pydantic import SecretStr

from src.config import DEFAULT_DEV_API_KEY, Settings, verify_production_secrets


def test_defaults_are_local_and_dev():
    s = Settings()
    assert s.connectors_env == "local"
    assert s.api_key.get_secret_value() == DEFAULT_DEV_API_KEY
    assert s.consumer_keys.get_secret_value() == ""
    assert s.google_credentials_json.get_secret_value() == ""


@pytest.mark.parametrize("dev_key", [DEFAULT_DEV_API_KEY, SecretStr(DEFAULT_DEV_API_KEY)])
def test_production_with_dev_api_key_is_refused(dev_key):
    # api_key is a SecretStr, and a SecretStr never compares equal to a str.
    # If verify_production_secrets ever stops unwrapping it, this check
    # silently evaluates False and the service boots to production with the
    # committed dev secret — so this test must keep raising, whether the field
    # was populated from a raw str (env var) or an explicit SecretStr.
    s = Settings(connectors_env="production", api_key=dev_key)
    with pytest.raises(RuntimeError, match="API_KEY"):
        verify_production_secrets(s)


def test_production_with_strong_key_passes():
    s = Settings(connectors_env="production", api_key="a-strong-unique-value")
    verify_production_secrets(s)


def test_missing_google_credentials_never_blocks_startup():
    # A connectors service that cannot reach Drive is degraded, not broken.
    s = Settings(connectors_env="production", api_key="strong", google_credentials_json="")
    verify_production_secrets(s)


def test_google_credentials_json_never_leaks_via_string_conversion():
    # Regression: google_credentials_json used to be a plain str, so a failing
    # assertion diff (or any repr/log/traceback) printed the raw service-
    # account key in full. SecretStr must keep that structurally impossible.
    secret = "super-secret-service-account-json-b64"
    s = Settings(google_credentials_json=secret)

    assert secret not in repr(s)
    assert secret not in str(s)
    assert secret not in str(s.google_credentials_json)
    assert s.google_credentials_json.get_secret_value() == secret


def test_api_key_never_leaks_via_string_conversion():
    # Same guarantee as google_credentials_json: the env-bootstrap key carries
    # admin scope, so it must never render in a repr/log/assertion diff.
    secret = "super-secret-bootstrap-api-key"
    s = Settings(api_key=secret)

    assert secret not in repr(s)
    assert secret not in str(s)
    assert secret not in str(s.api_key)
    assert s.api_key.get_secret_value() == secret


def test_consumer_keys_never_leaks_via_string_conversion():
    # consumer_keys holds argon2 hashes rather than plaintext keys, but a hash
    # is still credential material and must not render either.
    secret = '[{"name":"docs","prefix":"abc12345","key_hash":"$argon2id$super-secret"}]'
    s = Settings(consumer_keys=secret)

    assert secret not in repr(s)
    assert secret not in str(s)
    assert secret not in str(s.consumer_keys)
    assert s.consumer_keys.get_secret_value() == secret
