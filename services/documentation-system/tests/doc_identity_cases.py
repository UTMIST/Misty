"""Identical doc-id contract exercised by both adapters.

`create_doc` accepts a caller-supplied `doc_id` so the evaluation harness can seed a
document under the same identity every run. The two stores fail differently when such
an id is already taken -- the in-memory dict would overwrite, Postgres would raise a
primary-key violation -- so the rejection is pinned here rather than in either suite.
"""

from uuid import uuid4

import pytest

from contracts.storage import DuplicateDocId

FIXED = uuid4()


class DocIdentityCases:
    @staticmethod
    def make(adapter, url, *, doc_id=None, title="Doc"):
        return adapter.create_doc(
            url=url,
            url_normalized=url,
            source_id="web",
            title=title,
            description=None,
            owning_team_id=None,
            owning_team_label=None,
            owning_person_id=None,
            owning_person_label=None,
            content_snapshot=None,
            fetched_at=None,
            tags=[],
            actor="test",
            doc_id=doc_id,
        )

    def test_a_supplied_id_is_used_verbatim(self, adapter):
        doc = self.make(adapter, "https://example.test/supplied", doc_id=FIXED)
        assert doc.id == FIXED
        assert adapter.get_doc(FIXED) is not None

    def test_an_omitted_id_is_generated(self, adapter):
        first = self.make(adapter, "https://example.test/gen-1")
        second = self.make(adapter, "https://example.test/gen-2")
        assert first.id != second.id

    def test_a_duplicate_id_is_rejected_rather_than_overwriting(self, adapter):
        doc_id = uuid4()
        self.make(adapter, "https://example.test/first", doc_id=doc_id, title="First")
        with pytest.raises(DuplicateDocId):
            self.make(adapter, "https://example.test/second", doc_id=doc_id, title="Second")
        # The original survives intact: a silent replacement would leave the existing
        # content, chunks and grants attached to a document that is no longer theirs.
        kept = adapter.get_doc(doc_id)
        assert kept.title == "First"
        assert kept.url == "https://example.test/first"

    def test_a_taken_id_is_reported_even_when_the_url_collides_too(self, adapter):
        # Both adapters must name the same failure. Postgres would otherwise report the
        # url conflict its unique index catches first, while the in-memory store, which
        # has no url constraint, reports the id -- one call, two contracts.
        doc_id = uuid4()
        self.make(adapter, "https://example.test/collide", doc_id=doc_id)
        with pytest.raises(DuplicateDocId):
            self.make(adapter, "https://example.test/collide", doc_id=doc_id)

    def test_a_retired_documents_id_is_still_taken(self, adapter):
        doc_id = uuid4()
        self.make(adapter, "https://example.test/retired", doc_id=doc_id)
        adapter.update_doc(doc_id, {"active": False}, actor="test")
        with pytest.raises(DuplicateDocId):
            self.make(adapter, "https://example.test/reused", doc_id=doc_id)
