"""The same chunk persistence assertions run in both adapter suites."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from contracts.types import MAX_CHUNK_CHARS, DocChunk, SourceGrant
from contracts.visibility import Actor, DENY, SEE_ALL
from src.content import content_hash
from tests.fake_embedder import FAKE_MODEL

LARGE_MODEL = "text-embedding-3-large"


def make_chunk(ordinal=0, *, text="UTMIST CPSIF", start=0, source_text=None, **values):
    return DocChunk(
        **{
            "ordinal": ordinal,
            "chunk_text": text,
            "start_offset": start,
            "end_offset": start + len(text),
            "source_content_hash": content_hash(source_text or text),
            "source_title_hash": content_hash("Chunk storage test"),
            "embedding": [0.1, -0.2, 0.3] + [0.0] * 1533,
            "embedding_model": "text-embedding-3-small",
            "dimensions": 1536,
            **values,
        }
    )


def make_doc(adapter):
    url = f"https://example.com/{uuid4()}"
    return adapter.create_doc(
        url=url,
        url_normalized=url,
        source_id="web",
        title="Chunk storage test",
        description=None,
        owning_team_id=None,
        owning_team_label=None,
        owning_person_id=None,
        owning_person_label=None,
        content_snapshot=None,
        fetched_at=None,
        tags=[],
        actor="test",
    )


class ChunkStorageCases:
    def test_chunk_round_trip_with_unicode_offsets_and_order(self, adapter):
        doc = make_doc(adapter)
        source = "α😀\r\nUTMIST CPSIF"
        chunks = [
            make_chunk(0, text=source[:10], source_text=source),
            make_chunk(1, text=source[4:], start=4, source_text=source),
        ]
        assert adapter.replace_doc_chunks(doc.id, list(reversed(chunks))) is True
        assert adapter.list_doc_chunks(doc.id) == chunks
        for chunk in adapter.list_doc_chunks(doc.id):
            assert chunk.chunk_text == source[chunk.start_offset : chunk.end_offset]
            assert chunk.embedding[:3] == pytest.approx([0.1, -0.2, 0.3])

    def test_chunk_replacement_is_idempotent_removes_old_rows_and_can_clear(self, adapter):
        doc, other = make_doc(adapter), make_doc(adapter)
        chunks = [make_chunk(0), make_chunk(1)]
        adapter.replace_doc_chunks(other.id, chunks)
        adapter.replace_doc_chunks(doc.id, chunks)
        adapter.replace_doc_chunks(doc.id, chunks)
        assert adapter.list_doc_chunks(doc.id) == chunks
        replacement = [make_chunk(text="short", embedding_model="text-embedding-3-large")]
        adapter.replace_doc_chunks(doc.id, replacement)
        assert adapter.list_doc_chunks(doc.id) == replacement
        assert adapter.replace_doc_chunks(doc.id, []) is True
        assert adapter.list_doc_chunks(doc.id) == []
        assert adapter.list_doc_chunks(other.id) == chunks

    def test_chunks_for_missing_and_unindexed_docs(self, adapter):
        missing = uuid4()
        assert adapter.list_doc_chunks(missing) == []
        assert adapter.replace_doc_chunks(missing, [make_chunk()]) is False
        assert adapter.replace_doc_chunks(missing, []) is False
        assert adapter.list_doc_chunks(make_doc(adapter).id) == []

    @pytest.mark.parametrize("kind", ["person", "team", "org"])
    def test_chunk_reads_use_doc_visibility(self, adapter, kind):
        doc, hidden = make_doc(adapter), make_doc(adapter)
        chunks = [make_chunk()]
        adapter.replace_doc_chunks(doc.id, chunks)
        adapter.replace_doc_chunks(hidden.id, chunks)
        person, team = uuid4(), uuid4()
        actor = Actor(person_id=person, team_ids=frozenset({team}))
        assert adapter.list_doc_chunks(doc.id, visibility=actor) == []
        grantee = {"person": person, "team": team, "org": None}[kind]
        adapter.add_grant(doc.id, grantee_type=kind, grantee_id=grantee, actor="test")
        assert adapter.list_doc_chunks(doc.id, visibility=actor) == chunks
        assert adapter.list_doc_chunks(hidden.id, visibility=actor) == []
        assert adapter.list_doc_chunks(doc.id, visibility=DENY) == []
        assert adapter.list_doc_chunks(hidden.id, visibility=SEE_ALL) == chunks

    @pytest.mark.parametrize("owner_field", ["owning_person_id", "owning_team_id"])
    def test_chunk_ownership_requires_an_independent_grant(self, adapter, owner_field):
        doc = make_doc(adapter)
        chunks = [make_chunk()]
        adapter.replace_doc_chunks(doc.id, chunks)
        person, team = uuid4(), uuid4()
        actor = Actor(person, frozenset({team}))
        owner = person if owner_field == "owning_person_id" else team
        adapter.update_doc(doc.id, {owner_field: owner}, actor="test")
        assert adapter.list_doc_chunks(doc.id, visibility=actor) == []
        adapter.add_grant(doc.id, grantee_type="person", grantee_id=person, actor="test")
        assert adapter.list_doc_chunks(doc.id, visibility=actor) == chunks
        adapter.remove_grant(doc.id, grantee_type="person", grantee_id=person)
        assert adapter.list_doc_chunks(doc.id, visibility=actor) == []

    @pytest.mark.parametrize("kind", ["person", "team"])
    def test_chunk_source_grants_require_current_access(self, adapter, kind):
        doc = make_doc(adapter)
        chunks = [make_chunk()]
        adapter.replace_doc_chunks(doc.id, chunks)
        person, team = uuid4(), uuid4()
        actor = Actor(person, frozenset({team}), source_team_ids=frozenset({team}))
        grant = SourceGrant(
            grantee_type=kind,
            grantee_id=person if kind == "person" else team,
            source_permission_id="permission-1",
            source_principal="reader@example.com",
            source_role="reader",
        )
        now = datetime.now(timezone.utc)
        for grants, expires_at, visible in [
            ([grant], now + timedelta(hours=1), True),
            ([grant], now - timedelta(hours=1), False),
            ([grant], now + timedelta(hours=1), True),
            ([], now + timedelta(hours=1), False),
        ]:
            adapter.replace_source_grants(
                doc.id,
                origin="google_drive",
                grants=grants,
                synced_at=now,
                expires_at=expires_at,
                actor="sync",
            )
            assert adapter.list_doc_chunks(doc.id, visibility=actor) == (chunks if visible else [])
            if kind == "team":
                ordinary_member = Actor(person, frozenset({team}))
                assert adapter.list_doc_chunks(doc.id, visibility=ordinary_member) == []

    def test_chunk_storage_is_detached_from_input_and_output_vectors(self, adapter):
        doc = make_doc(adapter)
        vector = [0.1] * 1536
        chunk = make_chunk(embedding=vector)
        chunks = [chunk]
        adapter.replace_doc_chunks(doc.id, chunks)
        vector[0] = 99.0
        chunks.clear()
        read = adapter.list_doc_chunks(doc.id)
        assert read[0].embedding[0] == pytest.approx(0.1)
        with pytest.raises(TypeError):
            read[0].embedding[0] = 88.0
        read.clear()
        assert adapter.list_doc_chunks(doc.id) == [chunk]

    @pytest.mark.parametrize("prefix", ["word", "\U00010400"])
    def test_chunk_text_limit_round_trips_with_keyword_metadata(self, adapter, prefix):
        # Distinct lexemes and multibyte text exercise generated tsvector storage.
        text = " ".join(f"{prefix}{i}" for i in range(MAX_CHUNK_CHARS))[:MAX_CHUNK_CHARS]
        chunk = make_chunk(text=text)
        doc = make_doc(adapter)
        assert adapter.replace_doc_chunks(doc.id, [chunk]) is True
        assert adapter.list_doc_chunks(doc.id) == [chunk]

    @pytest.mark.parametrize(
        "changes",
        [
            {"dimensions": 3072},
            {"embedding": [1.0]},
            {"embedding": [float("nan")] * 1536},
            {"embedding": [float("inf")] * 1536},
            {"embedding": [1e39] * 1536},
            {"ordinal": -1},
            {"ordinal": True},
            {"start_offset": -1},
            {"end_offset": 2**31},
            {"end_offset": 0},
            {"end_offset": 1},
            {"chunk_text": " "},
            {"chunk_text": "\x00"},
            {"chunk_text": "\ud800"},
            {"chunk_text": "a" * (MAX_CHUNK_CHARS + 1), "end_offset": MAX_CHUNK_CHARS + 1},
            {"embedding_model": " "},
            {"source_content_hash": "not-a-sha256"},
            {"source_title_hash": "not-a-sha256"},
        ],
    )
    def test_invalid_chunk_replacement_preserves_old_set(self, adapter, changes):
        doc = make_doc(adapter)
        original = [make_chunk()]
        adapter.replace_doc_chunks(doc.id, original)
        # model_copy deliberately bypasses Pydantic construction validation.
        invalid = make_chunk(1).model_copy(update=changes)
        with pytest.raises(ValueError):
            adapter.replace_doc_chunks(doc.id, [make_chunk(0), invalid])
        assert adapter.list_doc_chunks(doc.id) == original

    @pytest.mark.parametrize(
        "changes",
        [
            {"ordinal": 0},
            {"ordinal": 2},
            {"embedding_model": "text-embedding-3-large"},
            {"source_content_hash": "a" * 64},
            {"source_title_hash": "a" * 64},
            {"source_title_hash": None},
        ],
    )
    def test_invalid_chunk_set_preserves_old_set(self, adapter, changes):
        doc = make_doc(adapter)
        original = [make_chunk()]
        adapter.replace_doc_chunks(doc.id, original)
        with pytest.raises(ValueError):
            adapter.replace_doc_chunks(
                doc.id, [make_chunk(), make_chunk(**{"ordinal": 1, **changes})]
            )
        assert adapter.list_doc_chunks(doc.id) == original

    def test_invalid_chunks_are_validated_even_for_missing_doc(self, adapter):
        with pytest.raises(ValueError):
            adapter.replace_doc_chunks(uuid4(), [make_chunk(2)])

    # --- Indexing support (#175) ---

    def _with_content(self, adapter, text="UTMIST CPSIF"):
        doc = make_doc(adapter)
        adapter.upsert_doc_content(
            doc.id, content_text=text, content_hash=content_hash(text), fetched_at=None
        )
        return doc

    def test_index_state_reports_the_current_set(self, adapter):
        doc = self._with_content(adapter)
        assert adapter.get_doc_index_states(doc.id) == {}
        assert adapter.get_doc_index_states(uuid4()) == {}
        adapter.replace_doc_chunks(doc.id, [make_chunk(0), make_chunk(1)])
        state = adapter.get_doc_index_states(doc.id)["text-embedding-3-small"]
        assert state.source_content_hash == content_hash("UTMIST CPSIF")
        assert state.source_title_hash == content_hash(doc.title)
        assert state.embedding_model == "text-embedding-3-small"
        assert state.chunk_count == 2
        adapter.replace_doc_chunks(doc.id, [])
        assert adapter.get_doc_index_states(doc.id) == {}

    def test_conditional_replace_writes_only_for_current_active_content(self, adapter):
        doc = self._with_content(adapter)
        current = content_hash("UTMIST CPSIF")
        chunks = [make_chunk(0), make_chunk(1)]
        assert (
            adapter.replace_doc_chunks_if_current(
                doc.id, current, chunks, expected_title_hash=content_hash("Chunk storage test")
            )
            is True
        )
        assert adapter.list_doc_chunks(doc.id) == chunks

        # Content changed after the indexer read it: the stale set is refused
        # and the existing set is left alone.
        adapter.upsert_doc_content(
            doc.id, content_text="newer", content_hash=content_hash("newer"), fetched_at=None
        )
        assert (
            adapter.replace_doc_chunks_if_current(
                doc.id,
                current,
                [make_chunk()],
                expected_title_hash=content_hash("Chunk storage test"),
            )
            is False
        )
        assert adapter.list_doc_chunks(doc.id) == chunks

    def test_conditional_replace_refuses_missing_inactive_and_contentless_docs(self, adapter):
        current = content_hash("UTMIST CPSIF")
        assert (
            adapter.replace_doc_chunks_if_current(
                uuid4(),
                current,
                [make_chunk()],
                expected_title_hash=content_hash("Chunk storage test"),
            )
            is False
        )
        no_content = make_doc(adapter)
        assert (
            adapter.replace_doc_chunks_if_current(
                no_content.id,
                current,
                [make_chunk()],
                expected_title_hash=content_hash("Chunk storage test"),
            )
            is False
        )
        retired = self._with_content(adapter)
        adapter.update_doc(retired.id, {"active": False}, actor="test")
        assert (
            adapter.replace_doc_chunks_if_current(
                retired.id,
                current,
                [make_chunk()],
                expected_title_hash=content_hash("Chunk storage test"),
            )
            is False
        )
        assert adapter.list_doc_chunks(retired.id) == []

    def test_conditional_replace_rejects_chunks_from_other_content(self, adapter):
        doc = self._with_content(adapter)
        original = [make_chunk()]
        adapter.replace_doc_chunks(doc.id, original)
        with pytest.raises(ValueError):
            adapter.replace_doc_chunks_if_current(
                doc.id,
                content_hash("UTMIST CPSIF"),
                [make_chunk(source_text="other")],
                expected_title_hash=content_hash(doc.title),
            )
        assert adapter.list_doc_chunks(doc.id) == original

    @pytest.mark.parametrize("state", ["active", "retired", "no_content", "missing"])
    def test_conditional_clear_preserves_indexable_docs(self, adapter, state):
        doc = self._with_content(adapter) if state != "no_content" else make_doc(adapter)
        chunks = [make_chunk()]
        adapter.replace_doc_chunks(doc.id, chunks)
        if state == "retired":
            adapter.update_doc(doc.id, {"active": False}, actor="test")
        doc_id = uuid4() if state == "missing" else doc.id

        assert adapter.clear_doc_chunks_if_unindexable(doc_id) is (state != "active")
        assert adapter.list_doc_chunks(doc.id) == (chunks if state in {"active", "missing"} else [])
        assert adapter.clear_doc_chunks_if_unindexable(doc_id) is (state != "active")

    def test_docs_for_indexing(self, adapter):
        current = self._with_content(adapter)
        adapter.replace_doc_chunks(current.id, [make_chunk()])
        unindexed = self._with_content(adapter)
        stale = self._with_content(adapter)
        adapter.replace_doc_chunks(stale.id, [make_chunk(source_text="older text")])
        no_content = make_doc(adapter)
        retired_indexed = self._with_content(adapter)
        adapter.replace_doc_chunks(retired_indexed.id, [make_chunk()])
        adapter.update_doc(retired_indexed.id, {"active": False}, actor="test")
        retired_clean = self._with_content(adapter)
        adapter.update_doc(retired_clean.id, {"active": False}, actor="test")
        orphaned = make_doc(adapter)
        adapter.replace_doc_chunks(orphaned.id, [make_chunk()])

        stale_ids = sorted([unindexed.id, stale.id, retired_indexed.id, orphaned.id])
        assert adapter.list_docs_for_indexing(models=[FAKE_MODEL]) == stale_ids
        assert adapter.list_docs_for_indexing(models=[FAKE_MODEL], stale_only=False) == sorted(
            [current.id, *stale_ids]
        )
        assert no_content.id not in adapter.list_docs_for_indexing(
            models=[FAKE_MODEL], stale_only=False
        )
        assert retired_clean.id not in adapter.list_docs_for_indexing(
            models=[FAKE_MODEL], stale_only=False
        )

    def test_docs_for_indexing_pages_by_id(self, adapter):
        ids = sorted(self._with_content(adapter).id for _ in range(5))
        assert adapter.list_docs_for_indexing(models=[FAKE_MODEL], limit=2) == ids[:2]
        assert (
            adapter.list_docs_for_indexing(models=[FAKE_MODEL], after=ids[1], limit=2) == ids[2:4]
        )
        assert adapter.list_docs_for_indexing(models=[FAKE_MODEL], after=ids[4]) == []

    # --- Chunk sets per embedding model (#209) ---

    def _write_set(self, adapter, doc, model, **values):
        chunks = [make_chunk(0, embedding_model=model, **values)]
        return adapter.replace_doc_chunks_if_current(
            doc.id,
            chunks[0].source_content_hash,
            chunks,
            expected_title_hash=content_hash("Chunk storage test"),
        )

    def test_model_sets_coexist_and_conditional_replace_keeps_other_models(self, adapter):
        doc = self._with_content(adapter)
        assert self._write_set(adapter, doc, FAKE_MODEL) is True
        assert self._write_set(adapter, doc, LARGE_MODEL) is True
        # Ordered by model id, then ordinal ("-large" sorts before "-small").
        assert [(c.embedding_model, c.ordinal) for c in adapter.list_doc_chunks(doc.id)] == [
            (LARGE_MODEL, 0),
            (FAKE_MODEL, 0),
        ]
        assert list(adapter.get_doc_index_states(doc.id)) == [LARGE_MODEL, FAKE_MODEL]

        # Rewriting one model's set leaves the other untouched.
        large = adapter.list_doc_chunks(doc.id)[0]
        assert self._write_set(adapter, doc, FAKE_MODEL, embedding=[0.5] + [0.0] * 1535) is True
        assert adapter.list_doc_chunks(doc.id)[0] == large
        assert adapter.list_doc_chunks(doc.id)[1].embedding[0] == pytest.approx(0.5)

        # Whitespace-only text clears every set; an unconditional replace
        # leaves exactly the one set it was given.
        assert self._write_set(adapter, doc, LARGE_MODEL) is True
        adapter.replace_doc_chunks(doc.id, [make_chunk()])
        assert [c.embedding_model for c in adapter.list_doc_chunks(doc.id)] == [FAKE_MODEL]
        self._write_set(adapter, doc, LARGE_MODEL)
        assert (
            adapter.replace_doc_chunks_if_current(
                doc.id,
                content_hash("UTMIST CPSIF"),
                [],
                expected_title_hash=content_hash("Chunk storage test"),
            )
            is True
        )
        assert adapter.list_doc_chunks(doc.id) == []

    def test_delete_doc_chunks_except_keeps_listed_models(self, adapter):
        doc = self._with_content(adapter)
        self._write_set(adapter, doc, FAKE_MODEL)
        self._write_set(adapter, doc, LARGE_MODEL)
        assert adapter.delete_doc_chunks_except(doc.id, [FAKE_MODEL, LARGE_MODEL]) is False
        assert adapter.delete_doc_chunks_except(doc.id, [LARGE_MODEL]) is True
        assert list(adapter.get_doc_index_states(doc.id)) == [LARGE_MODEL]
        assert adapter.delete_doc_chunks_except(doc.id, []) is True
        assert adapter.get_doc_index_states(doc.id) == {}
        assert adapter.delete_doc_chunks_except(uuid4(), []) is False

    def test_docs_for_indexing_is_model_aware(self, adapter):
        doc = self._with_content(adapter)
        self._write_set(adapter, doc, FAKE_MODEL)
        both = [FAKE_MODEL, LARGE_MODEL]
        assert doc.id not in adapter.list_docs_for_indexing(models=[FAKE_MODEL])
        # A configured re-embed target without a set is due ...
        assert doc.id in adapter.list_docs_for_indexing(models=both)
        self._write_set(adapter, doc, LARGE_MODEL)
        # ... and no longer once its set is current, so a rerun resumes.
        assert doc.id not in adapter.list_docs_for_indexing(models=both)
        # After cutover the old model's set is due for deletion.
        assert doc.id in adapter.list_docs_for_indexing(models=[LARGE_MODEL])
        # A target set built from older text is stale like any other set.
        _update = "UTMIST CPSIF v2"
        adapter.upsert_doc_content(
            doc.id, content_text=_update, content_hash=content_hash(_update), fetched_at=None
        )
        self._write_set(adapter, doc, FAKE_MODEL, text=_update)
        assert doc.id in adapter.list_docs_for_indexing(models=both)

    def test_index_coverage_counts_sets_per_model(self, adapter):
        migrated = self._with_content(adapter)
        self._write_set(adapter, migrated, FAKE_MODEL)
        self._write_set(adapter, migrated, LARGE_MODEL)
        pending = self._with_content(adapter)
        self._write_set(adapter, pending, FAKE_MODEL)
        self._with_content(adapter)  # unindexed: missing from no model
        retired = self._with_content(adapter)
        self._write_set(adapter, retired, FAKE_MODEL)
        adapter.update_doc(retired.id, {"active": False}, actor="test")

        coverage = adapter.get_index_coverage([LARGE_MODEL, "unused-model"])
        assert coverage.indexable_docs == 3
        by_model = {c.model: c for c in coverage.models}
        assert [c.model for c in coverage.models] == sorted(by_model)
        assert by_model[FAKE_MODEL].model_dump() == {
            "model": FAKE_MODEL,
            "docs": 3,
            "chunks": 3,
            "chunk_chars": 3 * len("UTMIST CPSIF"),
            "missing_docs": 0,
        }
        assert (by_model[LARGE_MODEL].docs, by_model[LARGE_MODEL].missing_docs) == (1, 1)
        assert by_model["unused-model"].model_dump() == {
            "model": "unused-model",
            "docs": 0,
            "chunks": 0,
            "chunk_chars": 0,
            "missing_docs": 2,
        }

    @pytest.mark.parametrize("title", ["New project 😀", None, ""])
    def test_title_change_is_stale_and_rejects_old_title_writes(self, adapter, title):
        doc = self._with_content(adapter)
        chunks = [make_chunk()]
        adapter.replace_doc_chunks(doc.id, chunks)
        adapter.update_doc(doc.id, {"title": title}, actor="test")
        assert doc.id in adapter.list_docs_for_indexing(models=[FAKE_MODEL])
        for replacement in [chunks, []]:
            assert (
                adapter.replace_doc_chunks_if_current(
                    doc.id,
                    chunks[0].source_content_hash,
                    replacement,
                    expected_title_hash=content_hash(doc.title),
                )
                is False
            )
        assert adapter.list_doc_chunks(doc.id) == chunks

        updated = [make_chunk(source_title_hash=content_hash(title or ""))]
        assert (
            adapter.replace_doc_chunks_if_current(
                doc.id,
                chunks[0].source_content_hash,
                updated,
                expected_title_hash=content_hash(title or ""),
            )
            is True
        )
        assert adapter.list_doc_chunks(doc.id) == updated
        assert doc.id not in adapter.list_docs_for_indexing(models=[FAKE_MODEL])

    @pytest.mark.parametrize("title", ["Project 😀", None, ""])
    def test_unknown_title_provenance_is_stale_until_replaced(self, adapter, title):
        doc = self._with_content(adapter)
        adapter.update_doc(doc.id, {"title": title}, actor="test")
        legacy = make_chunk(source_title_hash=None)
        adapter.replace_doc_chunks(doc.id, [legacy])
        assert adapter.get_doc_index_states(doc.id)[FAKE_MODEL].source_title_hash is None
        assert doc.id in adapter.list_docs_for_indexing(models=[FAKE_MODEL])
        updated = make_chunk(source_title_hash=content_hash(title or ""))
        assert (
            adapter.replace_doc_chunks_if_current(
                doc.id,
                updated.source_content_hash,
                [updated],
                expected_title_hash=updated.source_title_hash,
            )
            is True
        )
        assert doc.id not in adapter.list_docs_for_indexing(models=[FAKE_MODEL])

    @pytest.mark.parametrize("title_hash", [None, "a" * 64])
    def test_conditional_replace_rejects_chunks_from_other_title(self, adapter, title_hash):
        doc = self._with_content(adapter)
        original = make_chunk()
        adapter.replace_doc_chunks(doc.id, [original])
        with pytest.raises(ValueError):
            adapter.replace_doc_chunks_if_current(
                doc.id,
                original.source_content_hash,
                [make_chunk(source_title_hash=title_hash)],
                expected_title_hash=content_hash(doc.title),
            )
        assert adapter.list_doc_chunks(doc.id) == [original]

    def test_delete_doc_content_removes_text_and_chunks_but_keeps_the_doc(self, adapter):
        doc = self._with_content(adapter)
        adapter.replace_doc_chunks(doc.id, [make_chunk()])
        expected = adapter.get_doc_content_meta(doc.id)
        assert adapter.delete_doc_content(doc.id, expected=expected) is True
        assert adapter.get_doc_content(doc.id) is None
        assert adapter.list_doc_chunks(doc.id) == []
        assert adapter.get_doc(doc.id) is not None
        assert adapter.delete_doc_content(doc.id, expected=None) is True
        assert adapter.delete_doc_content(uuid4(), expected=None) is False
        assert doc.id not in adapter.list_docs_for_indexing(models=[FAKE_MODEL], stale_only=False)

    @pytest.mark.parametrize("change", ["text", "fetched_at", "first_fetch"])
    def test_delete_doc_content_preserves_a_newer_fetch(self, adapter, change):
        doc = make_doc(adapter) if change == "first_fetch" else self._with_content(adapter)
        expected = adapter.get_doc_content_meta(doc.id)
        text = "newer" if change == "text" else "UTMIST CPSIF"
        adapter.upsert_doc_content(
            doc.id,
            content_text=text,
            content_hash=content_hash(text),
            fetched_at=None if change == "text" else datetime.now(timezone.utc),
        )
        chunks = [make_chunk(text=text)]
        adapter.replace_doc_chunks(doc.id, chunks)

        assert adapter.delete_doc_content(doc.id, expected=expected) is False
        assert adapter.get_doc_content(doc.id) == text
        assert adapter.list_doc_chunks(doc.id) == chunks
