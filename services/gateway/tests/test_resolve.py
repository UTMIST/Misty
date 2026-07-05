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


def test_directory_down_503():
    c, h = _client(FakeDir(down=True))
    assert c.get("/v1/resolve/discord/octocat", headers=h).status_code == 503


def test_requires_scope_and_key():
    c, h = _client(FakeDir(person={"id": "p1"}, idents=[{"provider": "discord", "external_id": "42"}]))
    assert c.get("/v1/resolve/discord/octocat").status_code == 401
