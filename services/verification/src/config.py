from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Built-in dev secrets. Only acceptable when vf_env == "local"; any other
# environment must override them (see verify_production_secrets). Kept as plain
# str so the fail-fast comparisons below stay readable — the fields wrap them.
DEFAULT_DEV_API_KEY = "dev-api-key-change-me"
DEFAULT_DEV_HMAC_SECRET = "dev-hmac-secret-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://verification:dev_password@localhost:5434/verification"
    api_key: SecretStr = SecretStr(DEFAULT_DEV_API_KEY)
    code_hmac_secret: SecretStr = SecretStr(DEFAULT_DEV_HMAC_SECRET)
    vf_env: Literal["local", "staging", "production"] = "local"
    email_backend: Literal["fake", "resend", "gmail"] = "fake"
    email_from: str = ""  # sender address, e.g. "UTMIST <noreply@utmist.ca>"
    resend_api_key: SecretStr = SecretStr("")
    gmail_sender: str = ""
    # base64-encoded Google service-account JSON — a private key. This is
    # SecretStr, not str: a plain str field prints in full on any
    # repr/diff/traceback (the identical field in services/connectors once had a
    # failing assertion dump a real key into a terminal and a saved transcript).
    # SecretStr makes that structurally impossible — repr/str always render
    # "**********" — so don't revert this to str. Only the edge that hands the
    # raw value to GmailSender (src/api/deps.py) should call .get_secret_value().
    gmail_credentials_json: SecretStr = SecretStr("")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def verify_production_secrets(settings: Settings | None = None) -> None:
    """Fail fast if a non-local environment still uses built-in dev secrets."""
    settings = settings or get_settings()
    if settings.vf_env == "local":
        return
    problems: list[str] = []
    # .get_secret_value() is load-bearing on the equality checks below: a
    # SecretStr never compares equal to a str, so dropping the unwrap would
    # silently disable the guard rather than fail loudly. The emptiness checks
    # unwrap too — pydantic 2.x's SecretStr happens to define __len__ (so an
    # empty one is falsy today), but it defines no __bool__ and that is an
    # implementation detail we shouldn't stake a startup guard on.
    if settings.api_key.get_secret_value() == DEFAULT_DEV_API_KEY:
        problems.append("API_KEY")
    if settings.code_hmac_secret.get_secret_value() == DEFAULT_DEV_HMAC_SECRET:
        problems.append("CODE_HMAC_SECRET")
    if "dev_password@localhost" in settings.database_url:
        problems.append("DATABASE_URL")
    if settings.email_backend == "fake":
        # The fake sender silently drops mail — a non-local deploy on it would
        # 202 every request while delivering nothing.
        problems.append("EMAIL_BACKEND=fake")
    if settings.email_backend == "resend" and not settings.resend_api_key.get_secret_value():
        problems.append("RESEND_API_KEY")
    if settings.email_backend == "resend" and not settings.email_from:
        problems.append("EMAIL_FROM")
    if settings.email_backend == "gmail" and not settings.gmail_sender:
        problems.append("GMAIL_SENDER")
    if settings.email_backend == "gmail" and not settings.gmail_credentials_json.get_secret_value():
        problems.append("GMAIL_CREDENTIALS_JSON")
    if problems:
        raise RuntimeError(
            f"Refusing to start in vf_env={settings.vf_env!r}: "
            f"{', '.join(problems)} — still a built-in dev default, missing, or unsafe "
            "for a non-local deployment. Set strong, unique values via environment variables."
        )
