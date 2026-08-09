from uuid import uuid4, UUID

import httpx
import pytest

from contracts.directory import DirectoryUnavailable
from src.directory.http_client import HttpDirectoryClient


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_get_team_label_returns_label():
    def handler(request):
        assert request.headers["X-API-Key"] == "k"
        return httpx.Response(200, json={"id": str(uuid4()), "label": "Partnerships"})

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    assert dc.get_team_label(uuid4()) == "Partnerships"


def test_get_person_label_uses_display_name():
    def handler(request):
        return httpx.Response(200, json={"display_name": "Priya"})

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    assert dc.get_person_label(uuid4()) == "Priya"


def test_404_returns_none():
    def handler(request):
        return httpx.Response(404, json={"detail": "not found"})

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    assert dc.get_team_label(uuid4()) is None


def test_connection_error_raises_unavailable():
    def handler(request):
        raise httpx.ConnectError("boom")

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    with pytest.raises(DirectoryUnavailable):
        dc.get_team_label(uuid4())


def test_5xx_raises_unavailable():
    def handler(request):
        return httpx.Response(503)

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    with pytest.raises(DirectoryUnavailable):
        dc.get_person_label(uuid4())


def test_403_raises_unavailable():
    def handler(request):
        return httpx.Response(403, json={"detail": "forbidden"})

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    with pytest.raises(DirectoryUnavailable):
        dc.get_team_label(uuid4())


def test_get_active_team_ids_parses_team_ids():
    pid = UUID("11111111-1111-1111-1111-111111111111")

    def handler(request):
        assert request.url.path == "/memberships"
        assert request.url.params["person_id"] == str(pid)
        assert request.url.params["active_only"] == "true"
        return httpx.Response(
            200,
            json=[
                {"team_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
                {"team_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"},
            ],
        )

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    ids = dc.get_active_team_ids(pid)
    assert ids == frozenset(
        {
            UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        }
    )


def test_get_active_team_ids_empty():
    pid = UUID("11111111-1111-1111-1111-111111111111")

    def handler(request):
        return httpx.Response(200, json=[])

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    ids = dc.get_active_team_ids(pid)
    assert ids == frozenset()


def test_get_active_team_ids_5xx_raises_unavailable():
    pid = UUID("11111111-1111-1111-1111-111111111111")

    def handler(request):
        return httpx.Response(503)

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    with pytest.raises(DirectoryUnavailable):
        dc.get_active_team_ids(pid)


def test_get_active_team_ids_malformed_2xx_body_raises_unavailable():
    """A 200 with an unexpected body shape (missing team_id key) must degrade
    to DirectoryUnavailable, not leak a raw KeyError/ValueError/TypeError up
    to the caller — the read path must never 500 on a malformed 2xx."""
    pid = UUID("11111111-1111-1111-1111-111111111111")

    def handler(request):
        return httpx.Response(200, json=[{"not_team_id": "x"}])

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    with pytest.raises(DirectoryUnavailable):
        dc.get_active_team_ids(pid)


def test_get_active_team_ids_non_uuid_team_id_raises_unavailable():
    pid = UUID("11111111-1111-1111-1111-111111111111")

    def handler(request):
        return httpx.Response(200, json=[{"team_id": "not-a-uuid"}])

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    with pytest.raises(DirectoryUnavailable):
        dc.get_active_team_ids(pid)
