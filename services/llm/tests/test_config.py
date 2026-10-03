import pytest
from pydantic import ValidationError

from src.config import DEFAULT_DEV_API_KEY, Settings, verify_production_secrets


@pytest.mark.parametrize("field", ["request_timeout_s", "embed_max_request_chars"])
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_limits_rejected(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_defaults_are_local_and_dev():
    s = Settings()
    assert s.llm_env == "local"
    assert s.api_key.get_secret_value() == DEFAULT_DEV_API_KEY
    assert s.consumer_keys.get_secret_value() == ""
    assert s.embed_model == "openai-embed-3-small"
    assert s.openai_api_key.get_secret_value() == ""
    assert s.embed_max_request_chars == 400_000
    assert s.request_timeout_s == 60.0


def test_local_env_never_raises():
    verify_production_secrets(Settings(llm_env="local", openai_api_key=""))  # no raise


def test_production_with_default_key_raises():
    s = Settings(
        llm_env="production",
        api_key=DEFAULT_DEV_API_KEY,
        aws_region="us-east-1",
        openai_api_key="synthetic-openai-key",
    )
    with pytest.raises(RuntimeError, match="API_KEY"):
        verify_production_secrets(s)


@pytest.mark.parametrize("env", ["staging", "production"])
def test_dev_api_key_guard_still_fires_after_secretstr_conversion(env):
    # Regression guard for a SILENT failure mode. api_key is a SecretStr, and a
    # SecretStr never compares equal to a str: if verify_production_secrets is
    # ever "simplified" back to `settings.api_key == DEFAULT_DEV_API_KEY`, that
    # comparison is False forever, the service happily boots to staging/prod
    # with the committed dev secret, and nothing else in this suite notices.
    # This test is the thing that notices.
    s = Settings(
        llm_env=env,
        api_key=DEFAULT_DEV_API_KEY,
        aws_region="us-east-1",
        openai_api_key="synthetic-openai-key",
    )
    with pytest.raises(RuntimeError, match="API_KEY"):
        verify_production_secrets(s)


def test_production_with_empty_region_raises():
    s = Settings(
        llm_env="production",
        api_key="strong-unique-key",
        aws_region="",
        openai_api_key="synthetic-openai-key",
    )
    with pytest.raises(RuntimeError, match="AWS_REGION"):
        verify_production_secrets(s)


@pytest.mark.parametrize("env", ["staging", "production"])
def test_non_local_fully_configured_ok(env):
    # Non-local deploys need OPENAI_API_KEY as well as the chat credentials.
    s = Settings(
        llm_env=env,
        api_key="strong-unique-key",
        aws_region="us-east-1",
        openai_api_key="synthetic-openai-key",
    )
    verify_production_secrets(s)  # no raise


@pytest.mark.parametrize("env", ["staging", "production"])
@pytest.mark.parametrize("chat_provider", ["bedrock", "bedrock-converse"])
def test_non_local_requires_openai_key_with_either_chat_provider(env, chat_provider):
    s = Settings(
        llm_env=env,
        llm_provider=chat_provider,
        api_key="strong-unique-key",
        aws_region="us-east-1",
        openai_api_key="",
    )
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        verify_production_secrets(s)


def test_api_key_never_leaks_via_string_conversion():
    # Regression: api_key used to be a plain str, so a failing assertion diff
    # (or any repr/log/traceback) printed the bootstrap admin key in full — the
    # exact way connectors' google_credentials_json leaked a real private key
    # into a terminal and a session transcript. SecretStr must keep that
    # structurally impossible; do NOT "simplify" the annotation back to str.
    secret = "super-secret-bootstrap-admin-key"
    s = Settings(api_key=secret)

    assert secret not in repr(s)
    assert secret not in str(s)
    assert secret not in str(s.api_key)
    assert s.api_key.get_secret_value() == secret


def test_consumer_keys_never_leaks_via_string_conversion():
    # Same regression guard for CONSUMER_KEYS. It holds argon2 hashes rather
    # than plaintext keys, but a leak still exposes an offline cracking target
    # plus the consumer roster and their scopes. Keep it SecretStr.
    secret = (
        '[{"name":"bot","prefix":"llm_abcd","key_hash":"$argon2id$v=19$fake","scopes":["chat"]}]'
    )
    s = Settings(consumer_keys=secret)

    assert secret not in repr(s)
    assert secret not in str(s)
    assert secret not in str(s.consumer_keys)
    assert s.consumer_keys.get_secret_value() == secret


def test_openai_key_never_leaks_via_string_conversion():
    secret = "synthetic-private-openai-key"
    s = Settings(openai_api_key=secret)

    assert secret not in repr(s)
    assert secret not in str(s)
    assert secret not in str(s.openai_api_key)
    assert s.openai_api_key.get_secret_value() == secret


def test_malformed_consumer_keys_fails_on_boot(monkeypatch):
    from src.api import deps
    from src.config import get_settings

    monkeypatch.setenv("CONSUMER_KEYS", "{not valid json")
    get_settings.cache_clear()
    deps._key_store.cache_clear()
    from src.api.app import create_app

    with pytest.raises(RuntimeError, match="CONSUMER_KEYS"):
        create_app()

    deps._key_store.cache_clear()
    get_settings.cache_clear()
