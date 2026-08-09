"""Builds the meeting service's auth dependencies from platform_auth.

Keys are resolved from the config-seeded in-memory store (no DB) or the env
bootstrap key. No dev-spoof — this is a service-to-service API.
"""

from platform_auth import build_auth

from src.api.deps import get_key_store
from src.config import get_settings

_deps = build_auth(
    get_key_store,
    envelope="meeting_",
    # build_auth wants a plain `str | None` and compares it against a presented
    # header, so unwrap here: the SecretStr boundary stops at this edge.
    get_env_key=lambda: get_settings().api_key.get_secret_value(),
    is_prod=lambda: get_settings().meeting_env == "production",
    enable_dev_spoof=False,
    audit_logger_name="meeting.audit",
)

require_api_key = _deps.require_api_key
require_scope = _deps.require_scope
get_actor = _deps.get_actor
