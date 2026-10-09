from datetime import datetime, timezone
from uuid import uuid4

import pytest

from contracts.directory import DirectoryUnavailable
from contracts.fetcher import FetchError, FetchResult
from contracts.types import DocIngest, Source
from src.ingest import BadReference, ingest_doc
from src.storage.in_memory import InMemoryStorageAdapter


def _sources():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def mk(sid, patterns, fetch_on, auth=False):
        return Source(
            id=sid,
            label=sid,
            url_patterns=patterns,
            requires_auth=auth,
            has_api=False,
            content_fetch_enabled=fetch_on,
            created_at=now,
            updated_at=now,
            created_by="system",
            updated_by="system",
        )

    return [
        mk("web", [], True),
        mk("github", ["github.com"], True),
        mk("gdrive", ["drive.google.com"], False, auth=True),
    ]


class FakeFetchers:
    def __init__(self, result=None, error=None):
        self._result, self._error = result, error

    def fetch_for(self, source_id, url):
        if self._error:
            raise self._error
        return self._result


class CountingFetchers:
    """Records how many fetches were attempted, so a test can assert that the
    dedup path performs none."""

    def __init__(self, result=None):
        self._result = result
        self.calls = 0

    def fetch_for(self, source_id, url):
        self.calls += 1
        return self._result


class FakeDirectory:
    def __init__(self, team=None, person=None, unavailable=False):
        self._team, self._person, self._down = team, person, unavailable

    def get_team_label(self, team_id):
        if self._down:
            raise DirectoryUnavailable("down")
        return self._team

    def get_person_label(self, person_id):
        if self._down:
            raise DirectoryUnavailable("down")
        return self._person


@pytest.fixture
def store():
    return InMemoryStorageAdapter(seed_sources=_sources())


def test_ingest_happy_path_derives_source_and_fetches_title(store):
    fetchers = FakeFetchers(result=FetchResult(title="Repo", content_snapshot="body"))
    res = ingest_doc(
        DocIngest(url="https://github.com/a/b"),
        storage=store,
        fetchers=fetchers,
        directory=FakeDirectory(),
        actor="bot",
    )
    assert res.created is True
    assert res.doc.source_id == "github"
    assert res.doc.title == "Repo"
    assert res.warnings == []


def test_ingest_dedup_returns_existing_and_merges_tags(store):
    f = FakeFetchers(result=FetchResult(title="X"))
    first = ingest_doc(
        DocIngest(url="https://x.com/a", tags=["one"]),
        storage=store,
        fetchers=f,
        directory=FakeDirectory(),
        actor="bot",
    )
    second = ingest_doc(
        DocIngest(url="https://x.com/a/", tags=["two"]),
        storage=store,
        fetchers=f,
        directory=FakeDirectory(),
        actor="bot",
    )
    assert second.created is False
    assert second.doc.id == first.doc.id
    assert set(second.doc.tags) == {"one", "two"}


def test_duplicate_ingest_does_not_fetch(store):
    """#159's settled decision: dedup is a cheap, network-free merge. A second
    ingest of the same URL must not re-run the fetch, even though this doc has
    no stored content and a fetch is exactly what would repair it. Repair is
    POST /docs/{id}/refetch's job, driven in bulk by doc-backfill."""
    f = CountingFetchers(result=FetchResult(title="X"))  # title only, no content
    args = dict(storage=store, fetchers=f, directory=FakeDirectory(), actor="bot")
    ingest_doc(DocIngest(url="https://x.com/a"), **args)
    assert f.calls == 1
    ingest_doc(DocIngest(url="https://x.com/a"), **args)
    assert f.calls == 1  # unchanged: the dedup path made no fetch


def test_duplicate_ingest_warns_when_content_is_missing(store):
    """The gap is reported rather than hidden, so a caller holding this exact
    doc learns it is contentless."""
    f = CountingFetchers(result=FetchResult(title="X"))
    args = dict(storage=store, fetchers=f, directory=FakeDirectory(), actor="bot")
    ingest_doc(DocIngest(url="https://x.com/a"), **args)
    second = ingest_doc(DocIngest(url="https://x.com/a"), **args)
    assert second.created is False
    assert any("no content is stored" in w for w in second.warnings)


def test_duplicate_ingest_warning_names_the_fact_not_the_remedy(store):
    """Warnings are part of the HTTP contract and consumers render them
    verbatim, so the API must not advertise an operator CLI to an audience
    that may not be able to run it."""
    f = CountingFetchers(result=FetchResult(title="X"))
    args = dict(storage=store, fetchers=f, directory=FakeDirectory(), actor="bot")
    ingest_doc(DocIngest(url="https://x.com/a"), **args)
    second = ingest_doc(DocIngest(url="https://x.com/a"), **args)
    joined = " ".join(second.warnings)
    assert "backfill" not in joined and "refetch" not in joined


def test_duplicate_ingest_does_not_warn_when_content_exists(store):
    """Only a genuine gap warrants the warning — a doc that already has text
    must not be flagged as contentless."""
    f = CountingFetchers(result=FetchResult(title="X", content="body", content_snapshot="body"))
    args = dict(storage=store, fetchers=f, directory=FakeDirectory(), actor="bot")
    first = ingest_doc(DocIngest(url="https://x.com/a"), **args)
    assert store.get_doc_content(first.doc.id) == "body"
    second = ingest_doc(DocIngest(url="https://x.com/a"), **args)
    assert not any("no content is stored" in w for w in second.warnings)


def test_reingest_after_soft_remove_does_not_proliferate_active_docs(store):
    # Bug #5 end-to-end: once a URL's earliest row is soft-removed, repeated
    # re-ingest must NOT keep spawning new active duplicates. The dedup lookup
    # now prefers the active row, so the second re-ingest merges instead of
    # inserting.
    f = FakeFetchers(result=FetchResult(title="X"))

    def ingest():
        return ingest_doc(
            DocIngest(url="https://dup.com/a"),
            storage=store,
            fetchers=f,
            directory=FakeDirectory(),
            actor="bot",
        )

    first = ingest()
    assert first.created is True
    # Soft-remove the original row (row kept, active=False).
    store.update_doc(first.doc.id, {"active": False}, actor="admin")

    # Re-ingest: nothing active for this URL, so a fresh active row is created.
    second = ingest()
    assert second.created is True
    assert second.doc.id != first.doc.id

    # Re-ingest again: the live active row must be found and merged into,
    # NOT duplicated. Pre-fix this created a third active doc.
    third = ingest()
    assert third.created is False
    assert third.doc.id == second.doc.id

    active = [
        d for d in store.list_docs(active_only=True) if d.url_normalized == "https://dup.com/a"
    ]
    assert len(active) == 1
    assert active[0].id == second.doc.id


def test_ingest_fetch_failure_warns_and_falls_back_to_url(store):
    fetchers = FakeFetchers(error=FetchError("timeout"))
    res = ingest_doc(
        DocIngest(url="https://github.com/a/b"),
        storage=store,
        fetchers=fetchers,
        directory=FakeDirectory(),
        actor="bot",
    )
    assert res.created is True
    assert res.doc.title == "https://github.com/a/b"
    assert any("fetch" in w for w in res.warnings)


def test_ingest_auth_source_warns_no_snapshot(store):
    res = ingest_doc(
        DocIngest(url="https://drive.google.com/file/d/z"),
        storage=store,
        fetchers=FakeFetchers(),
        directory=FakeDirectory(),
        actor="bot",
    )
    assert res.doc.source_id == "gdrive"
    assert any("auth" in w for w in res.warnings)


def test_ingest_bad_team_id_when_directory_up_raises(store):
    with pytest.raises(BadReference):
        ingest_doc(
            DocIngest(url="https://x.com", owning_team_id=uuid4()),
            storage=store,
            fetchers=FakeFetchers(result=FetchResult(title="X")),
            directory=FakeDirectory(team=None),
            actor="bot",
        )


def test_ingest_directory_down_warns_and_defers_label(store):
    res = ingest_doc(
        DocIngest(url="https://x.com", owning_team_id=uuid4()),
        storage=store,
        fetchers=FakeFetchers(result=FetchResult(title="X")),
        directory=FakeDirectory(unavailable=True),
        actor="bot",
    )
    assert res.created is True
    assert res.doc.owning_team_label is None
    assert any("directory" in w.lower() for w in res.warnings)


def test_ingest_bad_source_id_raises(store):
    with pytest.raises(BadReference):
        ingest_doc(
            DocIngest(url="https://x.com", source_id="nope"),
            storage=store,
            fetchers=FakeFetchers(),
            directory=FakeDirectory(),
            actor="bot",
        )


def test_ingest_applies_grants(store):
    payload = DocIngest(url="https://g.com", grants=[{"grantee_type": "org"}])
    result = ingest_doc(
        payload,
        storage=store,
        fetchers=FakeFetchers(result=FetchResult(title="G")),
        directory=FakeDirectory(),
        actor="t",
    )
    grants = store.list_grants(result.doc.id)
    assert [(g.grantee_type, g.grantee_id) for g in grants] == [("org", None)]
    # Fix: the grant must record the actor passed to ingest_doc, not a hardcoded "ingest".
    assert grants[0].created_by == "t"


def test_ingest_persists_full_content(store):
    fetchers = FakeFetchers(
        result=FetchResult(title="T", content="the full body", content_snapshot="the full")
    )
    res = ingest_doc(
        DocIngest(url="https://github.com/a/b"),
        storage=store,
        fetchers=fetchers,
        directory=FakeDirectory(),
        actor="bot",
    )
    assert store.get_doc_content(res.doc.id) == "the full body"
    assert res.doc.content_snapshot == "the full"


def test_ingest_stores_sha256_of_content(store):
    from hashlib import sha256

    fetchers = FakeFetchers(result=FetchResult(title="T", content="body", content_snapshot="body"))
    res = ingest_doc(
        DocIngest(url="https://github.com/a/c"),
        storage=store,
        fetchers=fetchers,
        directory=FakeDirectory(),
        actor="bot",
    )
    meta = store.get_doc_content_meta(res.doc.id)
    assert meta.content_hash == sha256(b"body").hexdigest()


def test_ingest_truncates_content_over_cap_and_warns(store):
    from src.content import MAX_CONTENT_CHARS

    oversized = "x" * (MAX_CONTENT_CHARS + 100)
    fetchers = FakeFetchers(
        result=FetchResult(title="T", content=oversized, content_snapshot="preview")
    )
    res = ingest_doc(
        DocIngest(url="https://github.com/a/e"),
        storage=store,
        fetchers=fetchers,
        directory=FakeDirectory(),
        actor="bot",
    )
    assert len(store.get_doc_content(res.doc.id)) == MAX_CONTENT_CHARS
    assert any("truncat" in w.lower() for w in res.warnings)


def test_ingest_oversized_connectors_response_warns_via_truncation(store):
    # End-to-end regression for the pre-truncation bug: a real ConnectorsFetcher
    # (over an httpx mock transport, no pre-clamp of its own) feeding ingest_doc
    # must still surface the truncation warning — clamp_content is the only
    # place the size cap is enforced.
    import httpx

    from src.content import MAX_CONTENT_CHARS
    from src.fetch.connectors import ConnectorsFetcher
    from src.fetch.registry import FetcherRegistry

    oversized = "y" * (MAX_CONTENT_CHARS + 1000)

    def handler(request):
        return httpx.Response(200, json={"title": "Big", "content": oversized, "warnings": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    connectors_fetcher = ConnectorsFetcher(
        source_id="github", base_url="http://connectors", api_key="k", client=client
    )
    fetchers = FetcherRegistry({"github": connectors_fetcher})

    res = ingest_doc(
        DocIngest(url="https://github.com/a/big"),
        storage=store,
        fetchers=fetchers,
        directory=FakeDirectory(),
        actor="bot",
    )
    assert len(store.get_doc_content(res.doc.id)) == MAX_CONTENT_CHARS
    assert any("truncat" in w.lower() for w in res.warnings)


def test_ingest_empty_string_content_writes_no_doc_content_row(store):
    # FetchResult's contract says an empty extraction is None; a connector that
    # returns "" anyway must not create a doc_content row holding nothing.
    fetchers = FakeFetchers(result=FetchResult(title="T", content="", content_snapshot=""))
    res = ingest_doc(
        DocIngest(url="https://github.com/a/g"),
        storage=store,
        fetchers=fetchers,
        directory=FakeDirectory(),
        actor="bot",
    )
    assert store.get_doc_content(res.doc.id) is None


def test_ingest_under_cap_content_untouched_and_no_warning(store):
    fetchers = FakeFetchers(
        result=FetchResult(title="T", content="short body", content_snapshot="short body")
    )
    res = ingest_doc(
        DocIngest(url="https://github.com/a/f"),
        storage=store,
        fetchers=fetchers,
        directory=FakeDirectory(),
        actor="bot",
    )
    assert store.get_doc_content(res.doc.id) == "short body"
    assert not any("truncat" in w.lower() for w in res.warnings)


def test_ingest_without_content_creates_no_content_row(store):
    fetchers = FakeFetchers(result=FetchResult(title="T", content=None, content_snapshot=None))
    res = ingest_doc(
        DocIngest(url="https://github.com/a/d"),
        storage=store,
        fetchers=fetchers,
        directory=FakeDirectory(),
        actor="bot",
    )
    assert store.get_doc_content(res.doc.id) is None


def test_ingest_surfaces_fetcher_warnings(store):
    fetchers = FakeFetchers(
        result=FetchResult(
            title="Budget",
            content="a,b",
            content_snapshot="a,b",
            warnings=["first sheet only"],
        )
    )
    res = ingest_doc(
        DocIngest(url="https://github.com/a/warn"),
        storage=store,
        fetchers=fetchers,
        directory=FakeDirectory(),
        actor="bot",
    )
    assert "first sheet only" in res.warnings


def test_ingest_dedup_applies_grants_to_existing_doc(store):
    f = FakeFetchers(result=FetchResult(title="X"))
    first = ingest_doc(
        DocIngest(url="https://x.com/a"),
        storage=store,
        fetchers=f,
        directory=FakeDirectory(),
        actor="bot",
    )
    second = ingest_doc(
        DocIngest(url="https://x.com/a/", grants=[{"grantee_type": "org"}]),
        storage=store,
        fetchers=f,
        directory=FakeDirectory(),
        actor="bot-2",
    )
    assert second.doc.id == first.doc.id
    grants = store.list_grants(first.doc.id)
    assert [(g.grantee_type, g.grantee_id) for g in grants] == [("org", None)]
    # Fix: the dedup branch must also record the real caller, not a hardcoded "ingest".
    assert grants[0].created_by == "bot-2"
