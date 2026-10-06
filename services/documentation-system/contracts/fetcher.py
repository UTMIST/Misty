from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict


class SourcePermission(BaseModel):
    """Source ACL evidence returned alongside fetched content."""

    model_config = ConfigDict(extra="forbid")
    permission_id: str
    principal_type: Literal["user", "group", "domain", "anyone"]
    principal: str | None = None
    role: str
    inherited: bool = False
    inherited_from: list[str] = []
    allow_file_discovery: bool | None = None
    expiration_time: datetime | None = None


class FetchResult(BaseModel):
    """What a fetcher retrieved for one URL.

    INVARIANT — None means "nothing was extracted", never "extracted and empty".
    A fetcher that finds no text MUST return None for both content fields, not
    "". Ingest and refetch decide whether to overwrite already-stored text and
    snapshot by testing for absence, so a fetcher that returns "" for an empty
    page wipes content the previous fetch stored; None preserves it. Every
    Fetcher implementation is bound by this, including future connectors."""

    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    # Full extracted text, uncapped by the fetcher — ingest/refetch clamp it to
    # src.content.MAX_CONTENT_CHARS, which is the single truncation point.
    content: str | None = None
    content_snapshot: str | None = None  # bounded preview, derived from content
    # Non-fatal information loss reported by the fetcher (e.g. a spreadsheet
    # whose extra tabs were not read). Folded into the ingest warnings.
    warnings: list[str] = []
    # None means this fetcher has no source-ACL contract. Google fetches must
    # return a complete list, including [] when the file has no permissions.
    permissions: list[SourcePermission] | None = None


class FetchError(Exception):
    """Raised when a fetcher cannot retrieve or parse a URL. Non-fatal at the
    ingest layer — becomes a warning, never blocks the doc."""


class Fetcher(Protocol):
    def fetch(self, url: str) -> FetchResult:
        """Retrieve title, full content, and snapshot for a URL. Raises FetchError on failure."""
        ...
