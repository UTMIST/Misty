"""Model transitions through real APIs, shared by both storage adapters."""

import sys
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from contracts.embedder import RESOLVED_EMBED_MODELS
from contracts.retrieval import QueryEmbedding
from contracts.visibility import Actor
from src.api import deps
from src.api.app import create_app
from src.content import content_hash
from src.indexing import index_doc
from tests.fake_embedder import FakeEmbedder, FAKE_MODEL
from tests.meeting_record_cases import Directory, Writer, payload
from tests.test_api_discovery import discover
from tests.test_api_indexing import Fetchers, _ingest, _key, _configure
from tests.test_api_source_access import ADMIN, P1, FakeDirectory, _result

LARGE = "text-embedding-3-large"


class ReembedIntegrationCases:
    @pytest.fixture
    def flow(self, adapter, monkeypatch, fake_embedder):
        monkeypatch.setenv("API_KEY", "test-key")
        storage = adapter
        embedders = {}

        def build(*args, model, **kwargs):
            return embedders.setdefault(model, FakeEmbedder(model=RESOLVED_EMBED_MODELS[model]))

        monkeypatch.setattr(deps, "HttpEmbedder", build)
        _configure(monkeypatch, "openai-embed-3-small")
        fetchers = Fetchers()
        app = create_app()
        app.dependency_overrides[deps.get_storage] = lambda: storage
        app.dependency_overrides[deps.get_fetchers] = lambda: fetchers
        app.dependency_overrides[deps.get_directory] = FakeDirectory
        with TestClient(app) as client:
            yield SimpleNamespace(
                storage=storage,
                client=client,
                fetchers=fetchers,
                embedders=embedders,
                headers=_key(storage, "docs:index"),
                adapter=type(storage).__name__,
            )

    def test_retention_window_keeps_old_readers_through_inline_indexing(self, flow, monkeypatch):
        f = flow
        doc_id = UUID(_ingest(f.client)["doc"]["id"])
        old_chunks = f.storage.list_doc_chunks(doc_id)
        old_query = QueryEmbedding(model=FAKE_MODEL, vector=old_chunks[0].embedding)
        visibility = Actor(P1, frozenset())

        def old_reader():
            return f.storage.search_chunks(
                query="CPSIF", embedding=old_query, visibility=visibility, k=10, min_cosine=0.3
            )

        assert any(h.doc_id == doc_id for h in old_reader())
        _configure(monkeypatch, "openai-embed-3-small", "openai-embed-3-large")
        assert run_index(f)["indexed"] == 1
        assert status(f)["pending_docs"] == 0
        _configure(monkeypatch, "openai-embed-3-large", "openai-embed-3-small")
        before = count_calls(f)
        _ingest(f.client)
        assert count_calls(f) == before
        assert set(f.storage.get_doc_index_states(doc_id)) == {FAKE_MODEL, LARGE}
        assert any(h.doc_id == doc_id for h in old_reader())
        replacement = "CPSIF updated during the old-reader drain window."
        f.fetchers.result = _result("alice@example.com", content=replacement)
        assert f.client.post(f"/docs/{doc_id}/refetch", headers=ADMIN).status_code == 200
        assert all(
            s.source_content_hash == content_hash(replacement)
            for s in f.storage.get_doc_index_states(doc_id).values()
        )
        assert set(f.storage.get_doc_index_states(doc_id)) == {FAKE_MODEL, LARGE}
        assert any(h.doc_id == doc_id for h in old_reader())
        assert (
            f.client.patch(
                f"/docs/{doc_id}", headers=ADMIN, json={"title": "CPSIF updated title"}
            ).status_code
            == 200
        )
        assert set(f.storage.get_doc_index_states(doc_id)) == {FAKE_MODEL, LARGE}
        assert all(
            s.source_title_hash == content_hash("CPSIF updated title")
            for s in f.storage.get_doc_index_states(doc_id).values()
        )
        _configure(monkeypatch, "openai-embed-3-small", "openai-embed-3-large")
        assert status(f)["pending_docs"] == 0  # rollback during retention needs no repair
        _configure(monkeypatch, "openai-embed-3-large")
        before = count_calls(f)
        assert run_index(f)["unchanged"] == 1
        assert count_calls(f) == before
        assert set(f.storage.get_doc_index_states(doc_id)) == {LARGE}
        assert status(f)["pending_docs"] == 0

    def test_every_python_whitespace_character_clears_old_sets_then_is_not_pending(
        self, flow, monkeypatch
    ):
        f = flow
        _configure(monkeypatch, "openai-embed-3-small", "openai-embed-3-large")
        doc_id = UUID(_ingest(f.client)["doc"]["id"])
        blanks = [chr(i) for i in range(sys.maxunicode + 1) if chr(i).isspace()]
        for blank in [*blanks, "".join(blanks), ""]:
            body = "CPSIF older indexed content"
            f.storage.upsert_doc_content(
                doc_id, content_text=body, content_hash=content_hash(body), fetched_at=None
            )
            assert index_doc(
                doc_id,
                storage=f.storage,
                embedder=deps.get_embedder(),
                target=deps.get_target_embedder(),
            ).status in ("indexed", "unchanged")
            assert set(f.storage.get_doc_index_states(doc_id)) == {FAKE_MODEL, LARGE}
            f.storage.upsert_doc_content(
                doc_id, content_text=blank, content_hash=content_hash(blank), fetched_at=None
            )
            before = count_calls(f)
            assert status(f)["pending_docs"] == 1, repr(blank)
            result = run_index(f)
            assert result["attempted"] == result["cleared"] == 1, (repr(blank), result)
            assert not f.storage.list_doc_chunks(doc_id), repr(blank)
            for _ in range(2):
                assert status(f)["pending_docs"] == 0, repr(blank)
                assert run_index(f)["attempted"] == 0, repr(blank)
            assert count_calls(f) == before, repr(blank)
        for nonblank in ["\u200b", "\ufeff", "\u180e", "\u00a0x\u3000"]:
            assert nonblank.strip()
            f.storage.upsert_doc_content(
                doc_id, content_text=nonblank, content_hash=content_hash(nonblank), fetched_at=None
            )
            assert status(f)["pending_docs"] == 1, repr(nonblank)

    def test_discovery_builds_both_sets_and_checks_the_served_model(self, flow, monkeypatch):
        f = flow
        _configure(monkeypatch, "openai-embed-3-small", "openai-embed-3-large")
        result = discover(f.client)
        assert result["status"] == "indexed"
        doc_id = UUID(result["doc_id"])
        assert set(f.storage.get_doc_index_states(doc_id)) == {FAKE_MODEL, LARGE}
        before = count_calls(f)
        assert discover(f.client)["status"] == "existing"
        f.storage.delete_doc_chunks_except(doc_id, [LARGE])
        assert discover(f.client)["status"] == "index_failed"
        assert count_calls(f) == before
        assert run_index(f)["indexed"] == 1
        assert discover(f.client)["status"] == "existing"
        assert len(f.storage.list_docs()) == 1

    def test_meeting_target_failure_retry_and_removal_purge_both_models(self, flow, monkeypatch):
        f = flow
        _configure(monkeypatch, "openai-embed-3-small", "openai-embed-3-large")
        target = deps.get_target_embedder()
        target.fail_on_call = 1
        writer = Writer()
        f.client.app.dependency_overrides[deps.get_meeting_writer] = lambda: writer
        f.client.app.dependency_overrides[deps.get_directory] = Directory
        meeting_id = uuid4()
        path = f"/meeting-records/{meeting_id}"
        response = f.client.put(path, headers=ADMIN, json=payload().model_dump(mode="json"))
        assert response.status_code == 202, response.text
        saved = f.storage.get_meeting(meeting_id)
        assert saved.persistence_status == "saved" and saved.index_pending
        assert set(f.storage.get_doc_index_states(saved.doc_id)) == {FAKE_MODEL}
        assert f.client.post(path + "/retry", headers=ADMIN).status_code == 200
        assert not f.storage.get_meeting(meeting_id).index_pending
        assert set(f.storage.get_doc_index_states(saved.doc_id)) == {FAKE_MODEL, LARGE}
        assert len(f.embedders["openai-embed-3-small"].calls) == 1
        assert len(target.calls) == 2
        assert f.client.post(path + "/remove", headers=ADMIN).status_code == 200
        assert f.storage.get_doc(saved.doc_id).purged_at is not None
        assert f.storage.get_doc_content(saved.doc_id) is None
        assert f.storage.list_doc_chunks(saved.doc_id) == []
        assert f.storage.list_grants(saved.doc_id) == []
        # The meeting's registration fence must still reject an alternate URL.
        rejected = f.client.post(
            "/docs",
            headers=ADMIN,
            json={"url": f"https://drive.google.com/open?id={saved.google_document_id}"},
        )
        assert rejected.status_code == 400, rejected.text
        assert rejected.json()["detail"] == "document was permanently purged"
        assert len(writer.created) == 1


def count_calls(f):
    return sum(len(e.calls) for e in f.embedders.values())


def status(f):
    r = f.client.get("/docs/index/status", headers=f.headers)
    assert r.status_code == 200, r.text
    return r.json()


def run_index(f):
    r = f.client.post("/docs/index", headers=f.headers, json={})
    assert r.status_code == 200, r.text
    return r.json()
