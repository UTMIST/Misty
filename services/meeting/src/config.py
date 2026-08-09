from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Built-in dev secret. Only acceptable when meeting_env == "local"; any other
# environment must override API_KEY (see verify_production_secrets), because the
# env-bootstrap auth path grants admin scope to whoever presents this value.
DEFAULT_DEV_API_KEY = "dev-api-key-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    meeting_env: Literal["local", "staging", "production"] = "local"
    # These three are SecretStr, not str: a plain str field prints in full on
    # any repr/diff/traceback (in the sibling connectors service a failing
    # assertion once dumped a real credential to a terminal and a session
    # transcript). SecretStr makes that structurally impossible -- repr/str
    # always render "**********" -- so don't revert them to str. Only the
    # edges that hand the raw value to a consumer (auth.py, deps.py,
    # wiring.py, the WS env-key check in routers/meetings.py, and
    # verify_production_secrets below) should call .get_secret_value().
    api_key: SecretStr = SecretStr(DEFAULT_DEV_API_KEY)
    # JSON array of consumer keys: [{"name","prefix","key_hash","scopes"?}].
    # Holds argon2 *hashes*, not plaintext, so it is the least sensitive of the
    # three -- a hash is not directly usable -- but it is still credential
    # material and issue #162 puts it in scope.
    consumer_keys: SecretStr = SecretStr("")
    aws_region: str = ""
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    request_timeout_s: float = Field(default=60.0, gt=0)
    # Level for this service's own loggers (see logging_setup.configure_logging).
    # INFO by default: the volume is a startup note plus per-meeting summaries,
    # not per-request chatter, and the alternative is losing them entirely.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Safety cap (see sessions.py MeetingSession.feed): once a meeting exceeds
    # this many ms, further audio frames are dropped. This is a BACKSTOP, not
    # the normal end condition -- recordings usually end on /record stop or
    # auto-stop-on-empty (the bot ends a recording when everyone leaves the voice
    # channel). Audio is streamed out rather than buffered, so this bounds how
    # long one meeting can hold an AWS stream open rather than memory. Set
    # MAX_MEETING_MS to another value, or None, to change or disable it.
    max_meeting_ms: int | None = Field(default=14_400_000, gt=0)  # 4 hours
    # How long a session survives an abrupt WS disconnect (see
    # routers/meetings.py). A dropped socket does NOT mean the meeting is lost:
    # the transcript is already assembled server-side, so the session is HELD
    # for this long and POST /stop can still finalize it into minutes. Two
    # production meetings were lost in one morning because the disconnect path
    # discarded immediately and the bot had nothing left to claim.
    # Set DISCONNECT_GRACE_S=0 to restore the old discard-immediately behavior.
    disconnect_grace_s: float = Field(default=60.0, ge=0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def verify_production_secrets(settings: Settings | None = None) -> None:
    """Fail fast if a non-local environment is still misconfigured.

    Called from create_app(), so a bad deploy dies at startup, not first request.
    """
    settings = settings or get_settings()
    if settings.meeting_env == "local":
        return
    insecure: list[str] = []
    # .get_secret_value() is load-bearing on the api_key check: a SecretStr is
    # never == a str, so unwrapped this comparison would silently be False
    # forever and the guard would stop firing while the service kept booting.
    # The llm_api_key emptiness check below happens to still work unwrapped on
    # pydantic 2.13 (SecretStr defines no __bool__ but does define __len__, so
    # an empty one is falsy) -- that's an implementation detail, not a
    # documented guarantee, so unwrap there too rather than depend on it.
    if settings.api_key.get_secret_value() == DEFAULT_DEV_API_KEY:
        insecure.append("API_KEY")
    if not settings.aws_region:
        insecure.append("AWS_REGION")
    if not settings.llm_base_url:
        insecure.append("LLM_BASE_URL")
    if not settings.llm_api_key.get_secret_value():
        insecure.append("LLM_API_KEY")
    if insecure:
        raise RuntimeError(
            f"Refusing to start in meeting_env={settings.meeting_env!r}: "
            f"{', '.join(insecure)} not set to a real value."
        )
