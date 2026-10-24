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
    # Managed Google Groups (Cloud Identity). All five empty = sync disabled;
    # a partial set refuses to boot (see verify_production_secrets). The OAuth
    # grant is a user refresh token for the group-owning account, not a
    # service account — see docs/DEPLOYMENT.md.
    google_groups_customer_id: str = ""
    google_groups_domain: str = ""
    google_oauth_client_id: str = ""
    google_oauth_client_secret: SecretStr = SecretStr("")
    google_oauth_refresh_token: SecretStr = SecretStr("")

    def google_groups_fields(self) -> dict[str, bool]:
        """Env var name -> whether it is set."""
        return {
            "GOOGLE_GROUPS_CUSTOMER_ID": bool(self.google_groups_customer_id),
            "GOOGLE_GROUPS_DOMAIN": bool(self.google_groups_domain),
            "GOOGLE_OAUTH_CLIENT_ID": bool(self.google_oauth_client_id),
            "GOOGLE_OAUTH_CLIENT_SECRET": bool(self.google_oauth_client_secret.get_secret_value()),
            "GOOGLE_OAUTH_REFRESH_TOKEN": bool(self.google_oauth_refresh_token.get_secret_value()),
        }


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
    google = settings.google_groups_fields()
    if any(google.values()) and not all(google.values()):
        missing = ", ".join(k for k, v in google.items() if not v)
        raise RuntimeError(f"Google Groups is partially configured; also set: {missing}")
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
