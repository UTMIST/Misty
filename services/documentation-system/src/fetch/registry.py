from contracts.fetcher import Fetcher, FetchError, FetchResult
from src.fetch.connectors import ConnectorsFetcher
from src.fetch.github import GithubFetcher
from src.fetch.web import WebFetcher

GOOGLE_SOURCE_IDS = ("gdocs", "gsheets", "gslides", "gdrive")


class FetchUnsupported(FetchError):
    """No fetcher is registered for a source."""


class FetcherRegistry:
    def __init__(self, mapping: dict[str, Fetcher]) -> None:
        self._by_source = mapping

    def fetch_for(self, source_id: str, url: str) -> FetchResult:
        fetcher = self._by_source.get(source_id)
        if fetcher is None:
            raise FetchUnsupported(f"no fetcher registered for source: {source_id}")
        return fetcher.fetch(url)


def default_registry() -> FetcherRegistry:
    """Fetchers wired up today: public web + github in-process, and the Google
    sources via the connectors service. One ConnectorsFetcher per source id
    because Fetcher.fetch takes only a url."""
    from src.config import get_settings

    settings = get_settings()
    mapping: dict[str, Fetcher] = {"web": WebFetcher(), "github": GithubFetcher()}
    for source_id in GOOGLE_SOURCE_IDS:
        mapping[source_id] = ConnectorsFetcher(
            source_id=source_id,
            base_url=settings.connectors_base_url,
            # SecretStr boundary: ConnectorsFetcher sends the key as a plain
            # header value, so unwrap here rather than pushing SecretStr down.
            api_key=settings.connectors_api_key.get_secret_value(),
        )
    return FetcherRegistry(mapping)
