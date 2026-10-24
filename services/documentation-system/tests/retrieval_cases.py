"""Portable fixture loader and search assertions shared by both adapters."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import UUID

import pytest

from contracts.retrieval import QueryEmbedding
from contracts.types import SourceGrant
from contracts.visibility import Actor, Channel, DENY, SEE_ALL
from src.content import content_hash
from tests.chunk_storage_cases import make_chunk, make_doc

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "docs/fixtures/retrieval.json").read_text()
)
P1, P2, P3 = map(UUID, FIXTURE["people"])
T1, T2 = map(UUID, FIXTURE["synced_team_ids"])
ALICE = Actor(P1, frozenset({T1}), frozenset({T1}))
BOB = Actor(P2, frozenset({T2}), frozenset({T2}))
CHANNEL = Channel(frozenset({T1, T2}))


def vector(prefix):
    return prefix + [0.0] * (1536 - len(prefix))


def embedding(prefix=None):
    return QueryEmbedding(model=FIXTURE["model"], vector=vector(prefix or [1, 0]))


def seed(adapter):
    docs = {}
    for item in FIXTURE["documents"]:
        doc = adapter.create_doc(
            url=item["url"],
            url_normalized=item["url"],
            source_id=item["source_id"],
            title=item["title"],
            description=None,
            owning_team_id=None,
            owning_team_label=None,
            owning_person_id=None,
            owning_person_label=None,
            content_snapshot=None,
            fetched_at=None,
            tags=[],
            actor="fixture",
        )
        docs[item["key"]] = doc
        if not item["active"]:
            adapter.update_doc(doc.id, {"active": False}, actor="fixture")
        source = []
        for kind, grantee, origin in item["grants"]:
            grantee = UUID(grantee) if grantee else None
            if origin == "manual":
                adapter.add_grant(doc.id, grantee_type=kind, grantee_id=grantee, actor="fixture")
            else:
                source.append(
                    SourceGrant(
                        grantee_type=kind,
                        grantee_id=grantee,
                        source_permission_id=f"{kind}-{grantee}",
                        source_principal="fixture@example.test",
                        source_role="reader",
                    )
                )
        adapter.replace_source_grants(
            doc.id,
            origin="google_drive",
            grants=source,
            synced_at=datetime.now(timezone.utc),
            expires_at=datetime(2100, 1, 1, tzinfo=timezone.utc),
            actor="fixture",
        )
        adapter.replace_doc_chunks(
            doc.id,
            [
                make_chunk(
                    text=item["text"],
                    embedding=vector(item["embedding_prefix"]),
                )
            ],
        )
        adapter.upsert_doc_content(
            doc.id,
            content_text=item["text"],
            content_hash=content_hash(item["text"]),
            fetched_at=None,
        )
    return docs


def expected_response(case, docs):
    result = deepcopy(case["response"]["json"])
    by_url = {doc.url: doc.id for doc in docs.values()}
    for hit in result.get("results", []):
        hit["doc_id"] = str(by_url[hit["url"]])
    return result


def search(adapter, *, context=ALICE, query="CPSIF", k=10, prefix=None, min_cosine=0.3):
    return adapter.search_chunks(
        query=query,
        embedding=embedding(prefix),
        visibility=context,
        k=k,
        min_cosine=min_cosine,
    )


class RetrievalCases:
    @pytest.mark.parametrize(
        "name,context",
        [
            ("alice", ALICE),
            ("bob", BOB),
            ("channel-union", CHANNEL),
            ("unconfigured-channel", Channel(frozenset())),
            ("empty-relevance", ALICE),
            ("filter-before-rank", ALICE),
        ],
    )
    def test_shared_contract(self, adapter, name, context):
        docs = seed(adapter)
        case = next(case for case in FIXTURE["cases"] if case["name"] == name)
        body = case["request"]["json"]
        hits = search(
            adapter, context=context, **body, prefix=[0, -1] if name == "empty-relevance" else None
        )
        expected = expected_response(case, docs)
        actual = {"results": [hit.model_dump(mode="json") for hit in hits]}
        assert actual == expected

    @pytest.mark.parametrize("path", ["hybrid", "keyword", "vector"])
    def test_filter_before_branch_limit(self, adapter, path):
        docs = seed(adapter)
        # More hidden, better-ranked chunks than either branch's candidate cap.
        for _ in range(55):
            hidden = make_doc(adapter)
            adapter.replace_doc_chunks(
                hidden.id,
                [
                    make_chunk(
                        text="CPSIF",
                        embedding=vector([1, 0]),
                    )
                ],
            )
        hits = search(
            adapter,
            k=1,
            query="semantic" if path == "vector" else "CPSIF",
            prefix=[0, -1] if path == "keyword" else None,
        )
        assert [hit.doc_id for hit in hits] == [docs["personal-alice"].id]

    def test_grants_expiry_and_revocation_are_live(self, adapter):
        docs = seed(adapter)
        private = docs["personal-alice"].id
        assert private in {hit.doc_id for hit in search(adapter)}
        adapter.replace_source_grants(
            private,
            origin="google_drive",
            grants=[],
            synced_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            actor="sync",
        )
        assert private not in {hit.doc_id for hit in search(adapter)}
        team = docs["team-a"].id
        adapter.replace_source_grants(
            team,
            origin="google_drive",
            grants=[
                SourceGrant(
                    grantee_type="team",
                    grantee_id=T1,
                    source_permission_id="group",
                    source_principal="team@example.test",
                    source_role="reader",
                )
            ],
            synced_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            actor="sync",
        )
        assert team not in {hit.doc_id for hit in search(adapter, context=CHANNEL)}

    def test_new_scope_excludes_former_team_and_person_shares(self, adapter):
        docs = seed(adapter)
        assert docs["team-a"].id in {hit.doc_id for hit in search(adapter, context=CHANNEL)}
        hits = search(adapter, context=Channel(frozenset({T2})))
        assert [hit.doc_id for hit in hits] == [docs["team-b"].id]
        hits = search(adapter, context=Actor(P1, frozenset()))
        assert {hit.doc_id for hit in hits} == {docs["personal-alice"].id, docs["org"].id}

    def test_no_authorized_documents_is_empty(self, adapter):
        doc = make_doc(adapter)
        adapter.replace_doc_chunks(doc.id, [make_chunk(embedding=vector([1, 0]))])
        adapter.update_doc(doc.id, {"owning_person_id": P3}, actor="test")
        assert search(adapter, context=Actor(P3, frozenset())) == []

    def test_model_mismatch_is_excluded_from_both_paths(self, adapter):
        docs = seed(adapter)
        for doc in docs.values():
            adapter.replace_doc_chunks(
                doc.id,
                [
                    make_chunk(
                        text="CPSIF",
                        embedding=vector([1, 0]),
                        embedding_model="text-embedding-3-large",
                    )
                ],
            )
        assert search(adapter) == []

    def test_coexisting_model_sets_are_searched_one_model_at_a_time(self, adapter):
        # A re-embed (#209) builds a second set beside the served one. Each
        # search reads only its own model's set: no duplicate or mixed hits.
        large = "text-embedding-3-large"
        for doc in seed(adapter).values():
            text = adapter.get_doc_content(doc.id)
            title_hash = content_hash(doc.title or "")
            adapter.update_doc(doc.id, {"active": True}, actor="test")
            for model, prefix in ((FIXTURE["model"], [1, 0]), (large, [0, 1])):
                chunk = make_chunk(
                    text=text,
                    embedding=vector(prefix),
                    embedding_model=model,
                    source_title_hash=title_hash,
                )
                assert adapter.replace_doc_chunks_if_current(
                    doc.id, chunk.source_content_hash, [chunk], expected_title_hash=title_hash
                )

        def hits(model, prefix):
            found = adapter.search_chunks(
                query="CPSIF",
                embedding=QueryEmbedding(model=model, vector=vector(prefix)),
                visibility=ALICE,
                k=10,
                min_cosine=0.3,
            )
            assert len({(h.doc_id, h.chunk.ordinal) for h in found}) == len(found)
            return {h.doc_id for h in found}

        served = hits(FIXTURE["model"], [1, 0])
        assert served and hits(large, [0, 1]) == served
        for doc_id in served:
            adapter.delete_doc_chunks_except(doc_id, [large])
        assert hits(FIXTURE["model"], [1, 0]) == set()
        assert hits(large, [0, 1]) == served

    def test_relevance_floor_keyword_override_and_zero_vector(self, adapter):
        doc = make_doc(adapter)
        adapter.add_grant(doc.id, grantee_type="person", grantee_id=P1, actor="test")
        for prefix in ([0.2, 0.98], [0, 0]):
            adapter.replace_doc_chunks(doc.id, [make_chunk(text="CPSIF", embedding=vector(prefix))])
            assert search(adapter, query="irrelevant") == []
            assert [hit.doc_id for hit in search(adapter)] == [doc.id]
        adapter.replace_doc_chunks(doc.id, [])
        assert search(adapter) == []

    def test_keyword_punctuation_and_case_have_adapter_parity(self, adapter):
        doc = make_doc(adapter)
        adapter.add_grant(doc.id, grantee_type="person", grantee_id=P1, actor="test")
        adapter.replace_doc_chunks(
            doc.id,
            [
                make_chunk(
                    text="Email Alice+budget@UTMIST.ca on 2026-10-23, about CPSIF; AI².",
                    embedding=vector([0, 1]),
                )
            ],
        )
        for query in ("CPSIF", "alice budget", "utmist.ca", "2026-10-23", "AI", "AI²"):
            assert [hit.doc_id for hit in search(adapter, query=query)] == [doc.id]
        for query in ("unknown", "alice unknown", "你好", "K"):
            assert search(adapter, query=query) == []

    def test_citation_offsets_order_and_retirement(self, adapter):
        doc = make_doc(adapter)
        text = "😀 CPSIF\nCPSIF notes"
        chunks = [
            make_chunk(0, text=text[:7], source_text=text, embedding=vector([1, 0])),
            make_chunk(1, text=text[8:], start=8, source_text=text, embedding=vector([1, 0])),
        ]
        adapter.replace_doc_chunks(doc.id, chunks)
        adapter.add_grant(doc.id, grantee_type="person", grantee_id=P1, actor="test")
        for hit in search(adapter, query="semantic"):
            assert hit.text == text[hit.chunk.start_offset : hit.chunk.end_offset]
            assert hit.chunk.source_content_hash == chunks[0].source_content_hash
            assert hit.doc_id == doc.id
        assert [hit.chunk.ordinal for hit in search(adapter, query="semantic")] == [0, 1]
        adapter.update_doc(doc.id, {"active": False}, actor="test")
        assert search(adapter) == []

    @pytest.mark.parametrize("context", [SEE_ALL, DENY, None])
    def test_no_unfiltered_storage_variant(self, adapter, context):
        with pytest.raises(ValueError, match="requires a person or channel"):
            search(adapter, context=context)

    def test_manual_grant_revocation(self, adapter):
        docs = seed(adapter)
        doc_id = docs["manual-team"].id
        adapter.remove_grant(doc_id, grantee_type="team", grantee_id=T1)
        assert doc_id not in {hit.doc_id for hit in search(adapter)}
