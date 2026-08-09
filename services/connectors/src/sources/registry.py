"""Config-driven source selection. Add a source = new impl + one entry here."""

from collections.abc import Callable

from src.config import Settings
from src.sources.base import SourceFetcher
from src.sources.google import GOOGLE_SOURCE_IDS, GoogleSource


def _build_google(settings: Settings) -> SourceFetcher:
    return GoogleSource(
        credentials_json_b64=settings.google_credentials_json.get_secret_value(),
        max_content_chars=settings.max_content_chars,
        request_timeout_s=settings.request_timeout_s,
    )


SOURCE_BUILDERS: dict[str, Callable[[Settings], SourceFetcher]] = {
    source_id: _build_google for source_id in GOOGLE_SOURCE_IDS
}


def build_registry(settings: Settings) -> dict[str, SourceFetcher]:
    """Instantiate every configured source once, at startup.

    All four Google source ids (gdocs/gsheets/gslides/gdrive) share ONE
    GoogleSource instance rather than one each — they have identical config,
    and a GoogleSource memoizes its built credentials internally, so four
    separate instances would mean four independent (and four times as
    expensive) credential decodes and token exchanges for no benefit.
    """
    built: dict[Callable[[Settings], SourceFetcher], SourceFetcher] = {}
    registry: dict[str, SourceFetcher] = {}
    for source_id, build in SOURCE_BUILDERS.items():
        if build not in built:
            built[build] = build(settings)
        registry[source_id] = built[build]
    return registry
