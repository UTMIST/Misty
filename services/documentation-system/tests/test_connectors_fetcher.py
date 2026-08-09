import httpx
import pytest

from contracts.fetcher import FetchError
from src.fetch.connectors import ConnectorsFetcher


def _fetcher(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ConnectorsFetcher(
        source_id="gdocs", base_url="http://connectors", api_key="k", client=client
    )


def test_successful_fetch_returns_content_and_derived_snapshot():
    def handler(request):
        return httpx.Response(200, json={"title": "Deck", "content": "x" * 5000, "warnings": []})

    result = _fetcher(handler).fetch("https://docs.google.com/document/d/abc/edit")
    assert result.title == "Deck"
    assert len(result.content) == 5000
    assert len(result.content_snapshot) == 2000
    assert result.content.startswith(result.content_snapshot)


def test_warnings_are_surfaced():
    def handler(request):
        return httpx.Response(
            200, json={"title": "Budget", "content": "a,b", "warnings": ["first sheet only"]}
        )

    result = _fetcher(handler).fetch("https://docs.google.com/spreadsheets/d/abc/edit")
    assert result.warnings == ["first sheet only"]


def test_source_id_is_sent_in_the_request_body():
    seen = {}

    def handler(request):
        import json as _json

        seen.update(_json.loads(request.content))
        return httpx.Response(200, json={"title": "t", "content": "c", "warnings": []})

    _fetcher(handler).fetch("https://docs.google.com/document/d/abc/edit")
    assert seen["source_id"] == "gdocs"
    assert seen["url"] == "https://docs.google.com/document/d/abc/edit"


@pytest.mark.parametrize("status", [403, 404, 422, 502, 503, 500])
def test_every_error_status_becomes_a_fetcherror(status):
    def handler(request):
        return httpx.Response(status, json={"detail": "nope"})

    with pytest.raises(FetchError):
        _fetcher(handler).fetch("https://docs.google.com/document/d/abc/edit")


def test_transport_failure_becomes_a_fetcherror():
    def handler(request):
        raise httpx.ConnectError("connectors is down")

    with pytest.raises(FetchError):
        _fetcher(handler).fetch("https://docs.google.com/document/d/abc/edit")


def test_200_with_non_json_body_becomes_a_fetcherror():
    # A proxy/edge returning a 200 HTML error page must never escape as a raw
    # json.JSONDecodeError — it has to become an ordinary per-doc FetchError,
    # same as any other connectors failure mode.
    def handler(request):
        return httpx.Response(200, content=b"<html>not json</html>")

    with pytest.raises(FetchError):
        _fetcher(handler).fetch("https://docs.google.com/document/d/abc/edit")


def test_200_with_json_list_body_becomes_a_fetcherror():
    # A 200 whose decoded body is a JSON list (not an object) must not reach
    # body.get(...) and raise a raw AttributeError.
    def handler(request):
        return httpx.Response(200, json=["not", "an", "object"])

    with pytest.raises(FetchError):
        _fetcher(handler).fetch("https://docs.google.com/document/d/abc/edit")


def test_empty_content_normalizes_content_and_snapshot_together():
    # Matches WebFetcher's convention: "" is treated as no content, so
    # content and content_snapshot are both None rather than "" paired with
    # None (which would write a doc_content row holding sha256("")).
    def handler(request):
        return httpx.Response(200, json={"title": "Empty", "content": "", "warnings": []})

    result = _fetcher(handler).fetch("https://docs.google.com/document/d/abc/edit")
    assert result.content is None
    assert result.content_snapshot is None


def test_wrong_typed_content_int_becomes_a_fetcherror():
    # A well-formed JSON object with the wrong field type must not escape as
    # a raw TypeError (len(123)) — it has to become an ordinary FetchError,
    # same as any other connectors failure mode, so ingest_doc's
    # `except FetchError` catches it instead of a 500 on POST /docs.
    def handler(request):
        return httpx.Response(200, json={"title": "t", "content": 123, "warnings": []})

    with pytest.raises(FetchError):
        _fetcher(handler).fetch("https://docs.google.com/document/d/abc/edit")


def test_wrong_typed_content_list_becomes_a_fetcherror():
    # A well-formed JSON object with content as a list must not escape as a
    # raw pydantic ValidationError from FetchResult construction.
    def handler(request):
        return httpx.Response(200, json={"title": "t", "content": ["a"], "warnings": []})

    with pytest.raises(FetchError):
        _fetcher(handler).fetch("https://docs.google.com/document/d/abc/edit")


def test_wrong_typed_title_becomes_a_fetcherror():
    def handler(request):
        return httpx.Response(200, json={"title": 5, "content": "c", "warnings": []})

    with pytest.raises(FetchError):
        _fetcher(handler).fetch("https://docs.google.com/document/d/abc/edit")


def test_oversized_content_is_not_pre_truncated_by_the_fetcher():
    # Regression: ConnectorsFetcher used to clamp content to MAX_CONTENT_CHARS
    # itself, which made clamp_content's len() check always true and its
    # "content truncated" warning unreachable. src.content.clamp_content must
    # be the single authority on the size cap — the fetcher passes the full
    # length through untouched.
    from src.content import MAX_CONTENT_CHARS

    oversized = "x" * (MAX_CONTENT_CHARS + 1000)

    def handler(request):
        return httpx.Response(200, json={"title": "Big", "content": oversized, "warnings": []})

    result = _fetcher(handler).fetch("https://docs.google.com/document/d/abc/edit")
    assert len(result.content) == MAX_CONTENT_CHARS + 1000
