"""Envelope-parametrized API-key hashing (argon2).

The envelope (e.g. "tt_", "doc_") is passed in by the calling service rather
than hardcoded, so one implementation serves every service.
"""

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()

PREFIX_LENGTH = 8  # chars in the public prefix segment


def generate_key(envelope: str) -> tuple[str, str, str]:
    """Generate a new API key. Returns (plaintext, prefix, key_hash).

    The plaintext MUST be shown to the caller exactly once and then discarded.
    Format: <envelope><prefix>_<secret>.
    """
    raw_prefix = secrets.token_urlsafe(12).replace("_", "-")
    prefix = raw_prefix[:PREFIX_LENGTH]
    secret = secrets.token_urlsafe(32)
    plaintext = f"{envelope}{prefix}_{secret}"
    key_hash = _hasher.hash(plaintext)
    return plaintext, prefix, key_hash


def verify_key(candidate: str, key_hash: str) -> bool:
    """Constant-time verify a candidate plaintext against an argon2 hash."""
    try:
        return _hasher.verify(key_hash, candidate)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def parse_prefix(candidate: str, envelope: str) -> str | None:
    """Extract the prefix segment, or None if the candidate is not <envelope><prefix>_<secret>."""
    if not candidate.startswith(envelope):
        return None
    body = candidate[len(envelope) :]
    parts = body.split("_", 1)
    if len(parts) != 2 or len(parts[0]) != PREFIX_LENGTH:
        return None
    return parts[0]
