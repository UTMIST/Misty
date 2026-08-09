"""Shared API-key auth for UTMIST platform services.

Pure leaf: depends only on fastapi/starlette/argon2 + stdlib. Never imports
service code. Wire it per-service via `build_auth(...)`.
"""

from platform_auth.audit import AuditLogMiddleware
from platform_auth.factory import AuthDeps, build_auth
from platform_auth.hashing import PREFIX_LENGTH, generate_key, parse_prefix, verify_key
from platform_auth.memory_store import ConsumerKeyRow, InMemoryKeyStore, key_store_from_config
from platform_auth.models import (
    ADMIN_SCOPE,
    DEV_SPOOF_SCOPE,
    ApiKeyRow,
    ApiKeyStore,
    AuthedKey,
)

__all__ = [
    "AuditLogMiddleware",
    "AuthDeps",
    "build_auth",
    "PREFIX_LENGTH",
    "generate_key",
    "parse_prefix",
    "verify_key",
    "ADMIN_SCOPE",
    "DEV_SPOOF_SCOPE",
    "ApiKeyRow",
    "ApiKeyStore",
    "AuthedKey",
    "ConsumerKeyRow",
    "InMemoryKeyStore",
    "key_store_from_config",
]
