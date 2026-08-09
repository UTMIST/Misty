import pytest

from src.config import DEFAULT_DEV_API_KEY, Settings, verify_production_secrets


def test_defaults_local():
    s = Settings(_env_file=None)
    assert s.meeting_env == "local"
    assert s.api_key.get_secret_value() == DEFAULT_DEV_API_KEY
    assert s.consumer_keys.get_secret_value() == ""
    assert s.llm_api_key.get_secret_value() == ""


def test_max_meeting_ms_defaults_to_4h_backstop():
    # A 4h safety cap backstops worst-case memory; the normal end is /record
    # stop or auto-stop-on-empty. Can be overridden, or set to None to disable.
    s = Settings(_env_file=None)
    assert s.max_meeting_ms == 14_400_000


def test_max_meeting_ms_is_overridable_and_validated():
    assert Settings(_env_file=None, max_meeting_ms=60_000).max_meeting_ms == 60_000
    assert Settings(_env_file=None, max_meeting_ms=None).max_meeting_ms is None  # disable
    with pytest.raises(ValueError):
        Settings(_env_file=None, max_meeting_ms=0)  # gt=0 still enforced


def test_prod_requires_secrets():
    s = Settings(_env_file=None, meeting_env="production")
    with pytest.raises(RuntimeError):
        verify_production_secrets(s)


def test_prod_requires_llm_api_key():
    s = Settings(
        _env_file=None,
        meeting_env="production",
        api_key="real-key",
        aws_region="us-east-1",
        llm_base_url="https://llm.example.com",
        llm_api_key="",
    )
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        verify_production_secrets(s)


def test_prod_with_dev_api_key_is_refused():
    # Regression guard for the SecretStr conversion: a SecretStr is never == a
    # str, so an unwrapped `settings.api_key == DEFAULT_DEV_API_KEY` would
    # silently never fire again and the service would happily boot to
    # staging/production with the committed dev secret. This test is the only
    # thing that notices, since nothing else about the service breaks.
    s = Settings(
        _env_file=None,
        meeting_env="production",
        api_key=DEFAULT_DEV_API_KEY,
        aws_region="us-east-1",
        llm_base_url="https://llm.example.com",
        llm_api_key="real-llm-key",
    )
    with pytest.raises(RuntimeError, match="API_KEY"):
        verify_production_secrets(s)


def test_prod_with_real_secrets_passes():
    s = Settings(
        _env_file=None,
        meeting_env="production",
        api_key="a-strong-unique-value",
        aws_region="us-east-1",
        llm_base_url="https://llm.example.com",
        llm_api_key="real-llm-key",
    )
    verify_production_secrets(s)


def test_secrets_never_leak_via_string_conversion():
    # Regression guard: api_key/consumer_keys/llm_api_key used to be plain str,
    # so a failing assertion diff (or any repr/log/traceback) printed them in
    # full -- that is exactly how a real Google service-account key leaked out
    # of the sibling connectors service. SecretStr must keep that structurally
    # impossible, so do NOT "simplify" these annotations back to str.
    secrets_by_field = {
        "api_key": "super-secret-bootstrap-api-key",
        "consumer_keys": '[{"name":"n","prefix":"p","key_hash":"super-secret-hash"}]',
        "llm_api_key": "super-secret-llm-api-key",
    }
    s = Settings(_env_file=None, **secrets_by_field)

    for field, secret in secrets_by_field.items():
        value = getattr(s, field)
        assert secret not in repr(s)
        assert secret not in str(s)
        assert secret not in str(value)
        assert secret not in repr(value)
        assert value.get_secret_value() == secret
