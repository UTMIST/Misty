"""Builds the LLM service's auth dependencies from platform_auth.

Keys are resolved from the config-seeded in-memory store (no DB) or the env
bootstrap key. No dev-spoof — this is a service-to-service API.
"""

from platform_auth import build_auth

from src.api.deps import get_key_store
from src.config import get_settings

_deps = build_auth(
    get_key_store,
    envelope="llm_",
    # Unwrap at the boundary: build_auth takes Callable[[], str | None] and
    # compares the result against the presented header, so it must get a plain
    # str. The SecretStr stops at this line — don't push it into platform_auth.
    get_env_key=lambda: get_settings().api_key.get_secret_value(),
    is_prod=lambda: get_settings().llm_env == "production",
    enable_dev_spoof=False,
    audit_logger_name="llm.audit",
)

require_api_key = _deps.require_api_key
require_scope = _deps.require_scope
get_actor = _deps.get_actor
