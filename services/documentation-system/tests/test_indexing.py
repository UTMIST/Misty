from dataclasses import replace
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.exc import OperationalError

from contracts.embedder import EmbedderRejected
from src.chunking import chunk_text
from src.content import content_hash
from src.embed.http_client import HttpEmbedder
from src.indexing import clear_doc_index, embedding_input, index_doc, index_warning
from src.storage.in_memory import InMemoryStorageAdapter
from tests.chunk_storage_cases import make_doc
from tests.conftest import build_seed_sources
from tests.fake_embedder import FAKE_MODEL, FakeEmbedder, fake_vector
from tests.test_embed_client import _ok

# The shape the connectors Google Docs extractor stores: markdown headings,
# paragraphs, and lists. Long enough for several ~500-token chunks.
GOOGLE_DOC = "\n\n".join(
    [
        "# UTMIST Sponsorship Guide",
        "UTMIST partners with companies to fund CPSIF and the annual conference. " * 12,
        "## Tiers",
        "\n".join(
            f"- Tier {i}: benefits include booth space and logo placement." for i in range(30)
        ),
        "## Contacts",
        "Email the partnerships team before committing to any sponsor deliverable. " * 20,
    ]
)


@pytest.fixture
def storage():
    return InMemoryStorageAdapter(seed_sources=build_seed_sources())


@pytest.fixture
def embedder():
    return FakeEmbedder()


def _doc(storage, text=GOOGLE_DOC):
    doc = make_doc(storage)
    _store(storage, doc.id, text)
    return doc


def _store(storage, doc_id, text):
    storage.upsert_doc_content(
        doc_id, content_text=text, content_hash=content_hash(text), fetched_at=None
    )


def test_stored_text_is_chunked_embedded_and_persisted(storage, embedder):
    doc = _doc(storage)

    outcome = index_doc(doc.id, storage=storage, embedder=embedder)

    expected = chunk_text(GOOGLE_DOC).chunks
    assert len(expected) > 2
    assert outcome.status == "indexed"
    assert outcome.chunk_count == len(expected)
    stored = storage.list_doc_chunks(doc.id)
    assert [c.ordinal for c in stored] == list(range(len(expected)))
    for chunk, source in zip(stored, expected, strict=True):
        assert (chunk.start_offset, chunk.end_offset) == (source.start, source.end)
        assert chunk.chunk_text == GOOGLE_DOC[chunk.start_offset : chunk.end_offset]
        assert chunk.source_content_hash == content_hash(GOOGLE_DOC)
        assert chunk.embedding_model == FAKE_MODEL
        assert chunk.dimensions == 1536
        # Embedded with title + heading context, stored without it.
        assert chunk.embedding == pytest.approx(
            fake_vector(embedding_input(doc.title, source)), abs=1e-6
        )
    assert embedder.calls == [[embedding_input(doc.title, c) for c in expected]]


def test_embedding_input_adds_title_and_heading_path():
    chunk = replace(chunk_text("Gold tier.").chunks[0], heading_path=("Guide", "Tiers"))
    assert embedding_input("Sponsors", chunk) == "Sponsors\nGuide > Tiers\n\nGold tier."
    no_heading = replace(chunk, heading_path=())
    assert embedding_input("Sponsors", no_heading) == "Sponsors\n\nGold tier."


@pytest.mark.parametrize("title", [None, "Long title " * 4_000], ids=["no-title", "long-title"])
def test_long_context_indexes_without_changing_source_chunks(storage, title):
    # A late long heading used to fail after an earlier batch had been paid
    # for. Use enough ordinary text to cross the HTTP client's batch boundary.
    text = (
        "\n\n".join("ordinary paragraph " * 100 for _ in range(100))
        + "\n\n# "
        + "long heading " * 3_000
        + "\n\nFinal paragraph."
    )
    doc = _doc(storage, text)
    storage.update_doc(doc.id, {"title": title}, actor="test")
    inputs = []

    def embed(request):
        body = json.loads(request.content)
        if any(len(value) > 32_000 for value in body["inputs"]):
            return httpx.Response(422, json={"detail": "input exceeds the LLM character limit"})
        inputs.extend(body["inputs"])
        return _ok(body["inputs"])

    with httpx.Client(transport=httpx.MockTransport(embed)) as client:
        outcome = index_doc(
            doc.id, storage=storage, embedder=HttpEmbedder("http://llm", "k", client=client)
        )

    assert outcome.status == "indexed"
    stored = storage.list_doc_chunks(doc.id)
    assert len(stored) > 96
    assert len(inputs) == len(stored)
    assert storage.get_doc_content(doc.id) == text
    for value, chunk in zip(inputs, stored, strict=True):
        assert value.endswith("\n\n" + chunk.chunk_text)
        assert len(value) - len(chunk.chunk_text) <= 1_002  # bounded context + separator
        assert chunk.chunk_text == text[chunk.start_offset : chunk.end_offset]
        assert chunk.source_content_hash == content_hash(text)


def test_reindexing_unchanged_content_makes_no_call_and_no_duplicates(storage, embedder):
    doc = _doc(storage)
    index_doc(doc.id, storage=storage, embedder=embedder)
    before = storage.list_doc_chunks(doc.id)

    outcome = index_doc(doc.id, storage=storage, embedder=embedder)

    assert outcome.status == "unchanged"
    assert outcome.chunk_count == len(before)
    assert len(embedder.calls) == 1
    assert storage.list_doc_chunks(doc.id) == before


def test_force_reembeds_unchanged_content(storage, embedder):
    doc = _doc(storage)
    index_doc(doc.id, storage=storage, embedder=embedder)
    embedder.model = "text-embedding-3-large"

    outcome = index_doc(doc.id, storage=storage, embedder=embedder, force=True)

    assert outcome.status == "indexed"
    assert {c.embedding_model for c in storage.list_doc_chunks(doc.id)} == {
        "text-embedding-3-large"
    }


def test_old_title_embedding_cannot_overwrite_a_newer_index(storage, embedder, monkeypatch):
    doc = _doc(storage)
    original_embed = embedder.embed
    newer_chunks = []

    def embed_then_rename(inputs):
        old_batch = original_embed(inputs)
        storage.update_doc(doc.id, {"title": "New title"}, actor="test")
        assert index_doc(doc.id, storage=storage, embedder=FakeEmbedder()).status == "indexed"
        newer_chunks.extend(storage.list_doc_chunks(doc.id))
        return old_batch

    monkeypatch.setattr(embedder, "embed", embed_then_rename)
    assert index_doc(doc.id, storage=storage, embedder=embedder).status == "skipped"
    assert storage.list_doc_chunks(doc.id) == newer_chunks


def test_shorter_content_replaces_the_set_and_drops_tail_chunks(storage, embedder):
    doc = _doc(storage)
    index_doc(doc.id, storage=storage, embedder=embedder)
    assert len(storage.list_doc_chunks(doc.id)) > 1

    _store(storage, doc.id, "# Sponsorship\n\nNow a single short paragraph.")
    outcome = index_doc(doc.id, storage=storage, embedder=embedder)

    assert outcome.status == "indexed"
    stored = storage.list_doc_chunks(doc.id)
    assert [c.ordinal for c in stored] == [0]
    assert stored[0].chunk_text.endswith("Now a single short paragraph.")


@pytest.mark.parametrize("state", ["retired", "no_content", "missing", "blank"])
def test_docs_without_indexable_text_end_with_no_chunks(storage, embedder, state):
    doc = _doc(storage)
    index_doc(doc.id, storage=storage, embedder=embedder)
    calls = len(embedder.calls)
    doc_id = doc.id
    if state == "retired":
        storage.update_doc(doc.id, {"active": False}, actor="test")
    elif state == "no_content":
        doc_id = make_doc(storage).id
        storage.replace_doc_chunks(doc_id, storage.list_doc_chunks(doc.id))
    elif state == "missing":
        doc_id = uuid4()
    else:
        _store(storage, doc.id, "  \n\n ")

    outcome = index_doc(doc_id, storage=storage, embedder=embedder)

    assert outcome.status == "cleared"
    assert storage.list_doc_chunks(doc_id) == []
    assert len(embedder.calls) == calls


def test_embedding_failure_keeps_the_previous_set_and_is_retryable(storage):
    doc = _doc(storage)
    index_doc(doc.id, storage=storage, embedder=FakeEmbedder())
    previous = storage.list_doc_chunks(doc.id)
    _store(storage, doc.id, GOOGLE_DOC + "\n\nA new closing paragraph.")

    failing = FakeEmbedder(fail_on_call=1)
    outcome = index_doc(doc.id, storage=storage, embedder=failing)

    assert outcome.status == "failed"
    assert "unavailable" in outcome.detail
    assert index_warning(outcome).endswith("retry with POST /docs/index")
    assert storage.list_doc_chunks(doc.id) == previous

    retried = index_doc(doc.id, storage=storage, embedder=failing)
    assert retried.status == "indexed"
    assert storage.list_doc_chunks(doc.id)[-1].chunk_text.endswith("A new closing paragraph.")


@pytest.mark.parametrize("stale_state", ["retired", "no_content"])
def test_delayed_cleanup_preserves_a_newer_successful_index(
    storage, embedder, monkeypatch, stale_state
):
    doc = _doc(storage)
    if stale_state == "retired":
        storage.update_doc(doc.id, {"active": False}, actor="test")
        read_name = "get_doc"
    else:
        storage.delete_doc_content(doc.id, expected=storage.get_doc_content_meta(doc.id))
        read_name = "get_doc_content"
    original_read = getattr(storage, read_name)
    rebuilt = []

    def read_then_rebuild(doc_id, **kwargs):
        stale = original_read(doc_id, **kwargs)
        monkeypatch.setattr(storage, read_name, original_read)
        storage.update_doc(doc_id, {"active": True}, actor="test")
        _store(storage, doc_id, "Fresh content from a completed refetch.")
        assert index_doc(doc_id, storage=storage, embedder=embedder).status == "indexed"
        rebuilt.extend(storage.list_doc_chunks(doc_id))
        return stale

    monkeypatch.setattr(storage, read_name, read_then_rebuild)

    outcome = index_doc(doc.id, storage=storage, embedder=embedder)

    assert outcome.status == "skipped"
    assert rebuilt
    assert storage.list_doc_chunks(doc.id) == rebuilt


def test_rejected_embedding_is_a_failure_not_an_exception(storage):
    class Rejecting(FakeEmbedder):
        def embed(self, inputs):
            raise EmbedderRejected("llm service returned 422")

    doc = _doc(storage)
    outcome = index_doc(doc.id, storage=storage, embedder=Rejecting())
    assert outcome.status == "failed"
    assert storage.list_doc_chunks(doc.id) == []


def test_storage_failure_is_reported_and_writes_nothing(storage, embedder, monkeypatch):
    doc = _doc(storage)

    def broken(*args, **kwargs):
        raise OperationalError("INSERT", {}, Exception("connection lost"))

    monkeypatch.setattr(storage, "replace_doc_chunks_if_current", broken)
    outcome = index_doc(doc.id, storage=storage, embedder=embedder)
    assert outcome.status == "failed"
    assert outcome.detail == "index write failed: OperationalError"
    assert storage.list_doc_chunks(doc.id) == []


def test_content_changed_while_embedding_is_skipped(storage):
    doc = _doc(storage)

    class RacingEmbedder(FakeEmbedder):
        def embed(self, inputs):
            # A refetch stores newer text while this index is embedding.
            _store(storage, doc.id, "newer text from a concurrent refetch")
            return super().embed(inputs)

    outcome = index_doc(doc.id, storage=storage, embedder=RacingEmbedder())

    assert outcome.status == "skipped"
    assert storage.list_doc_chunks(doc.id) == []


def test_clear_doc_index_removes_chunks(storage, embedder):
    doc = _doc(storage)
    index_doc(doc.id, storage=storage, embedder=embedder)
    assert clear_doc_index(doc.id, storage=storage).status == "cleared"
    assert storage.list_doc_chunks(doc.id) == []


# --- Re-embedding after a model change (#209) ---

LARGE_MODEL = "text-embedding-3-large"


def _models(storage, doc_id):
    return sorted(storage.get_doc_index_states(doc_id))


def test_failing_target_never_holds_back_the_served_set(storage, embedder):
    doc = _doc(storage)
    target = FakeEmbedder(model=LARGE_MODEL, fail_on_call=1)

    outcome = index_doc(doc.id, storage=storage, embedder=embedder, target=target)

    assert outcome.status == "failed"
    assert LARGE_MODEL in outcome.detail
    assert _models(storage, doc.id) == [FAKE_MODEL]
    assert doc.id in storage.list_docs_for_indexing(models=[FAKE_MODEL, LARGE_MODEL])


def test_interrupted_reembed_resumes_without_repeating_finished_docs(storage, embedder):
    docs = [_doc(storage, f"Doc {i}: UTMIST sponsors fund CPSIF.") for i in range(3)]
    for doc in docs:
        index_doc(doc.id, storage=storage, embedder=embedder)
    target = FakeEmbedder(model=LARGE_MODEL, fail_on_call=2)
    models = [FAKE_MODEL, LARGE_MODEL]

    def run():
        # What an operator's POST /docs/index loop does, one listing page.
        due = storage.list_docs_for_indexing(models=models)
        return [index_doc(d, storage=storage, embedder=embedder, target=target).status for d in due]

    assert run() == ["indexed", "failed", "indexed"]
    assert run() == ["indexed"]  # only the doc that failed
    assert run() == []
    assert len(target.calls) == 4  # three docs plus the one failed attempt
    assert len(embedder.calls) == 3  # served sets were never re-embedded
    assert all(_models(storage, d.id) == [LARGE_MODEL, FAKE_MODEL] for d in docs)
