"""Shared content-handling rules for stored document text.

The cap and the hash live here, not in any one fetcher, because every source
connector's content reaches storage through ingest/refetch — enforcing at that
shared boundary means a new connector inherits both rules for free."""

from hashlib import sha256

# Safety cap on stored document text. Guards against pathological pages and
# keeps a single doc_content row loadable in memory.
MAX_CONTENT_CHARS = 1_000_000


def content_hash(text: str) -> str:
    """The canonical digest for doc_content.content_hash. Every writer MUST use
    this — a second hashing convention silently breaks change detection."""
    return sha256(text.encode()).hexdigest()


def clamp_content(text: str) -> tuple[str, bool]:
    """Return (text capped at MAX_CONTENT_CHARS, whether it was truncated)."""
    if len(text) <= MAX_CONTENT_CHARS:
        return text, False
    return text[:MAX_CONTENT_CHARS], True
