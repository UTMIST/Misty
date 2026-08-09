"""Source-agnostic fetch interface — no FastAPI, no vendor SDK.

Neutral result type the API layer maps from, plus the source protocol and a
normalized error hierarchy the router maps to HTTP status codes.
"""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class SourceResult:
    title: str | None = None
    content: str | None = None
    # Non-fatal information loss (e.g. a spreadsheet whose extra tabs were not
    # read). Surfaced to the caller so partial content is never silent.
    warnings: list[str] = field(default_factory=list)


class SourceFetcher(Protocol):
    def fetch(self, url: str) -> SourceResult: ...


class SourceError(Exception):
    """Base for normalized source failures."""


class SourceNotConfigured(SourceError):
    """No credential is configured for this source."""


class SourceForbidden(SourceError):
    """Credential is valid but has no permission for this file."""


class SourceNotFound(SourceError):
    """No such file, or the URL yields no usable file id."""


class SourceUnsupported(SourceError):
    """Unknown source id, or a recognized file with no text form."""


class SourceUnavailable(SourceError):
    """Upstream 5xx, timeout, or transport failure."""
