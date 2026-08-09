from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Built-in dev secret. Only acceptable when connectors_env == "local"; any other
# environment must override API_KEY (see verify_production_secrets), because the
# env-bootstrap auth path grants admin scope to whoever presents this value.
DEFAULT_DEV_API_KEY = "dev-api-key-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    connectors_env: Literal["local", "staging", "production"] = "local"
    # Env-bootstrap key granting admin scope. SecretStr for the same reason as
    # google_credentials_json below: a plain str prints in full on any
    # repr/diff/traceback. Only the auth factory boundary (src/api/auth.py)
    # should call .get_secret_value() on it — plus verify_production_secrets,
    # which must compare the unwrapped value (a SecretStr never compares equal
    # to a str, so comparing the wrapper would silently disable that guard).
    api_key: SecretStr = SecretStr(DEFAULT_DEV_API_KEY)
    # JSON array of consumer keys: [{"name","prefix","key_hash","scopes"?}].
    # Holds argon2 hashes rather than plaintext keys, but it is still
    # credential material, so it is SecretStr too. Only the key-store boundary
    # (src/api/deps.py) should call .get_secret_value() on it.
    consumer_keys: SecretStr = SecretStr("")
    # base64-encoded Google service-account JSON — a private key. Empty is a
    # valid running state: Google fetches then fail as SourceNotConfigured
    # (503) while the rest of the service keeps serving. This is SecretStr,
    # not str: a plain str field prints in full on any repr/diff/traceback
    # (a failing assertion once dumped a real key to a CI log). SecretStr
    # makes that structurally impossible — repr/str always render
    # "**********" — so don't revert this to str. Only the registry (the
    # boundary that hands the raw value to GoogleSource) should ever call
    # .get_secret_value() on it.
    google_credentials_json: SecretStr = SecretStr("")
    # Transport guard only. documentation-system's own clamp decides what is
    # actually stored; this just stops a pathological file becoming a huge
    # HTTP response. Deliberately set ABOVE documentation-system's
    # MAX_CONTENT_CHARS (1,000,000) so this guard never trips first — the
    # catalog's clamp must remain the authoritative one, or its "content
    # truncated" warning can never fire.
    max_content_chars: int = Field(default=1_200_000, gt=0)
    request_timeout_s: float = Field(default=30.0, gt=0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def verify_production_secrets(settings: Settings | None = None) -> None:
    """Fail fast if a non-local environment still uses the built-in dev secret.

    Google credentials are deliberately NOT checked here: a connectors service
    that cannot reach Drive is degraded, not broken, and must still start and
    serve /health so it can be deployed before the service account exists.
    """
    settings = settings or get_settings()
    if settings.connectors_env == "local":
        return
    insecure: list[str] = []
    if settings.api_key.get_secret_value() == DEFAULT_DEV_API_KEY:
        insecure.append("API_KEY")
    if insecure:
        raise RuntimeError(
            f"Refusing to start in connectors_env={settings.connectors_env!r}: "
            f"{', '.join(insecure)} still set to the built-in dev default. "
            "Set a strong, unique value via environment variables."
        )
