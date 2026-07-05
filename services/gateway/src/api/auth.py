"""Thin shim: builds the gateway's auth deps from platform_auth (external keys)."""

from platform_auth import ADMIN_SCOPE, AuthedKey, build_auth  # noqa: F401

from src.api.deps import get_storage
from src.config import get_settings

_deps = build_auth(
    get_storage,
    envelope="gw_",
    get_env_key=lambda: get_settings().api_key,
    audit_logger_name="gateway.audit",
)

require_api_key = _deps.require_api_key
require_scope = _deps.require_scope
get_actor = _deps.get_actor
