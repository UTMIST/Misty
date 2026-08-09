import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.deps import get_source_registry
from src.sources.base import (
    SourceForbidden,
    SourceNotConfigured,
    SourceNotFound,
    SourceResult,
    SourceUnavailable,
    SourceUnsupported,
)

AUTH = {"X-API-Key": "dev-api-key-change-me"}


class _FakeSource:
    def __init__(self, result=None, error=None):
        self._result, self._error = result, error

    def fetch(self, url):
        if self._error is not None:
            raise self._error
        return self._result


def _client(source):
    app = create_app()
    app.dependency_overrides[get_source_registry] = lambda: {"gdocs": source}
    return TestClient(app)


def test_fetch_returns_title_content_and_warnings():
    source = _FakeSource(
        result=SourceResult(title="Deck", content="body text", warnings=["heads up"])
    )
    with _client(source) as c:
        resp = c.post("/fetch", json={"url": "https://x", "source_id": "gdocs"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"title": "Deck", "content": "body text", "warnings": ["heads up"]}


def test_fetch_requires_a_key():
    source = _FakeSource(result=SourceResult(title="t", content="c"))
    with _client(source) as c:
        resp = c.post("/fetch", json={"url": "https://x", "source_id": "gdocs"})
    assert resp.status_code in (401, 403)


def test_unknown_source_id_is_422():
    source = _FakeSource(result=SourceResult(title="t", content="c"))
    with _client(source) as c:
        resp = c.post("/fetch", json={"url": "https://x", "source_id": "nope"}, headers=AUTH)
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "error,expected",
    [
        (SourceNotConfigured("no creds"), 503),
        (SourceForbidden("denied"), 403),
        (SourceNotFound("gone"), 404),
        (SourceUnsupported("binary"), 422),
        (SourceUnavailable("upstream 500"), 502),
    ],
)
def test_source_errors_map_to_status_codes(error, expected):
    with _client(_FakeSource(error=error)) as c:
        resp = c.post("/fetch", json={"url": "https://x", "source_id": "gdocs"}, headers=AUTH)
    assert resp.status_code == expected


def test_credentials_never_appear_in_an_error_body():
    with _client(_FakeSource(error=SourceNotConfigured("no creds"))) as c:
        resp = c.post("/fetch", json={"url": "https://x", "source_id": "gdocs"}, headers=AUTH)
    assert "credentials" not in resp.text.lower() or "not configured" in resp.text.lower()
    assert "BEGIN PRIVATE KEY" not in resp.text
