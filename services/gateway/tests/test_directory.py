import json

import httpx
import pytest
from fastapi.testclient import TestClient

from contracts.directory import DirectoryUnavailable
from src.api.app import create_app
from src.api.deps import get_directory, get_storage
from src.api.hashing import generate_key
from src.directory.http_client import HttpDirectoryClient
from src.storage.in_memory import InMemoryStorageAdapter


def _client(handler):
    return HttpDirectoryClient(
        "http://d", "k", client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_get_person_by_github_found_and_404():
    def h(req):
        if req.url.path == "/people/by-identifier/github/octocat":
            return httpx.Response(200, json={"id": "p1"})
        return httpx.Response(404)

    c = _client(h)
    assert c.get_person_by_github("octocat") == {"id": "p1"}
    assert c.get_person_by_github("ghost") is None


def test_get_person_by_github_percent_encodes_login():
    captured = {}

    def h(req):
        # raw_path is the on-the-wire (percent-encoded) path; req.url.path is decoded.
        captured["path"] = req.url.raw_path.decode()
        return httpx.Response(404)

    c = _client(h)
    c.get_person_by_github("a b/c#d")

    path = captured["path"]
    segment = path.removeprefix("/people/by-identifier/github/")
    assert " " not in segment
    assert "#" not in segment
    assert "/" not in segment
    assert segment == "a%20b%2Fc%23d"


def test_list_identifiers_and_5xx_raises():
    c = _client(
        lambda req: httpx.Response(200, json=[{"provider": "discord", "external_id": "42"}])
    )
    assert c.list_identifiers("p1") == [{"provider": "discord", "external_id": "42"}]
    c2 = _client(lambda req: httpx.Response(503))
    try:
        c2.get_person_by_github("x")
        assert False
    except DirectoryUnavailable:
        pass


@pytest.mark.parametrize(
    "person,identifiers",
    [
        ([], []),
        ({}, []),
        ({"id": 1}, []),
        ({"id": ""}, []),
        (None, []),
        ({"id": "p1"}, {}),
        ({"id": "p1"}, [None]),
        ({"id": "p1"}, [{"provider": "discord", "external_id": 42}]),
        ({"id": "p1"}, [{"provider": "discord", "external_id": ""}]),
        ({"id": "p1"}, None),
    ],
)
def test_malformed_directory_payload_returns_static_503(person, identifiers):
    def upstream(request):
        result = identifiers if request.url.path.endswith("/identifiers") else person
        return httpx.Response(200, content=json.dumps(result))

    directory = _client(upstream)
    store = InMemoryStorageAdapter()
    key, prefix, key_hash = generate_key()
    store.create_api_key(
        name="consumer", prefix=prefix, key_hash=key_hash, scopes=["resolve:discord"], actor="test"
    )
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_directory] = lambda: directory
    with TestClient(app) as client:
        response = client.get("/v1/resolve/discord/octocat", headers={"X-API-Key": key})
    assert response.status_code == 503
    assert response.json() == {"detail": "directory temporarily unavailable"}


def test_invalid_json_is_an_upstream_failure():
    directory = _client(lambda req: httpx.Response(200, content="private invalid response"))
    with pytest.raises(DirectoryUnavailable, match="invalid directory JSON") as exc:
        directory.get_person_by_github("octocat")
    assert "private" not in str(exc.value)


def test_timeout_is_an_upstream_failure_without_request_details():
    def timeout(request):
        raise httpx.ReadTimeout("private request details", request=request)

    with pytest.raises(DirectoryUnavailable, match="directory unreachable") as exc:
        _client(timeout).get_person_by_github("octocat")
    assert "private" not in str(exc.value)


def test_outbound_calls_use_the_internal_key():
    def upstream(request):
        assert request.headers["X-API-Key"] == "internal-only"
        assert "X-On-Behalf-Of" not in request.headers
        return httpx.Response(200, json={"id": "p1"})

    directory = HttpDirectoryClient(
        "http://d", "internal-only", client=httpx.Client(transport=httpx.MockTransport(upstream))
    )
    assert directory.get_person_by_github("octocat") == {"id": "p1"}
