from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from contracts.fetcher import FetchError, FetchResult
from src.api.app import create_app
from src.api.deps import get_directory, get_fetchers, get_storage
from src.storage.in_memory import InMemoryStorageAdapter
from tests.conftest import build_seed_sources
from tests.test_api_source_access import FakeDirectory, FakeFetchers, P1, P2, _result
from contracts.visibility import Actor

AUTH = {"X-API-Key": "test-key"}


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    store, fetchers = InMemoryStorageAdapter(build_seed_sources()), FakeFetchers()
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_fetchers] = lambda: fetchers
    app.dependency_overrides[get_directory] = lambda: FakeDirectory()
    with TestClient(app) as client:
        yield client, store, fetchers


def discover(client, file_id="file", source_id="gdocs"):
    response = client.post(
        "/docs/discovery", headers=AUTH, json={"file_id": file_id, "source_id": source_id}
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize(
    "source_id,url",
    [
        ("gdocs", "https://docs.google.com/document/d/file/edit"),
        ("gsheets", "https://docs.google.com/spreadsheets/d/file/edit"),
        ("gslides", "https://docs.google.com/presentation/d/file/edit"),
        ("gdrive", "https://drive.google.com/file/d/file/view"),
    ],
)
def test_discovery_stores_source_link_and_preserves_drive_identity(ctx, source_id, url):
    client, store, fetchers = ctx
    result = discover(client, source_id=source_id)
    doc = store.get_doc(UUID(result["doc_id"]))
    assert doc.url == url
    assert doc.url_normalized == "https://drive.google.com/file/d/file"
    assert fetchers.calls == [(source_id, url)]
    assert discover(client, source_id=source_id)["doc_id"] == str(doc.id)
    assert len(fetchers.calls) == len(store.list_docs()) == 1
    assert client.get(f"/docs/{doc.id}", headers=AUTH).json()["url"] == url


def test_discovery_indexes_with_actual_acl_and_skips_repeated_fetch(ctx, fake_embedder):
    client, store, fetchers = ctx
    result = discover(client)
    doc_id = UUID(result["doc_id"])
    assert result["status"] == "indexed"
    assert store.list_doc_chunks(doc_id, visibility=Actor(P1, frozenset()))
    assert not store.list_doc_chunks(doc_id, visibility=Actor(P2, frozenset()))
    assert all(g.origin == "google_drive" for g in store.list_grants(doc_id))
    assert store.get_doc(doc_id).created_by == "env-bootstrap"
    assert discover(client)["status"] == "existing"
    assert len(fetchers.calls) == len(fake_embedder.calls) == 1


def test_manual_legacy_alias_and_retired_records_are_not_reimported(ctx, fake_embedder):
    client, store, fetchers = ctx
    manual = client.post(
        "/docs",
        headers=AUTH,
        json={"url": "https://docs.google.com/document/d/file/edit?usp=sharing"},
    ).json()["doc"]
    doc_id = UUID(manual["id"])
    # Simulate a URL key persisted before identity normalization was introduced.
    store._docs[doc_id].url_normalized = manual["url"]
    assert discover(client)["doc_id"] == str(doc_id)
    assert len(store.list_docs()) == 1
    store.update_doc(doc_id, {"active": False}, actor="operator")
    assert discover(client)["status"] == "retired"
    assert len(fetchers.calls) == 1
    assert len(fake_embedder.calls) == 1


@pytest.mark.parametrize(
    "result",
    [
        FetchResult(title="restricted", content="secret", permissions=None),
        _result("unknown@example.com"),
        _result(None),
    ],
)
def test_missing_acl_or_unresolved_principals_never_store_content(ctx, result):
    client, store, fetchers = ctx
    fetchers.result = result
    imported = discover(client)
    assert imported["status"] == "registered"
    doc_id = UUID(imported["doc_id"])
    assert store.get_doc_content(doc_id) is None
    assert store.list_grants(doc_id) == []
    assert store.list_doc_chunks(doc_id) == []
    assert discover(client)["status"] == "registered"
    assert len(fetchers.calls) == 1


def test_disabled_source_and_validation_before_fetch(ctx):
    client, store, fetchers = ctx
    store._sources["gdocs"].content_fetch_enabled = False
    assert discover(client)["status"] == "disabled"
    store._sources["gdocs"].content_fetch_enabled = True
    store._sources["gdocs"].active = False
    assert discover(client)["status"] == "disabled"
    for extra in ({"created_by": "claimed"}, {"grants": [{"grantee_type": "org"}]}):
        assert (
            client.post(
                "/docs/discovery",
                headers=AUTH,
                json={"file_id": "file", "source_id": "gdocs", **extra},
            ).status_code
            == 422
        )
    assert (
        client.post("/docs/discovery", json={"file_id": "file", "source_id": "gdocs"}).status_code
        == 401
    )
    assert fetchers.calls == []
    assert store.list_docs() == []


def test_index_failure_is_observable_and_existing_index_endpoint_repairs(ctx, fake_embedder):
    client, store, fetchers = ctx
    fake_embedder.fail_on_call = 1
    result = discover(client)
    assert result["status"] == "index_failed"
    assert discover(client)["status"] == "index_failed"
    assert len(fetchers.calls) == len(fake_embedder.calls) == 1
    response = client.post("/docs/index", headers=AUTH, json={"doc_ids": [result["doc_id"]]})
    assert response.json()["indexed"] == 1
    assert discover(client)["status"] == "existing"


def test_fetch_failure_catalogues_once_and_refresh_owns_retry(ctx):
    client, store, fetchers = ctx
    original = fetchers.fetch_for

    def fail(*args):
        raise FetchError("unavailable")

    fetchers.fetch_for = fail
    result = discover(client)
    assert result["status"] == "registered"
    doc_id = UUID(result["doc_id"])
    fetchers.fetch_for = original
    store.update_doc(
        doc_id,
        {"source_access_attempted_at": datetime.now(timezone.utc) - timedelta(days=2)},
        actor="test",
    )
    refreshed = client.post("/docs/source-access/refresh-due", headers=AUTH)
    assert refreshed.json()["refreshed"] == 1
    assert discover(client)["status"] == "existing"
    assert len(store.list_docs()) == 1


def test_manual_ingest_aliases_reuse_discovered_file_identity(ctx):
    client, store, _ = ctx
    result = discover(client)
    for url in (
        "https://docs.google.com/document/d/file/edit?usp=sharing",
        "https://drive.google.com/open?id=file",
    ):
        response = client.post("/docs", headers=AUTH, json={"url": url})
        assert response.json()["created"] is False
        assert response.json()["doc"]["id"] == result["doc_id"]
    assert len(store.list_docs()) == 1


@pytest.mark.parametrize("account", ["", "u/0/"])
def test_manual_form_uses_google_acl_and_deduplicates_discovery(ctx, account):
    client, store, fetchers = ctx
    response = client.post(
        "/docs", headers=AUTH, json={"url": f"https://docs.google.com/forms/{account}d/form/edit"}
    )
    doc = response.json()["doc"]
    assert doc["source_id"] == "gdrive"
    assert fetchers.calls == [("gdrive", doc["url"])]
    assert all(g.origin == "google_drive" for g in store.list_grants(UUID(doc["id"])))
    assert discover(client, "form", "gdrive")["doc_id"] == doc["id"]
    assert len(store.list_docs()) == len(fetchers.calls) == 1


def test_legacy_web_form_enters_refresh_and_preserves_identity_and_manual_grants(ctx):
    client, store, fetchers = ctx
    # Explicit web simulates a row created before Forms source derivation was fixed.
    fetchers.result = FetchResult(title="Old web title", content="Old web content")
    old = client.post(
        "/docs",
        headers=AUTH,
        json={
            "url": "https://docs.google.com/forms/u/0/d/form/edit",
            "source_id": "web",
            "tags": ["legacy"],
            "grants": [{"grantee_type": "person", "grantee_id": str(P2)}],
        },
    ).json()["doc"]
    doc_id = UUID(old["id"])
    grants = store.list_grants(doc_id)
    assert store.get_doc_index_states(doc_id)
    assert discover(client, "form", "gdrive")["status"] == "registered"
    assert discover(client, "form", "gdrive")["status"] == "registered"
    assert len(fetchers.calls) == 1
    repaired = store.get_doc(doc_id)
    assert repaired.source_id == "gdrive"
    assert repaired.tags == ["legacy"]
    assert repaired.grants == grants
    assert repaired.source_access_synced_at is None
    assert store.get_doc_content(doc_id) == "Old web content"

    # Missing ACL fails closed; the repaired record stays eligible for recovery.
    fetchers.result = FetchResult(title="Private", content="private without ACL")
    failed = client.post("/docs/source-access/refresh-due", headers=AUTH).json()
    assert failed["failed"] == 1
    assert store.list_grants(doc_id) == grants
    assert store.get_doc_content(doc_id) == "Old web content"
    assert discover(client, "form", "gdrive")["status"] == "registered"
    store.update_doc(
        doc_id,
        {"source_access_attempted_at": datetime.now(timezone.utc) - timedelta(days=2)},
        actor="test",
    )
    fetchers.result = _result("alice@example.com", content="Recovered Form")
    refreshed = client.post("/docs/source-access/refresh-due", headers=AUTH).json()
    assert refreshed["refreshed"] == 1
    assert refreshed["index_failed"] == 0
    assert discover(client, "form", "gdrive")["status"] == "existing"
    assert len(store.list_docs()) == 1
    assert store.get_doc_content(doc_id) == "Recovered Form"
    assert [g for g in store.list_grants(doc_id) if g.origin == "manual"] == grants
    assert store.list_doc_chunks(doc_id, visibility=Actor(P1, frozenset()))
    assert not store.list_doc_chunks(doc_id, visibility=Actor(UUID(int=3), frozenset()))
    assert fetchers.calls[-1] == ("gdrive", old["url"])


def test_disabled_discovery_source_does_not_reclassify_legacy_record(ctx):
    client, store, _ = ctx
    old = client.post(
        "/docs",
        headers=AUTH,
        json={"url": "https://docs.google.com/forms/d/form/edit", "source_id": "web"},
    ).json()["doc"]
    store._sources["gdrive"].content_fetch_enabled = False
    assert discover(client, "form", "gdrive")["status"] == "disabled"
    assert store.get_doc(UUID(old["id"])).source_id == "web"


def test_discovered_content_is_searchable_only_with_actual_person_permission(ctx):
    from types import SimpleNamespace

    from contracts.retrieval import QueryEmbedding
    from src.api.deps import get_embeddings
    from src.api.hashing import generate_key
    from tests.fake_embedder import FAKE_MODEL, fake_vector

    client, store, _ = ctx
    result = discover(client)
    client.app.dependency_overrides[get_embeddings] = lambda: SimpleNamespace(
        embed_query=lambda query: QueryEmbedding(model=FAKE_MODEL, vector=fake_vector(query))
    )
    key, prefix, key_hash = generate_key()
    store.create_api_key(
        name="search-test",
        prefix=prefix,
        key_hash=key_hash,
        scopes=["docs:read", "act-as-user"],
        actor="test",
    )
    for person, expected in ((P1, [result["doc_id"]]), (P2, [])):
        response = client.post(
            "/search",
            json={"query": "old content"},
            headers={"X-API-Key": key, "X-On-Behalf-Of": str(person)},
        )
        assert response.status_code == 200
        assert [hit["doc_id"] for hit in response.json()["results"]] == expected


@pytest.mark.parametrize("retired", [False, True])
def test_concurrent_registration_is_reported_truthfully(ctx, monkeypatch, retired):
    client, store, fetchers = ctx
    fetchers.result = _result(None)
    result = discover(client)
    doc_id = UUID(result["doc_id"])
    if retired:
        store.update_doc(doc_id, {"active": False}, actor="operator")
    original = store.get_doc_by_google_file_id
    calls = 0

    def racing_lookup(file_id):
        nonlocal calls
        calls += 1
        return None if calls == 1 else original(file_id)

    monkeypatch.setattr(store, "get_doc_by_google_file_id", racing_lookup)
    assert discover(client)["status"] == ("retired" if retired else "registered")
    assert len(fetchers.calls) == 1
    assert len(store.list_docs(active_only=False)) == 1


def test_http_runner_imports_nested_shares_through_catalog_and_reuses_refresh(ctx, fake_embedder):
    import json

    import httpx
    from pydantic import SecretStr

    from src.discovery import DiscoveryClient, Settings, State, run_discovery

    client, store, fetchers = ctx
    listings = {
        None: [
            {"file_id": "direct", "kind": "file", "source_id": "gdocs"},
            {"file_id": "folder", "kind": "folder", "source_id": None},
        ],
        "folder": [{"file_id": "child", "kind": "file", "source_id": "gdocs"}],
    }

    def transport(request):
        if request.url.path == "/discover/google":
            folder_id = json.loads(request.content)["folder_id"]
            return httpx.Response(
                200,
                json={"items": listings[folder_id], "next_page_token": None, "incomplete": False},
            )
        return client.request(
            request.method, request.url.path, headers=dict(request.headers), content=request.content
        )

    settings = Settings(
        docs_api_key=SecretStr("test-key"), connectors_api_key=SecretStr("fake-discovery-key")
    )
    state = State(identity="default")
    runner = DiscoveryClient(settings, httpx.Client(transport=httpx.MockTransport(transport)))
    assert run_discovery(runner, settings, state, lambda: None) == 0
    assert state.counts["indexed"] == 2
    assert len(store.list_docs()) == 2
    listings["folder"].append({"file_id": "later", "kind": "file", "source_id": "gdocs"})
    assert run_discovery(runner, settings, state, lambda: None) == 0
    assert state.counts["existing"] == 2
    assert state.counts["indexed"] == 1
    assert len(fetchers.calls) == len(fake_embedder.calls) == 3
    for doc in store.list_docs():
        assert store.list_doc_chunks(doc.id, visibility=Actor(P1, frozenset()))
        assert not store.list_doc_chunks(doc.id, visibility=Actor(P2, frozenset()))
