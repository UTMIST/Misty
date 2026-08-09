"""HTTP-backed fetcher for the connectors service.

Implements the same Fetcher protocol as WebFetcher/GithubFetcher, so ingest and
refetch need no changes. source_id is bound at construction because
Fetcher.fetch takes only a url — the registry has already resolved the source.
"""

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from contracts.fetcher import FetchError, FetchResult
from src.fetch.web import SNAPSHOT_CHARS


class _ConnectorsResponseBody(BaseModel):
    """Mirrors the connectors service's FetchResponse shape. Validating the
    decoded JSON against this (rather than trusting body.get(...) calls)
    turns a well-formed object with wrong field types (e.g. content as an
    int or a list) into a FetchError instead of a raw TypeError/pydantic
    ValidationError escaping past ingest_doc's `except FetchError`."""

    model_config = ConfigDict(extra="ignore")
    title: str | None = None
    content: str | None = None
    warnings: list[str] = []


class ConnectorsFetcher:
    def __init__(
        self,
        *,
        source_id: str,
        base_url: str,
        api_key: str,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._source_id = source_id
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client
        self._timeout = timeout

    def fetch(self, url: str) -> FetchResult:
        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            resp = client.post(
                f"{self._base_url}/fetch",
                json={"url": url, "source_id": self._source_id},
                headers={"X-API-Key": self._api_key},
            )
            if resp.status_code != 200:
                detail = _safe_detail(resp)
                raise FetchError(f"connectors returned {resp.status_code}: {detail}")
            raw_body = resp.json()
            if not isinstance(raw_body, dict):
                raise FetchError(
                    f"connectors returned a non-object JSON body: {type(raw_body).__name__}"
                )
            try:
                body = _ConnectorsResponseBody.model_validate(raw_body)
            except ValidationError as e:
                # A well-formed JSON object with a wrong field type (e.g.
                # content: 123 or content: ["a"]) must map to FetchError like
                # every other failure mode here, not escape as a raw
                # TypeError/ValidationError past ingest_doc's `except
                # FetchError`.
                raise FetchError(f"connectors returned a malformed body: {e}") from e
        except httpx.HTTPError as e:
            raise FetchError(f"connectors unreachable: {e}") from e
        except ValueError as e:
            # resp.json() raises json.JSONDecodeError (a ValueError subclass)
            # for a 200 with a non-JSON body — e.g. a proxy/edge error page.
            # Must map to FetchError like every other failure mode here, so
            # ingest_doc's `except FetchError` catches it and it becomes a
            # per-doc warning, never a 500 on POST /docs.
            raise FetchError(f"connectors returned an unparseable body: {e}") from e
        finally:
            if self._client is None:
                client.close()

        # Normalize "" to None, matching WebFetcher's convention (see the
        # comment in src/fetch/web.py): empty content and no snapshot travel
        # together rather than leaving content="" paired with no snapshot.
        # No pre-truncation here: src.content.clamp_content is the single
        # authority on the stored-size cap (see ingest.py), and it must see
        # the full length to decide whether to emit the "content truncated"
        # warning — clamping here would make that warning unreachable.
        content = body.content or None
        return FetchResult(
            title=body.title,
            content=content,
            content_snapshot=content[:SNAPSHOT_CHARS] if content else None,
            warnings=list(body.warnings or []),
        )


def _safe_detail(resp: httpx.Response) -> str:
    """A short, non-sensitive description of an error response."""
    try:
        return str(resp.json().get("detail", ""))[:200]
    except Exception:
        return resp.text[:200]
