"""Identical alias lookup contract exercised by both adapters."""

import pytest

from src.url_norm import normalize_url


ALIASES = [
    "https://docs.google.com/document/d/identity/edit?usp=sharing",
    "https://docs.google.com/spreadsheets/d/identity/edit#gid=1",
    "https://docs.google.com/presentation/d/identity/view",
    "https://docs.google.com/forms/d/identity/edit",
    "https://drive.google.com/file/d/identity/view",
    "https://drive.google.com/open?usp=sharing&id=identity&other=1",
]


class GoogleIdentityCases:
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
