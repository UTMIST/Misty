"""Thin shim over platform_auth, binding the gateway key envelope."""

from platform_auth import PREFIX_LENGTH, verify_key  # noqa: F401
from platform_auth import generate_key as _generate_key
from platform_auth import parse_prefix as _parse_prefix

KEY_ENVELOPE = "gw_"


def generate_key() -> tuple[str, str, str]:
    return _generate_key(KEY_ENVELOPE)


def parse_prefix(candidate: str) -> str | None:
    return _parse_prefix(candidate, KEY_ENVELOPE)
