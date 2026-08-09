from datetime import datetime, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from contracts.fetcher import FetchResult
from contracts.types import Source
from src.api.app import create_app
from src.api.deps import get_directory, get_fetchers, get_storage
from src.storage.in_memory import InMemoryStorageAdapter

AUTH = {"X-API-Key": "test-key"}


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
        return FetchResult(title="Fetched", content="full fetched body", content_snapshot="body")


class FakeDirectory:
    def get_team_label(self, team_id):
        return "Partnerships"

    def get_person_label(self, person_id):
        return "Priya"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    from src.config import get_settings

    get_settings.cache_clear()
    adapter = InMemoryStorageAdapter(seed_sources=_sources())
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: adapter
    app.dependency_overrides[get_fetchers] = lambda: FakeFetchers()
    app.dependency_overrides[get_directory] = lambda: FakeDirectory()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_and_store(monkeypatch):
    """Like `client`, but also yields the backing adapter so tests can assert on
    storage the API deliberately does not expose (e.g. full doc content)."""
    monkeypatch.setenv("API_KEY", "test-key")
    from src.config import get_settings

    get_settings.cache_clear()
    adapter = InMemoryStorageAdapter(seed_sources=_sources())
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: adapter
    app.dependency_overrides[get_fetchers] = lambda: FakeFetchers()
    app.dependency_overrides[get_directory] = lambda: FakeDirectory()
    with TestClient(app) as c:
        yield c, adapter


def test_ingest_returns_201_with_warnings_envelope(client):
    resp = client.post("/docs", json={"url": "https://x.com/a"}, headers=AUTH)
    assert resp.status_code == 201
    body = resp.json()
    assert body["created"] is True
    assert body["doc"]["title"] == "Fetched"
    assert body["warnings"] == []


def test_reingest_returns_200(client):
    client.post("/docs", json={"url": "https://x.com/a"}, headers=AUTH)
    resp = client.post("/docs", json={"url": "https://x.com/a/"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["created"] is False


def test_ingest_requires_write_scope_via_missing_key(client):
    resp = client.post("/docs", json={"url": "https://x.com"})
    assert resp.status_code == 401


def test_ingest_bad_team_id_returns_400(client):
    class NotFoundDir:
        def get_team_label(self, team_id):
            return None

        def get_person_label(self, person_id):
            return None

    client.app.dependency_overrides[get_directory] = lambda: NotFoundDir()
    resp = client.post(
        "/docs",
        json={"url": "https://x.com", "owning_team_id": "00000000-0000-0000-0000-000000000001"},
        headers=AUTH,
    )
    assert resp.status_code == 400


def test_list_filter_by_tag(client):
    client.post("/docs", json={"url": "https://a.com", "tags": ["x"]}, headers=AUTH)
    client.post("/docs", json={"url": "https://b.com", "tags": ["y"]}, headers=AUTH)
    resp = client.get("/docs?tag=x", headers=AUTH)
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_ingest_tags_are_normalized_and_tag_filter_is_case_insensitive(client):
    created = client.post(
        "/docs", json={"url": "https://onboarding.com", "tags": ["Onboarding"]}, headers=AUTH
    ).json()
    doc_id = created["doc"]["id"]
    assert client.get(f"/docs/{doc_id}", headers=AUTH).json()["tags"] == ["onboarding"]

    resp_lower = client.get("/docs?tag=onboarding", headers=AUTH)
    assert resp_lower.status_code == 200
    assert len(resp_lower.json()) == 1

    resp_mixed = client.get("/docs?tag=Onboarding", headers=AUTH)
    assert resp_mixed.status_code == 200
    assert len(resp_mixed.json()) == 1


def test_get_404(client):
    resp = client.get("/docs/00000000-0000-0000-0000-000000000000", headers=AUTH)
    assert resp.status_code == 404


def test_patch_soft_delete_hides_from_default_list(client):
    created = client.post("/docs", json={"url": "https://a.com"}, headers=AUTH).json()
    doc_id = created["doc"]["id"]
    client.patch(f"/docs/{doc_id}", json={"active": False}, headers=AUTH)
    assert client.get("/docs", headers=AUTH).json() == []
    assert len(client.get("/docs?active_only=false", headers=AUTH).json()) == 1


def test_patch_bad_team_id_returns_400(client):
    doc_id = client.post("/docs", json={"url": "https://a.com"}, headers=AUTH).json()["doc"]["id"]

    class NotFoundDir:
        def get_team_label(self, team_id):
            return None

        def get_person_label(self, person_id):
            return None

    client.app.dependency_overrides[get_directory] = lambda: NotFoundDir()
    resp = client.patch(
        f"/docs/{doc_id}",
        json={"owning_team_id": "00000000-0000-0000-0000-000000000001"},
        headers=AUTH,
    )
    assert resp.status_code == 400


def test_add_and_remove_tag(client):
    doc_id = client.post("/docs", json={"url": "https://a.com"}, headers=AUTH).json()["doc"]["id"]
    assert client.post(f"/docs/{doc_id}/tags", json={"tag": "new"}, headers=AUTH).status_code == 200
    assert "new" in client.get(f"/docs/{doc_id}", headers=AUTH).json()["tags"]
    assert client.request("DELETE", f"/docs/{doc_id}/tags/new", headers=AUTH).status_code == 200
    assert "new" not in client.get(f"/docs/{doc_id}", headers=AUTH).json()["tags"]


def test_refetch_persists_full_content(client_and_store):
    client, store = client_and_store
    created = client.post("/docs", json={"url": "https://x.com/refetch"}, headers=AUTH).json()
    doc_id = created["doc"]["id"]

    resp = client.post(f"/docs/{doc_id}/refetch", headers=AUTH)
    assert resp.status_code == 200
    assert store.get_doc_content(UUID(doc_id)) == "full fetched body"


def test_refetch_no_content_leaves_prior_content_untouched(client_and_store):
    client, store = client_and_store
    created = client.post("/docs", json={"url": "https://x.com/goes-empty"}, headers=AUTH).json()
    doc_id = created["doc"]["id"]

    before = client.post(f"/docs/{doc_id}/refetch", headers=AUTH).json()
    before_text = store.get_doc_content(UUID(doc_id))
    before_meta = store.get_doc_content_meta(UUID(doc_id))

    class _NoContentFetchers:
        def fetch_for(self, source_id, url):
            return FetchResult(title="Fetched", content=None, content_snapshot=None)

    client.app.dependency_overrides[get_fetchers] = lambda: _NoContentFetchers()
    resp = client.post(f"/docs/{doc_id}/refetch", headers=AUTH)
    assert resp.status_code == 200

    after_text = store.get_doc_content(UUID(doc_id))
    after_meta = store.get_doc_content_meta(UUID(doc_id))
    assert after_text == before_text
    assert after_meta.content_hash == before_meta.content_hash
    # The snapshot is half of the same preserved pair — blanking it while the
    # full text survives leaves the doc self-inconsistent.
    assert resp.json()["content_snapshot"] == before["content_snapshot"]


def test_refetch_empty_string_content_does_not_wipe_stored_content(client_and_store):
    # A connector that violates FetchResult's None-not-"" invariant must still
    # not be able to blank stored text or the snapshot through refetch.
    client, store = client_and_store
    created = client.post("/docs", json={"url": "https://x.com/blanks"}, headers=AUTH).json()
    doc_id = created["doc"]["id"]

    before = client.post(f"/docs/{doc_id}/refetch", headers=AUTH).json()
    before_text = store.get_doc_content(UUID(doc_id))

    class _BlankFetchers:
        def fetch_for(self, source_id, url):
            return FetchResult(title="Fetched", content="", content_snapshot="")

    client.app.dependency_overrides[get_fetchers] = lambda: _BlankFetchers()
    resp = client.post(f"/docs/{doc_id}/refetch", headers=AUTH)
    assert resp.status_code == 200

    assert store.get_doc_content(UUID(doc_id)) == before_text
    assert resp.json()["content_snapshot"] == before["content_snapshot"]


def test_refetch_unchanged_content_leaves_hash_stable(client_and_store):
    client, store = client_and_store
    created = client.post("/docs", json={"url": "https://x.com/stable"}, headers=AUTH).json()
    doc_id = created["doc"]["id"]

    client.post(f"/docs/{doc_id}/refetch", headers=AUTH)
    first_hash = store.get_doc_content_meta(UUID(doc_id)).content_hash
    client.post(f"/docs/{doc_id}/refetch", headers=AUTH)
    assert store.get_doc_content_meta(UUID(doc_id)).content_hash == first_hash


def test_refetch_changed_content_updates_text_and_hash(client_and_store):
    client, store = client_and_store
    created = client.post("/docs", json={"url": "https://x.com/changed"}, headers=AUTH).json()
    doc_id = created["doc"]["id"]

    client.post(f"/docs/{doc_id}/refetch", headers=AUTH)
    before_meta = store.get_doc_content_meta(UUID(doc_id))

    class _ChangedFetchers:
        def fetch_for(self, source_id, url):
            return FetchResult(title="Fetched", content="edited body", content_snapshot="edited")

    client.app.dependency_overrides[get_fetchers] = lambda: _ChangedFetchers()
    client.post(f"/docs/{doc_id}/refetch", headers=AUTH)

    after_text = store.get_doc_content(UUID(doc_id))
    after_meta = store.get_doc_content_meta(UUID(doc_id))
    assert after_text == "edited body"
    assert after_meta.content_hash != before_meta.content_hash
