from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from contracts.embedder import EmbedderRejected
from src.chunking import chunk_text
from src.content import content_hash
from src.indexing import clear_doc_index, embedding_input, index_doc, index_warning
from src.storage.in_memory import InMemoryStorageAdapter
from tests.chunk_storage_cases import make_doc
from tests.conftest import build_seed_sources
from tests.fake_embedder import FAKE_MODEL, FakeEmbedder, fake_vector

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
