from fastapi.testclient import TestClient

from contracts.directory import DirectoryUnavailable
from src.api.app import create_app
from src.api.deps import get_directory, get_storage
from src.api.hashing import generate_key
from src.storage.in_memory import InMemoryStorageAdapter


class FakeDir:
    def __init__(self, person=None, idents=None, down=False):
        self._p, self._i, self._down = person, idents or [], down
    def get_person_by_github(self, login):
        if self._down:
            raise DirectoryUnavailable("x")
        return self._p
    def list_identifiers(self, pid):
        return self._i


def _client(fake):
    store = InMemoryStorageAdapter()
    plaintext, prefix, key_hash = generate_key()
    store.create_api_key(name="c", prefix=prefix, key_hash=key_hash,
                         scopes=["resolve:discord"], actor="t")
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_directory] = lambda: fake
    return TestClient(app), {"X-API-Key": plaintext}


def test_resolves_discord_id():
    c, h = _client(FakeDir(person={"id": "p1"},
                           idents=[{"provider": "github", "external_id": "octocat"},
                                   {"provider": "discord", "external_id": "42"}]))
    r = c.get("/v1/resolve/discord/octocat", headers=h)
    assert r.status_code == 200 and r.json() == {"discord_id": "42"}


def test_login_not_found_404():
    c, h = _client(FakeDir(person=None))
    assert c.get("/v1/resolve/discord/ghost", headers=h).status_code == 404


def test_no_discord_identifier_404():
    c, h = _client(FakeDir(person={"id": "p1"}, idents=[{"provider": "github", "external_id": "x"}]))
    assert c.get("/v1/resolve/discord/octocat", headers=h).status_code == 404


def test_the_two_misses_are_indistinguishable():
    """"Not in the directory" and "in it, but no Discord link" must look identical.

    Otherwise the endpoint is a membership oracle: anyone holding a
    resolve:discord key could walk a list of GitHub logins and learn which of
    them belong to UTMIST members, which is more than this endpoint is meant to
    disclose about anyone.
    """
    absent, absent_h = _client(FakeDir(person=None))
    present, present_h = _client(
        FakeDir(person={"id": "p1"}, idents=[{"provider": "github", "external_id": "x"}])
    )
    a = absent.get("/v1/resolve/discord/octocat", headers=absent_h)
    b = present.get("/v1/resolve/discord/octocat", headers=present_h)
    assert a.status_code == b.status_code == 404
    assert a.json() == b.json()


def test_directory_down_503():
    c, h = _client(FakeDir(down=True))
    assert c.get("/v1/resolve/discord/octocat", headers=h).status_code == 503


def test_person_without_an_id_fails_closed_to_503():
    # An unrecognised upstream shape is an upstream fault, not a missing record.
    # Indexing it blindly would raise KeyError and surface as a 500.
    c, h = _client(FakeDir(person={"name": "no id here"}))
    assert c.get("/v1/resolve/discord/octocat", headers=h).status_code == 503


def test_response_carries_only_the_discord_id():
    c, h = _client(
        FakeDir(
            person={"id": "p1", "primary_email": "someone@example.com", "full_name": "Someone"},
            idents=[
                {"provider": "discord", "external_id": "42"},
                {"provider": "uoft_email", "external_id": "someone@utoronto.ca"},
            ],
        )
    )
    r = c.get("/v1/resolve/discord/octocat", headers=h)
    assert r.status_code == 200
    assert r.json() == {"discord_id": "42"}
    body = r.text
    for leaked in ("someone@example.com", "Someone", "utoronto.ca", "p1"):
        assert leaked not in body


def test_requires_scope_and_key():
    c, h = _client(FakeDir(person={"id": "p1"}, idents=[{"provider": "discord", "external_id": "42"}]))
    assert c.get("/v1/resolve/discord/octocat").status_code == 401
