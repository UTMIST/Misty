from platform_auth import ADMIN_SCOPE, AuthedKey, build_auth  # noqa: F401

from src.api.deps import get_key_store
from src.config import get_settings

_deps = build_auth(
    get_key_store,
    envelope="vf_",
    # Unwrap at the boundary: build_auth takes Callable[[], str | None] and
    # compares the result against the presented header, so it must get a plain
    # str — a SecretStr would never match and auth would silently always fail.
    get_env_key=lambda: get_settings().api_key.get_secret_value(),
    is_prod=lambda: get_settings().vf_env == "production",
    enable_dev_spoof=False,
    bootstrap_honors_x_actor=True,
    audit_logger_name="verification.audit",
)

require_api_key = _deps.require_api_key
require_scope = _deps.require_scope
get_actor = _deps.get_actor
