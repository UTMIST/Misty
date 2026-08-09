from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# The built-in dev secret. Only acceptable when tt_env == "local"; any other
# environment must override API_KEY with a strong value (see
# verify_production_secrets). Shipping this default to staging/production would
# accept a publicly-known admin key via the env-bootstrap auth path.
DEFAULT_DEV_API_KEY = "dev-api-key-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+psycopg://team_tracking:dev_password@localhost:5433/team_tracking"
    )
    # The env-bootstrap admin key. This is SecretStr, not str: a plain str
    # field prints in full on any repr/diff/traceback (in connectors, a failing
    # assertion once dumped a real credential to a terminal and a session
    # transcript). SecretStr makes that structurally impossible — repr/str
    # always render "**********" — so don't revert this to str. Only the
    # boundaries that must compare the raw value (verify_production_secrets
    # below, and src/api/auth.py handing it to platform_auth) should ever call
    # .get_secret_value() on it.
    api_key: SecretStr = SecretStr(DEFAULT_DEV_API_KEY)
    tt_env: Literal["local", "staging", "production"] = "local"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def verify_production_secrets(settings: Settings | None = None) -> None:
    """Fail fast if a non-local environment is still using built-in dev secrets.

    The env-bootstrap auth path (src/api/auth.py) grants admin scope to anyone
    presenting `api_key`. When that value is the committed default, the key is
    publicly known — so we refuse to boot outside `local`. Called from
    create_app(), so a misconfigured deploy dies at startup, not on first
    request.
    """
    settings = settings or get_settings()
    if settings.tt_env == "local":
        return
    insecure: list[str] = []
    # .get_secret_value() is required: SecretStr never compares equal to a str,
    # so `settings.api_key == DEFAULT_DEV_API_KEY` would silently be False
    # forever and this guard would stop firing without any test noticing.
    if settings.api_key.get_secret_value() == DEFAULT_DEV_API_KEY:
        insecure.append("API_KEY")
    if insecure:
        raise RuntimeError(
            f"Refusing to start in tt_env={settings.tt_env!r}: "
            f"{', '.join(insecure)} still set to the built-in dev default. "
            "Set a strong, unique value via environment variables."
        )
