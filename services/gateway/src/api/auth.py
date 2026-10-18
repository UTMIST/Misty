"""Thin shim: builds the gateway's auth deps from platform_auth (external keys)."""

from platform_auth import AuthedKey, build_auth  # noqa: F401

from src.api.deps import get_storage

_deps = build_auth(
    get_storage,
    envelope="gw_",
    # No env-bootstrap key, unlike every other service's shim. That path mints
    # an AuthedKey carrying ADMIN_SCOPE, and ADMIN_SCOPE is a wildcard —
    # AuthedKey.has_scope() returns True for every scope once it is present. On
    # the internal services that is a deliberate grace path: it is how you
    # authenticate to the admin API that issues the first real key, and it is
    # only reachable from the private network.
    #
    # The gateway has neither half of that justification. Its keys are issued
    # direct-to-DB by the gateway-keys CLI, so there is no admin API to
    # bootstrap; and it is the one service exposed to the internet, so the env
    # key would be a single wildcard credential on the public door. Returning
    # None disables the path outright: every caller must present a scoped key
    # from the api_keys table. tests/test_auth.py pins this.
    get_env_key=lambda: None,
    audit_logger_name="gateway.audit",
)

require_api_key = _deps.require_api_key
require_scope = _deps.require_scope
get_actor = _deps.get_actor
