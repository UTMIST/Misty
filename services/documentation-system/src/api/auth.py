"""Thin shim: builds docs auth deps from platform_auth."""

from platform_auth import ADMIN_SCOPE, AuthedKey, build_auth  # noqa: F401

from src.api.deps import get_storage
from src.config import get_settings

_deps = build_auth(
    get_storage,
    envelope="doc_",
    # SecretStr boundary: build_auth takes `Callable[[], str | None]` and
    # compare_digest's the result against the presented X-API-Key header, so
    # it must receive a plain str. Unwrap here, not deeper.
    get_env_key=lambda: get_settings().api_key.get_secret_value(),
    audit_logger_name="documentation_system.audit",
)

require_api_key = _deps.require_api_key
require_scope = _deps.require_scope
get_actor = _deps.get_actor
get_on_behalf_actor = _deps.get_on_behalf_actor
