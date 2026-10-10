"""The same chunk persistence assertions run in both adapter suites."""

from uuid import uuid4

import pytest

from contracts.types import DocChunk
from contracts.visibility import Actor, DENY, SEE_ALL
from src.content import content_hash


def make_chunk(ordinal=0, *, text="UTMIST CPSIF", start=0, source_text=None, **values):
    return DocChunk(
        **{
            "ordinal": ordinal,
            "chunk_text": text,
            "start_offset": start,
            "end_offset": start + len(text),
            "source_content_hash": content_hash(source_text or text),
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

    @pytest.mark.parametrize(
        "access", ["person_owner", "team_owner", "person_grant", "team_grant", "org_grant"]
    )
    def test_chunk_reads_use_doc_visibility(self, adapter, access):
        doc, hidden = make_doc(adapter), make_doc(adapter)
        chunks = [make_chunk()]
        adapter.replace_doc_chunks(doc.id, chunks)
        adapter.replace_doc_chunks(hidden.id, chunks)
        person, team = uuid4(), uuid4()
        actor = Actor(person_id=person, team_ids=frozenset({team}))
        assert adapter.list_doc_chunks(doc.id, visibility=actor) == []
        if access.endswith("owner"):
            column, owner = (
                ("owning_person_id", person)
                if access == "person_owner"
                else ("owning_team_id", team)
            )
            adapter.update_doc(doc.id, {column: owner}, actor="test")
        else:
            kind = access.removesuffix("_grant")
            grantee = {"person": person, "team": team, "org": None}[kind]
            adapter.add_grant(doc.id, grantee_type=kind, grantee_id=grantee, actor="test")
        assert adapter.list_doc_chunks(doc.id, visibility=actor) == chunks
        assert adapter.list_doc_chunks(hidden.id, visibility=actor) == []
        assert adapter.list_doc_chunks(doc.id, visibility=DENY) == []
        assert adapter.list_doc_chunks(hidden.id, visibility=SEE_ALL) == chunks

    def test_chunk_storage_is_detached_from_input_and_output_vectors(self, adapter):
        doc = make_doc(adapter)
        chunk = make_chunk()
        adapter.replace_doc_chunks(doc.id, [chunk])
        chunk.embedding[0] = 99.0
        read = adapter.list_doc_chunks(doc.id)
        assert read[0].embedding[0] == pytest.approx(0.1)
        read[0].embedding[0] = 88.0
        assert adapter.list_doc_chunks(doc.id)[0].embedding[0] == pytest.approx(0.1)

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
            {"embedding_model": " "},
            {"source_content_hash": "not-a-sha256"},
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
