from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Built-in dev secret. Only acceptable when llm_env == "local"; any other
# environment must override API_KEY (see verify_production_secrets), because the
# env-bootstrap auth path grants admin scope to whoever presents this value.
DEFAULT_DEV_API_KEY = "dev-api-key-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_env: Literal["local", "staging", "production"] = "local"
    # The env-bootstrap key, presented verbatim as X-API-Key to get admin scope.
    # SecretStr, not str: a plain str field prints in full on any
    # repr/diff/log/traceback (in connectors, a failing assertion diff once
    # dumped a real credential to a terminal and a session transcript).
    # SecretStr makes that structurally impossible — repr/str always render
    # "**********" — so don't revert this to str. DEFAULT_DEV_API_KEY stays a
    # plain str constant; only the field default is wrapped. Only the auth
    # boundary (src/api/auth.py) and verify_production_secrets below should
    # call .get_secret_value() on it.
    api_key: SecretStr = SecretStr(DEFAULT_DEV_API_KEY)
    # JSON array of consumer keys: [{"name","prefix","key_hash","scopes"?}].
    # These are argon2 *hashes*, not plaintext keys, so this is the least
    # sensitive of the credential fields here — a hash is not directly usable
    # to authenticate. It is still credential material (issue #162 scopes it
    # in), and leaking it hands an attacker an offline cracking target plus the
    # roster of consumers and their scopes. Only src/api/deps.py, which hands
    # the raw JSON to key_store_from_config, should call .get_secret_value().
    consumer_keys: SecretStr = SecretStr("")
    llm_provider: str = "bedrock-converse"
    llm_model: str = "claude-sonnet-4-6"
    aws_region: str = ""
    request_timeout_s: float = Field(default=60.0, gt=0)
    thinking_default: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def verify_production_secrets(settings: Settings | None = None) -> None:
    """Fail fast if a non-local environment is still misconfigured.

    Called from create_app(), so a bad deploy dies at startup, not first request.
    """
    settings = settings or get_settings()
    if settings.llm_env == "local":
        return
    insecure: list[str] = []
    # .get_secret_value() is load-bearing: SecretStr never compares equal to a
    # str, so `settings.api_key == DEFAULT_DEV_API_KEY` would silently be False
    # forever and this fail-fast guard would stop firing without any test noticing.
    if settings.api_key.get_secret_value() == DEFAULT_DEV_API_KEY:
        insecure.append("API_KEY")
    if not settings.aws_region:
        insecure.append("AWS_REGION")
    if insecure:
        raise RuntimeError(
            f"Refusing to start in llm_env={settings.llm_env!r}: "
            f"{', '.join(insecure)} not set to a real value."
        )
