from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# The built-in dev secret for the gateway's OUTBOUND team-tracking key. Only
# acceptable when gateway_env == "local"; any other environment must override
# DIRECTORY_API_KEY with the real issued key (see verify_production_secrets).
DEFAULT_DEV_API_KEY = "dev-api-key-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://gateway:dev_password@localhost:5435/gateway"

    # NOTE: there is deliberately no inbound `api_key` here. Every other service
    # carries one to feed platform_auth's env-bootstrap path; the gateway does
    # not, because that path grants ADMIN_SCOPE and this is the only service
    # reachable from the internet. See src/api/auth.py for the full reasoning.

    # Outbound: the gateway's own team-tracking key (scoped identifiers:read).
    # SecretStr, not str: a plain str field prints in full on any repr/diff/
    # traceback, and this is a live credential for the private directory.
    # SecretStr makes that structurally impossible — repr/str always render
    # "**********" — so don't revert this to str. Only the two boundaries that
    # must see the raw value (verify_production_secrets below, and
    # src/api/deps.py building the outbound header) call .get_secret_value().
    directory_base_url: str = "http://localhost:8000"
    directory_api_key: SecretStr = SecretStr(DEFAULT_DEV_API_KEY)

    gateway_env: Literal["local", "staging", "production"] = "local"

    # Whether an upstream proxy sets X-Forwarded-For / X-Real-IP. Off by default
    # so a direct deploy can't be fed a spoofed client IP; the Railway deploy
    # sets TRUST_PROXY_HEADERS=true. Read by the per-IP rate limiter, which is
    # only as good as its notion of "who is calling" (see src/api/ratelimit.py).
    trust_proxy_headers: bool = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def verify_production_secrets(settings: Settings | None = None) -> None:
    """Fail fast if a non-local environment is still using built-in dev secrets.

    Called from create_app(), so a misconfigured deploy dies at startup rather
    than on the first request that needs the directory.
    """
    settings = settings or get_settings()
    if settings.gateway_env == "local":
        return
    insecure: list[str] = []
    # .get_secret_value() is required: SecretStr never compares equal to a str,
    # so `settings.directory_api_key == DEFAULT_DEV_API_KEY` would silently be
    # False forever and this guard would stop firing without any test noticing.
    # tests/test_config.py pins exactly that.
    if settings.directory_api_key.get_secret_value() == DEFAULT_DEV_API_KEY:
        insecure.append("DIRECTORY_API_KEY")
    if insecure:
        raise RuntimeError(
            f"Refusing to start in gateway_env={settings.gateway_env!r}: "
            f"{', '.join(insecure)} still set to the built-in dev default. "
            "Set a strong, unique value via environment variables."
        )
