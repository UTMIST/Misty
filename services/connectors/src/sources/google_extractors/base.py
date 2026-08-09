"""Extractor strategy for Google editor types.

Each Google editor type has a native API returning far richer structure than
Drive's flat export. Adopting them one at a time must not disturb the fetch
path, so extraction is a strategy keyed by MIME type: GoogleSource resolves an
extractor, hands it the API clients, and receives text plus warnings.
"""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ExtractedText:
    text: str
    # Non-fatal information loss (e.g. a spreadsheet whose extra tabs were not
    # read). Propagated to SourceResult.warnings.
    warnings: list[str] = field(default_factory=list)


class Extractor(Protocol):
    # OAuth scopes this extractor needs. GoogleSource unions these across every
    # registered extractor, so adding a native API adds a scope here rather
    # than at a single hard-coded constant.
    scopes: tuple[str, ...]

    # Google API client names this extractor needs (e.g. "drive", "docs").
    # GoogleSource unions these across every registered extractor and builds
    # exactly that set of clients, so a new extractor declares its needs here
    # rather than having them hard-coded at the build site.
    services: tuple[str, ...]

    def extract(self, services: dict, file_id: str, mime: str) -> ExtractedText:
        """`services` maps API name ("drive", "docs") to a built client."""
        ...


def execute(request):
    """Run a Google API request, normalizing failures to SourceError types.

    Shared by every extractor so one HTTP status maps to one SourceError in
    exactly one place.
    """
    from googleapiclient.errors import HttpError

    from src.sources.base import SourceForbidden, SourceNotFound, SourceUnavailable

    try:
        return request.execute()
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        if status == 403:
            raise SourceForbidden("google denied access to this file") from e
        if status == 404:
            raise SourceNotFound("google has no such file") from e
        raise SourceUnavailable(f"google api error (status {status})") from e
    except Exception as e:
        raise SourceUnavailable(f"google transport failure: {type(e).__name__}") from e
