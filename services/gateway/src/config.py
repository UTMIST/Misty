from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DEV_API_KEY = "dev-api-key-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://gateway:dev_password@localhost:5435/gateway"
    # Env grace-period admin key for platform_auth's bootstrap path (local dev).
    api_key: str = DEFAULT_DEV_API_KEY
    # Outbound: the gateway's own team-tracking key (scoped identifiers:read).
    directory_base_url: str = "http://localhost:8000"
    directory_api_key: str = DEFAULT_DEV_API_KEY
    gateway_env: Literal["local", "staging", "production"] = "local"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def verify_production_secrets(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if settings.gateway_env == "local":
        return
    insecure: list[str] = []
    if settings.api_key == DEFAULT_DEV_API_KEY:
        insecure.append("API_KEY")
    if settings.directory_api_key == DEFAULT_DEV_API_KEY:
        insecure.append("DIRECTORY_API_KEY")
    if insecure:
        raise RuntimeError(
            f"Refusing to start in gateway_env={settings.gateway_env!r}: "
            f"{', '.join(insecure)} still set to the built-in dev default."
        )
