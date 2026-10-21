"""Identical alias lookup contract exercised by both adapters."""

from uuid import UUID

import pytest

from contracts.fetcher import FetchError, FetchResult, SourcePermission
from contracts.types import DocIngest
from src.ingest import ingest_doc
from src.url_norm import normalize_url
from tests.test_ingest import FakeDirectory, FakeFetchers


ALIASES = [
    "https://docs.google.com/document/d/identity/edit?usp=sharing",
    "https://docs.google.com/spreadsheets/d/identity/edit#gid=1",
    "https://docs.google.com/presentation/d/identity/view",
    "https://docs.google.com/forms/d/identity/edit",
    "https://drive.google.com/file/d/identity/view",
    "https://drive.google.com/open?usp=sharing&id=identity&other=1",
]


class GoogleIdentityCases:
    @pytest.mark.parametrize("kind", ["document", "spreadsheets", "presentation", "forms"])
    def test_published_links_never_merge_documents_or_grants(self, adapter, kind):
        first_url = f"https://docs.google.com/{kind}/d/e/2PACX-first/pub"
        second_url = f"https://docs.google.com/{kind}/d/e/2PACX-second/pub"
        fetchers = FakeFetchers(error=FetchError("unsupported published URL"))
        first = ingest_doc(
            DocIngest(url=first_url, tags=["first"]),
            storage=adapter,
            fetchers=fetchers,
            directory=FakeDirectory(),
            actor="first",
        )
        second = ingest_doc(
            DocIngest(url=second_url, tags=["second"], grants=[{"grantee_type": "org"}]),
            storage=adapter,
            fetchers=fetchers,
            directory=FakeDirectory(),
            actor="second",
        )
        assert second.created is True
        assert first.doc.id != second.doc.id
        assert normalize_url(first_url) == first_url
        assert normalize_url(second_url) == second_url
        assert adapter.get_doc(first.doc.id).tags == ["first"]
        assert adapter.list_grants(first.doc.id) == []
        assert [g.grantee_type for g in adapter.list_grants(second.doc.id)] == ["org"]

    @pytest.mark.parametrize("kind", ["document", "spreadsheets", "presentation"])
    def test_legacy_published_link_is_not_file_e(self, adapter, kind):
        self.make(adapter, f"https://docs.google.com/{kind}/d/e/2PACX-published/pub")
        assert adapter.get_doc_by_google_file_id("e") is None

    @pytest.mark.parametrize("retired", [False, True])
    def test_discovery_skips_registration_during_fetch(self, adapter, retired):
        winner = []

        class InterleavedFetcher:
            def fetch_for(self, source_id, url):
                # A manual registration commits while discovery fetches. Its
                # legacy alias deliberately differs from discovery's canonical URL.
                doc = GoogleIdentityCases.make(adapter, ALIASES[0])
                if retired:
                    doc = adapter.update_doc(doc.id, {"active": False}, actor="operator")
                winner.append(doc)
                return FetchResult(
                    title="Fetched",
                    content="Fetched text",
                    permissions=[
                        SourcePermission(
                            permission_id="person-share",
                            principal_type="user",
                            role="reader",
                            principal="reader@example.com",
                        )
                    ],
                )

        result = ingest_doc(
            DocIngest(url="https://drive.google.com/file/d/identity", source_id="gdocs"),
            storage=adapter,
            fetchers=InterleavedFetcher(),
            directory=FakeDirectory(people={"reader@example.com": UUID(int=1)}),
            actor="discovery",
            refresh_existing=False,
        )
        assert result.created is False
        assert result.doc.id == winner[0].id
        assert result.doc.active is not retired
        assert len(adapter.list_docs(active_only=False)) == 1
        assert adapter.get_doc(result.doc.id).created_by == "test"
        assert adapter.get_doc(result.doc.id).title == "Legacy"
        assert adapter.get_doc_content_meta(result.doc.id) is None
        assert adapter.list_grants(result.doc.id) == []

    def test_manual_reingest_after_retirement_still_creates(self, adapter):
        old = self.make(adapter, ALIASES[0])
        adapter.update_doc(old.id, {"active": False}, actor="operator")
        result = ingest_doc(
            DocIngest(url="https://drive.google.com/file/d/identity", source_id="gdocs"),
            storage=adapter,
            fetchers=FakeFetchers(result=FetchResult(permissions=[])),
            directory=FakeDirectory(),
            actor="manual",
        )
        assert result.created is True
        assert result.doc.id != old.id
        assert result.doc.active is True
        assert adapter.get_doc(old.id).active is False

    @pytest.mark.parametrize("url", ALIASES)
    def test_legacy_alias_lookup(self, adapter, url):
        doc = self.make(adapter, url)
        assert adapter.get_doc_by_google_file_id("identity").id == doc.id
        assert normalize_url(url) == "https://drive.google.com/file/d/identity"
        assert adapter.get_doc_by_google_file_id("ident") is None

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.test/docs.google.com/document/d/identity/edit",
            "https://drive.google.com.evil.test/file/d/identity",
            "https://docs.google.com/forms/d/e/identity/viewform",
            "https://drive.google.com/file/d/identity-other/view",
        ],
    )
    def test_non_aliases_do_not_match(self, adapter, url):
        self.make(adapter, url)
        assert adapter.get_doc_by_google_file_id("identity") is None

    def test_active_then_earliest_and_retired_fallback(self, adapter):
        old = self.make(adapter, ALIASES[0])
        adapter.update_doc(old.id, {"active": False}, actor="operator")
        new = self.make(adapter, ALIASES[1])
        assert adapter.get_doc_by_google_file_id("identity").id == new.id
        adapter.update_doc(new.id, {"active": False}, actor="operator")
        assert adapter.get_doc_by_google_file_id("identity").id == old.id

    @staticmethod
    def make(adapter, url):
        return adapter.create_doc(
            url=url,
            url_normalized=url,
            source_id="web",
            title="Legacy",
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
