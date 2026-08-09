from datetime import datetime, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from contracts.types import Source
from src.api.app import create_app
from src.api.deps import get_directory, get_fetchers, get_storage
from src.api.hashing import generate_key
from src.storage.in_memory import InMemoryStorageAdapter

ADMIN = {"X-API-Key": "test-key"}
P1 = "11111111-1111-1111-1111-111111111111"


def _sources():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Source(
            id="web",
            label="Web",
            url_patterns=[],
            requires_auth=False,
            has_api=False,
            content_fetch_enabled=True,
            created_at=now,
            updated_at=now,
            created_by="system",
            updated_by="system",
        )
    ]


class FakeFetchers:
    def fetch_for(self, source_id, url):
        from contracts.fetcher import FetchResult

        return FetchResult(title="Fetched", content_snapshot="body")


class FakeDirectory:
    def __init__(self, teams=frozenset()):
        self._teams = teams

    def get_team_label(self, team_id):
        return "T"

    def get_person_label(self, person_id):
        return "P"

    def get_active_team_ids(self, person_id):
        return self._teams


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    from src.config import get_settings

    get_settings.cache_clear()
    adapter = InMemoryStorageAdapter(seed_sources=_sources())
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: adapter
    app.dependency_overrides[get_fetchers] = lambda: FakeFetchers()
    app.dependency_overrides[get_directory] = lambda: FakeDirectory()
    client = TestClient(app)

    def mk_key(scopes):
        plaintext, prefix, key_hash = generate_key()
        adapter.create_api_key(
            name=f"k-{prefix}", prefix=prefix, key_hash=key_hash, scopes=scopes, actor="t"
        )
        return {"X-API-Key": plaintext}

    return client, adapter, mk_key


def test_actor_sees_only_person_granted_doc(ctx):
    client, adapter, mk_key = ctx
    # two docs via admin
    a = client.post("/docs", json={"url": "https://granted"}, headers=ADMIN).json()["doc"]["id"]
    client.post("/docs", json={"url": "https://hidden"}, headers=ADMIN)
    client.post(
        f"/docs/{a}/grants", json={"grantee_type": "person", "grantee_id": P1}, headers=ADMIN
    )

    reader = mk_key(["docs:read", "act-as-user"])
    obo = {**reader, "X-On-Behalf-Of": P1}
    listed = client.get("/docs", headers=obo).json()
    assert [d["url"] for d in listed] == ["https://granted"]
    assert client.get(f"/docs/{a}", headers=obo).status_code == 200


def test_no_actor_plain_read_key_sees_nothing(ctx):
    client, adapter, mk_key = ctx
    client.post("/docs", json={"url": "https://x"}, headers=ADMIN)
    reader = mk_key(["docs:read"])
    assert client.get("/docs", headers=reader).json() == []


def test_read_all_key_sees_everything(ctx):
    client, adapter, mk_key = ctx
    client.post("/docs", json={"url": "https://x"}, headers=ADMIN)
    reader = mk_key(["docs:read:all"])
    assert len(client.get("/docs", headers=reader).json()) == 1


def test_grant_endpoints_add_and_remove(ctx):
    client, adapter, mk_key = ctx
    doc_id = client.post("/docs", json={"url": "https://d"}, headers=ADMIN).json()["doc"]["id"]
    r = client.post(f"/docs/{doc_id}/grants", json={"grantee_type": "org"}, headers=ADMIN)
    assert r.status_code == 200
    grant = next(g for g in r.json()["grants"] if g["grantee_type"] == "org")
    # Fix: the grant must record the real authenticated caller, not a hardcoded "api".
    assert grant["created_by"] == "env-bootstrap"
    d = client.request(
        "DELETE",
        f"/docs/{doc_id}/grants",
        json={"grantee_type": "org"},
        headers=ADMIN,
    )
    assert d.status_code == 200
    assert client.get(f"/docs/{doc_id}", headers=ADMIN).json()["grants"] == []


def test_on_behalf_write_to_invisible_doc_404(ctx):
    client, adapter, mk_key = ctx
    doc_id = client.post("/docs", json={"url": "https://secret"}, headers=ADMIN).json()["doc"]["id"]
    writer = mk_key(["docs:write", "act-as-user"])
    obo = {**writer, "X-On-Behalf-Of": P1}  # P1 has no grant on this doc
    r = client.patch(f"/docs/{doc_id}", json={"description": "x"}, headers=obo)
    assert r.status_code == 404


def test_no_actor_write_key_can_patch(ctx):
    client, adapter, mk_key = ctx
    doc_id = client.post("/docs", json={"url": "https://any"}, headers=ADMIN).json()["doc"]["id"]
    writer = mk_key(["docs:write"])
    r = client.patch(f"/docs/{doc_id}", json={"description": "x"}, headers=writer)
    assert r.status_code == 200


def test_team_grant_and_directory_down_degradation(ctx, monkeypatch):
    client, adapter, mk_key = ctx
    T1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    team_doc = client.post("/docs", json={"url": "https://team"}, headers=ADMIN).json()["doc"]["id"]
    org_doc = client.post("/docs", json={"url": "https://org"}, headers=ADMIN).json()["doc"]["id"]
    client.post(
        f"/docs/{team_doc}/grants", json={"grantee_type": "team", "grantee_id": T1}, headers=ADMIN
    )
    client.post(f"/docs/{org_doc}/grants", json={"grantee_type": "org"}, headers=ADMIN)

    # Directory that knows the actor is on T1
    client.app.dependency_overrides[get_directory] = lambda: FakeDirectory(
        teams=frozenset({UUID(T1)})
    )
    reader = mk_key(["docs:read", "act-as-user"])
    obo = {**reader, "X-On-Behalf-Of": P1}
    urls = {d["url"] for d in client.get("/docs", headers=obo).json()}
    assert urls == {"https://team", "https://org"}

    # Directory DOWN: team grant withheld, org grant still visible (partial fail-closed)
    class DownDir(FakeDirectory):
        def get_active_team_ids(self, person_id):
            from contracts.directory import DirectoryUnavailable

            raise DirectoryUnavailable("down")

    client.app.dependency_overrides[get_directory] = lambda: DownDir()
    urls_down = {d["url"] for d in client.get("/docs", headers=obo).json()}
    assert urls_down == {"https://org"}


def test_content_snapshot_withheld_from_unauthorized_via_get(ctx):
    client, adapter, mk_key = ctx
    doc_id = client.post("/docs", json={"url": "https://c"}, headers=ADMIN).json()["doc"]["id"]
    reader = mk_key(["docs:read", "act-as-user"])
    obo = {**reader, "X-On-Behalf-Of": P1}
    # Not granted -> 404, no content leaks
    assert client.get(f"/docs/{doc_id}", headers=obo).status_code == 404


def test_get_preserves_grants_when_label_backfill_fires(ctx):
    """A GET that triggers _backfill_labels (owner set, label null because the
    directory was down at ingest) must still return the doc's grants — the
    backfill's storage.update_doc round-trip must not silently drop them."""
    client, adapter, mk_key = ctx
    T1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    class DownDirectory(FakeDirectory):
        def get_team_label(self, team_id):
            from contracts.directory import DirectoryUnavailable

            raise DirectoryUnavailable("down")

    # Directory unavailable at ingest -> owning_team_label stays null.
    client.app.dependency_overrides[get_directory] = lambda: DownDirectory()
    doc_id = client.post(
        "/docs", json={"url": "https://backfill", "owning_team_id": T1}, headers=ADMIN
    ).json()["doc"]["id"]
    client.post(f"/docs/{doc_id}/grants", json={"grantee_type": "org"}, headers=ADMIN)

    # Directory reachable again -> the next GET triggers _backfill_labels.
    client.app.dependency_overrides[get_directory] = lambda: FakeDirectory()
    r = client.get(f"/docs/{doc_id}", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["owning_team_label"] == "T"
    assert any(g["grantee_type"] == "org" for g in body["grants"])
