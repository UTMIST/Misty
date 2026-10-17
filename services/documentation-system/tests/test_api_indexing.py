"""Index lifecycle through the API (#175): triggers, retirement, source
deletion, and the admin POST /docs/index. Offline: the autouse
`fake_embedder` fixture stands in for the llm service."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from contracts.fetcher import FetchError, SourceGone
from src.api.app import create_app
from src.api.deps import get_directory, get_fetchers, get_storage
from src.api.hashing import generate_key
from src.content import content_hash
from src.storage.in_memory import InMemoryStorageAdapter
from tests.conftest import build_seed_sources
from tests.test_api_source_access import ADMIN, FakeDirectory, _result

URL = "https://docs.google.com/document/d/sponsors/edit"
LONG = "\n\n".join(
    ["# Sponsorship"] + [f"Paragraph {i}: " + "UTMIST sponsors fund CPSIF. " * 30 for i in range(6)]
)


class Fetchers:
    def __init__(self):
        self.result = _result("alice@example.com", content=LONG)
        self.error: Exception | None = None

    def fetch_for(self, source_id, url):
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    sources = [
        source.model_copy(update={"content_fetch_enabled": source.id == "gdocs"})
        for source in build_seed_sources()
    ]
    store = InMemoryStorageAdapter(seed_sources=sources)
    fetchers = Fetchers()
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_fetchers] = lambda: fetchers
    app.dependency_overrides[get_directory] = lambda: FakeDirectory()
    with TestClient(app) as client:
        yield client, store, fetchers


def _key(store, *scopes):
    plaintext, prefix, key_hash = generate_key()
    store.create_api_key(
        name=f"key-{uuid4()}", prefix=prefix, key_hash=key_hash, scopes=list(scopes), actor="t"
    )
    return {"X-API-Key": plaintext}


def _ingest(client, url=URL):
    response = client.post("/docs", headers=ADMIN, json={"url": url})
    assert response.status_code in (200, 201), response.text
    return response.json()


def _chunks(store, doc_id):
    return store.list_doc_chunks(UUID(doc_id))


def test_ingest_indexes_stored_google_content(ctx, fake_embedder):
    client, store, _ = ctx
    body = _ingest(client)

    assert body["warnings"] == []
    chunks = _chunks(store, body["doc"]["id"])
    assert len(chunks) > 1
    assert all(c.source_content_hash == content_hash(LONG) for c in chunks)
    assert all(c.chunk_text == LONG[c.start_offset : c.end_offset] for c in chunks)
    assert len(fake_embedder.calls) == 1


def test_repeated_ingest_of_unchanged_content_embeds_nothing(ctx, fake_embedder):
    client, store, _ = ctx
    doc_id = _ingest(client)["doc"]["id"]
    before = _chunks(store, doc_id)

    again = _ingest(client)

    assert again["created"] is False
    assert len(fake_embedder.calls) == 1
    assert _chunks(store, doc_id) == before


def test_embedding_failure_keeps_the_doc_and_warns_then_admin_retry_indexes(ctx, fake_embedder):
    client, store, _ = ctx
    fake_embedder.fail_on_call = 1

    body = _ingest(client)

    doc_id = body["doc"]["id"]
    assert body["created"] is True
    assert any(w.startswith("indexing failed") for w in body["warnings"])
    assert store.get_doc_content(UUID(doc_id)) == LONG
    assert _chunks(store, doc_id) == []

    retry = client.post("/docs/index", headers=ADMIN, json={})
    assert retry.status_code == 200
    assert retry.json()["indexed"] == 1
    assert len(_chunks(store, doc_id)) > 1


def test_refetch_with_shorter_content_replaces_the_set(ctx):
    client, store, fetchers = ctx
    doc_id = _ingest(client)["doc"]["id"]
    assert len(_chunks(store, doc_id)) > 1

    fetchers.result = _result("alice@example.com", content="# Sponsorship\n\nNow short.")
    assert client.post(f"/docs/{doc_id}/refetch", headers=ADMIN).status_code == 200

    chunks = _chunks(store, doc_id)
    assert [c.ordinal for c in chunks] == [0]
    assert chunks[0].chunk_text.endswith("Now short.")


def test_retiring_clears_and_reactivating_rebuilds(ctx):
    client, store, _ = ctx
    doc_id = _ingest(client)["doc"]["id"]
    count = len(_chunks(store, doc_id))

    assert client.patch(f"/docs/{doc_id}", headers=ADMIN, json={"active": False}).status_code == 200
    assert _chunks(store, doc_id) == []
    assert store.get_doc_content(UUID(doc_id)) == LONG

    assert client.patch(f"/docs/{doc_id}", headers=ADMIN, json={"active": True}).status_code == 200
    assert len(_chunks(store, doc_id)) == count


def test_confirmed_source_deletion_removes_content_and_chunks_for_good(ctx, fake_embedder):
    client, store, fetchers = ctx
    doc_id = _ingest(client)["doc"]["id"]
    fetchers.error = SourceGone("connectors returned 404: file not found for this url")

    assert client.post(f"/docs/{doc_id}/refetch", headers=ADMIN).status_code == 502

    assert store.get_doc(UUID(doc_id)) is not None
    assert store.get_doc_content(UUID(doc_id)) is None
    assert _chunks(store, doc_id) == []
    # Nothing left to rebuild: admin runs do not resurrect the chunks.
    assert (
        client.post("/docs/index", headers=ADMIN, json={"stale_only": False}).json()["attempted"]
        == 0
    )
    assert len(fake_embedder.calls) == 1


def test_transient_fetch_failure_is_not_a_deletion(ctx):
    client, store, fetchers = ctx
    doc_id = _ingest(client)["doc"]["id"]
    before = _chunks(store, doc_id)
    fetchers.error = FetchError("connectors returned 503: source not configured")

    assert client.post(f"/docs/{doc_id}/refetch", headers=ADMIN).status_code == 502

    assert store.get_doc_content(UUID(doc_id)) == LONG
    assert _chunks(store, doc_id) == before


def test_repeated_ingest_seeing_deletion_removes_content(ctx):
    client, store, fetchers = ctx
    doc_id = _ingest(client)["doc"]["id"]
    fetchers.error = SourceGone("connectors returned 404: file not found for this url")

    body = _ingest(client)

    assert any(w.startswith("source file not found") for w in body["warnings"])
    assert store.get_doc_content(UUID(doc_id)) is None
    assert _chunks(store, doc_id) == []


def test_scheduled_refresh_reindexes_and_handles_deletion(ctx, fake_embedder):
    client, store, fetchers = ctx
    changed = _ingest(client)["doc"]["id"]
    gone = _ingest(client, url="https://docs.google.com/document/d/gone/edit")["doc"]["id"]
    long_ago = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for doc_id in (changed, gone):
        store.update_doc(
            UUID(doc_id),
            {"source_access_synced_at": long_ago, "source_access_attempted_at": long_ago},
            actor="t",
        )

    class Split(Fetchers):
        def fetch_for(self, source_id, url):
            if "gone" in url:
                raise SourceGone("connectors returned 404: file not found for this url")
            return _result("alice@example.com", content="# Sponsorship\n\nRefreshed.")

    client.app.dependency_overrides[get_fetchers] = lambda: Split()
    fake_embedder.fail_on_call = len(fake_embedder.calls) + 1

    result = client.post("/docs/source-access/refresh-due", headers=ADMIN).json()

    assert result == {"attempted": 2, "refreshed": 1, "failed": 1, "index_failed": 1}
    assert store.get_doc_content(UUID(gone)) is None
    assert _chunks(store, gone) == []
    # The refreshed doc's old chunks stay until a retry succeeds.
    assert all(c.source_content_hash == content_hash(LONG) for c in _chunks(store, changed))
    assert client.post("/docs/index", headers=ADMIN, json={}).json()["indexed"] == 1
    assert _chunks(store, changed)[0].chunk_text.endswith("Refreshed.")


# --- POST /docs/index ---


@pytest.mark.parametrize("scopes", [(), ("docs:read", "docs:write")])
def test_index_requires_the_index_scope_before_any_provider_call(ctx, fake_embedder, scopes):
    client, store, _ = ctx
    fake_embedder.fail_on_call = 1  # Ingest leaves the doc stale.
    _ingest(client)
    calls = len(fake_embedder.calls)

    headers = _key(store, *scopes) if scopes else {}
    response = client.post("/docs/index", headers=headers, json={})

    assert response.status_code == (403 if scopes else 401)
    assert len(fake_embedder.calls) == calls


def test_index_scope_is_enough(ctx):
    client, store, _ = ctx
    assert client.post("/docs/index", headers=_key(store, "docs:index"), json={}).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"limit": 0},
        {"limit": 101},
        {"doc_ids": []},
        {"doc_ids": [str(uuid4())], "after": str(uuid4())},
        {"doc_ids": ["not-a-uuid"]},
        {"unknown": True},
    ],
)
def test_invalid_index_requests_fail_before_any_work(ctx, fake_embedder, body):
    client, _, _ = ctx
    assert client.post("/docs/index", headers=ADMIN, json=body).status_code == 422
    assert fake_embedder.calls == []


def test_unknown_doc_ids_fail_before_any_indexing(ctx, fake_embedder):
    client, store, _ = ctx
    fake_embedder.fail_on_call = 1
    doc_id = _ingest(client)["doc"]["id"]
    calls = len(fake_embedder.calls)

    response = client.post("/docs/index", headers=ADMIN, json={"doc_ids": [doc_id, str(uuid4())]})

    assert response.status_code == 404
    assert len(fake_embedder.calls) == calls
    assert _chunks(store, doc_id) == []


def test_index_named_docs_and_force_reembed(ctx, fake_embedder):
    client, store, _ = ctx
    doc_id = _ingest(client)["doc"]["id"]

    unchanged = client.post("/docs/index", headers=ADMIN, json={"doc_ids": [doc_id]}).json()
    assert unchanged["unchanged"] == 1
    assert unchanged["results"][0]["chunk_count"] == len(_chunks(store, doc_id))

    fake_embedder.model = "text-embedding-3-large"
    forced = client.post(
        "/docs/index", headers=ADMIN, json={"doc_ids": [doc_id], "force": True}
    ).json()
    assert forced["indexed"] == 1
    assert {c.embedding_model for c in _chunks(store, doc_id)} == {"text-embedding-3-large"}


def test_index_listing_pages_with_next_after(ctx, fake_embedder):
    client, store, _ = ctx
    fake_embedder.fail_on_call = 1
    ids = []
    for i in range(3):
        fake_embedder.fail_on_call = len(fake_embedder.calls) + 1
        ids.append(
            _ingest(client, url=f"https://docs.google.com/document/d/doc{i}/edit")["doc"]["id"]
        )
    fake_embedder.fail_on_call = None

    first = client.post("/docs/index", headers=ADMIN, json={"limit": 2}).json()
    assert first["attempted"] == 2
    assert first["next_after"] is not None
    rest = client.post(
        "/docs/index", headers=ADMIN, json={"limit": 2, "after": first["next_after"]}
    ).json()
    assert rest["attempted"] == 1
    assert rest["next_after"] is None
    assert sorted(r["doc_id"] for r in first["results"] + rest["results"]) == sorted(ids)
