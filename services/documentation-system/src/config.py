import logging
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# The built-in dev secret, shared by the inbound API key and the outbound
# directory key. Only acceptable when docs_env == "local"; any other
# environment must override these with strong values (see
# verify_production_secrets). Shipping the default `api_key` to
# staging/production would accept a publicly-known admin key via the
# env-bootstrap auth path.
#
# Deliberately a plain str, not a SecretStr: it is a committed, publicly-known
# constant with nothing to hide, and the comparisons in
# verify_production_secrets read cleanest against a str. It is wrapped in
# SecretStr at each field default below.
DEFAULT_DEV_API_KEY = "dev-api-key-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://docs:dev_password@localhost:5434/docs"
    # The three credentials below are SecretStr, not str: a plain str field
    # prints in full on any repr/diff/traceback. That is not hypothetical —
    # an identically-shaped plain-str credential in services/connectors leaked
    # a real Google service-account private key into a terminal and a session
    # transcript via a failing pytest assertion diff. SecretStr makes that
    # structurally impossible (repr/str always render "**********"), so don't
    # "simplify" these back to str. Only the boundaries that hand the raw
    # value to something that needs a str should call .get_secret_value():
    # src/api/auth.py (env-bootstrap key comparison), src/api/deps.py (the
    # directory HTTP client), src/fetch/registry.py (the connectors fetchers),
    # and verify_production_secrets below.
    api_key: SecretStr = SecretStr(DEFAULT_DEV_API_KEY)
    directory_base_url: str = "http://localhost:8000"
    directory_api_key: SecretStr = SecretStr(DEFAULT_DEV_API_KEY)
    connectors_base_url: str = "http://localhost:8005"
    connectors_api_key: SecretStr = SecretStr(DEFAULT_DEV_API_KEY)
    docs_env: Literal["local", "staging", "production"] = "local"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def verify_production_secrets(settings: Settings | None = None) -> None:
    """Fail fast if a non-local environment is still using built-in dev secrets.

    `api_key` is the inbound env-bootstrap admin key (src/api/auth.py); when it
    is the committed default it is publicly known. `directory_api_key` is an
    outbound credential this service presents to the directory service, which
    it depends on hard: every doc's owner and every access decision is
    resolved against directory, so a default there is a broken prod config
    and must block startup.

    `connectors_api_key` is different in kind, not just degree: connectors is
    a soft dependency. It only backs the four Google-backed sources (`gdocs`,
    `gsheets`, `gslides`, `gdrive`) — `web` and `github` fetch in-process and
    never touch it — and `ingest_doc` is designed to turn a connectors
    `FetchError` into a per-doc warning while still cataloguing the doc (see
    src/ingest.py). Losing connectors means "no Google content," not "broken
    catalog," so a default `connectors_api_key` gets a startup warning
    instead of a boot refusal (see below). This mirrors connectors' own
    `verify_production_secrets` (services/connectors/src/config.py), which
    deliberately excludes `GOOGLE_CREDENTIALS_JSON` for the same reason: a
    connectors service that can't reach Drive is degraded, not broken, and
    must still boot.

    Called from create_app(), so a misconfigured deploy dies at startup, not
    on first request.

    All three checks below MUST go through .get_secret_value(). A SecretStr
    never compares equal to a str, so `settings.api_key == DEFAULT_DEV_API_KEY`
    would silently evaluate False forever — the service would boot happily to
    staging/production with the committed dev secret and nothing would say so.
    """
    settings = settings or get_settings()
    if settings.docs_env == "local":
        return
    insecure: list[str] = []
    if settings.api_key.get_secret_value() == DEFAULT_DEV_API_KEY:
        insecure.append("API_KEY")
    if settings.directory_api_key.get_secret_value() == DEFAULT_DEV_API_KEY:
        insecure.append("DIRECTORY_API_KEY")
    if insecure:
        raise RuntimeError(
            f"Refusing to start in docs_env={settings.docs_env!r}: "
            f"{', '.join(insecure)} still set to the built-in dev default. "
            "Set a strong, unique value via environment variables."
        )
    if settings.connectors_api_key.get_secret_value() == DEFAULT_DEV_API_KEY:
        logger.warning(
            "CONNECTORS_API_KEY is still set to the built-in dev default in "
            "docs_env=%r. This service will still start, but Google-source "
            "fetches (gdocs, gsheets, gslides, gdrive) will fail against the "
            "real connectors service and be recorded as per-doc ingest "
            "warnings rather than content. Set a strong, unique value via "
            "environment variables to enable Google-source content.",
            settings.docs_env,
        )
