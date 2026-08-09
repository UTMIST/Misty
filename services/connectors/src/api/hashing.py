"""API-key hashing bound to this service's `connectors_` envelope."""

from platform_auth import generate_key as _generate_key
from platform_auth import parse_prefix as _parse_prefix
from platform_auth import verify_key  # noqa: F401  (re-exported for symmetry)

ENVELOPE = "connectors_"


def generate_key() -> tuple[str, str, str]:
    """Return (plaintext, prefix, key_hash) for a new `connectors_`-envelope key."""
    return _generate_key(ENVELOPE)


def parse_prefix(candidate: str) -> str | None:
    return _parse_prefix(candidate, ENVELOPE)
