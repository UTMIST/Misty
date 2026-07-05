import httpx

from contracts.directory import DirectoryUnavailable
from src.directory.http_client import HttpDirectoryClient


def _client(handler):
    return HttpDirectoryClient("http://d", "k", client=httpx.Client(transport=httpx.MockTransport(handler)))


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
    c = _client(lambda req: httpx.Response(200, json=[{"provider": "discord", "external_id": "42"}]))
    assert c.list_identifiers("p1") == [{"provider": "discord", "external_id": "42"}]
    c2 = _client(lambda req: httpx.Response(503))
    try:
        c2.get_person_by_github("x")
        assert False
    except DirectoryUnavailable:
        pass
