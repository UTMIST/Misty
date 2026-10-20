"""Offline tests for the doc-backfill CLI.

No real network: every test injects a FakeHttp standing in for httpx.Client,
so the suite exercises the real DocsApiClient request/response handling without
reaching documentation-system, connectors, Google or AWS.
"""

import httpx
import pytest

from src.backfill import (
    EMPTY,
    FAILED,
    PLANNED,
    REPAIRED,
    SKIPPED,
    BackfillError,
    DocsApiClient,
    build_parser,
    main,
    run_backfill,
)

ALL_SCOPES = ["docs:read", "docs:read:all", "docs:write"]


class FakeResp:
    def __init__(self, status_code: int, body=None, text: str = "") -> None:
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


class FakeHttp:
    """Routes the calls a backfill makes. `refetch` maps a doc id to the
    response it gets, so a test can make one doc succeed and another fail.

    GET /docs models the catalog, not a fixed reply: a doc whose refetch
    succeeded leaves the contentless listing, exactly as the real service
    behaves — unless its id is in `empty_extraction`, the case where the fetch
    returns 200 and stores nothing (a title-only github doc, an empty page).
    Without that the verification listing would be a tautology."""

    def __init__(
        self,
        *,
        sources=None,
        docs=None,
        refetch=None,
        get_raises=None,
        scopes=None,
        empty_extraction=(),
    ):
        self._sources = sources if sources is not None else []
        self._docs = docs if docs is not None else []
        self._refetch = refetch or {}
        self._get_raises = get_raises
        self._scopes = list(ALL_SCOPES if scopes is None else scopes)
        self._empty_extraction = set(empty_extraction)
        self.refetched: list[str] = []
        self.gets: list[tuple[str, dict]] = []

    @property
    def get_params(self) -> list[dict]:
        """Params of every GET, in order — excluding /api-keys/self, which
        takes none and would only shift the indices tests read."""
        return [p for (url, p) in self.gets if not url.endswith("/api-keys/self")]

    def params_for(self, suffix: str) -> list[dict]:
        return [p for (url, p) in self.gets if url.endswith(suffix)]

    def _repaired_ids(self) -> set[str]:
        done = set()
        for doc_id in self.refetched:
            if doc_id in self._empty_extraction:
                continue
            resp = self._refetch.get(doc_id)
            if resp is None or (isinstance(resp, FakeResp) and resp.status_code == 200):
                done.add(doc_id)
        return done

    def _contentless(self):
        if isinstance(self._docs, FakeResp):
            return self._docs
        repaired = self._repaired_ids()
        return [d for d in self._docs if d.get("id") not in repaired]

    def get(self, url, headers=None, params=None):
        if self._get_raises is not None:
            raise self._get_raises
        self.gets.append((url, dict(params or {})))
        if url.endswith("/api-keys/self"):
            return _as_resp({"name": "content-backfill", "scopes": self._scopes})
        if url.endswith("/sources"):
            return _as_resp(self._sources)
        if url.endswith("/docs"):
            return _as_resp(self._contentless())
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, headers=None):
        doc_id = url.rsplit("/docs/", 1)[1].removesuffix("/refetch")
        self.refetched.append(doc_id)
        outcome = self._refetch.get(doc_id, FakeResp(200, {"id": doc_id}))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _as_resp(value):
    return value if isinstance(value, FakeResp) else FakeResp(200, value)


def _client(http: FakeHttp) -> DocsApiClient:
    return DocsApiClient(base_url="http://docs.test", api_key="doc_test_key", client=http)


def _source(source_id: str, *, fetch: bool) -> dict:
    return {"id": source_id, "content_fetch_enabled": fetch}


def _doc(doc_id: str, *, source_id: str = "web", url: str | None = None) -> dict:
    return {"id": doc_id, "url": url or f"https://x.com/{doc_id}", "source_id": source_id}


# --- discovery wiring -------------------------------------------------------


def test_lists_only_contentless_active_docs():
    """The tool must ask for has_content=false; asking for everything and
    filtering client-side would refetch content the catalog already holds."""
    http = FakeHttp(sources=[_source("web", fetch=True)], docs=[])
    run_backfill(_client(http))
    docs_params = http.get_params[-1]
    assert docs_params["has_content"] == "false"
    assert docs_params["active_only"] == "true"


def test_source_id_narrows_the_listing():
    http = FakeHttp(sources=[_source("web", fetch=True)], docs=[])
    run_backfill(_client(http), source_id="gdocs")
    assert http.get_params[-1]["source_id"] == "gdocs"


# --- the happy path ---------------------------------------------------------


def test_successful_backfill_repairs_every_doc():
    http = FakeHttp(
        sources=[_source("web", fetch=True)],
        docs=[_doc("d1"), _doc("d2")],
    )
    report = run_backfill(_client(http))
    assert http.refetched == ["d1", "d2"]
    assert [r.outcome for r in report.results] == [REPAIRED, REPAIRED]
    assert report.failed == []


# --- item 3: per-doc failure reporting --------------------------------------


def test_repeated_source_failure_is_reported_per_doc_not_skipped():
    """A doc whose source still fails stays contentless and is named in the
    report with its reason — the issue's explicit requirement that failures are
    not silently skipped."""
    http = FakeHttp(
        sources=[_source("web", fetch=True)],
        docs=[_doc("d1"), _doc("d2")],
        refetch={"d2": FakeResp(502, {"detail": "connectors unreachable"})},
    )
    report = run_backfill(_client(http))
    assert [r.outcome for r in report.results] == [REPAIRED, FAILED]
    failure = report.failed[0]
    assert failure.doc_id == "d2"
    assert failure.url == "https://x.com/d2"
    assert "502" in failure.detail and "connectors unreachable" in failure.detail


def test_one_failure_does_not_abort_the_remaining_docs():
    """Order matters here: the failure comes first, so a run that aborted on it
    would never touch d2 and would silently under-repair the corpus."""
    http = FakeHttp(
        sources=[_source("web", fetch=True)],
        docs=[_doc("d1"), _doc("d2")],
        refetch={"d1": FakeResp(502, {"detail": "boom"})},
    )
    report = run_backfill(_client(http))
    assert http.refetched == ["d1", "d2"]
    assert [r.outcome for r in report.results] == [FAILED, REPAIRED]


def test_network_error_on_one_doc_is_a_per_doc_failure():
    """A connectors blip mid-run must not kill the whole run."""
    http = FakeHttp(
        sources=[_source("web", fetch=True)],
        docs=[_doc("d1"), _doc("d2")],
        refetch={"d1": httpx.ConnectError("conn refused")},
    )
    report = run_backfill(_client(http))
    assert report.failed[0].doc_id == "d1"
    assert "unreachable" in report.failed[0].detail
    assert [r.outcome for r in report.results] == [FAILED, REPAIRED]


def test_a_doc_deleted_mid_run_is_reported_not_crashed():
    http = FakeHttp(
        sources=[_source("web", fetch=True)],
        docs=[_doc("d1")],
        refetch={"d1": FakeResp(404, {"detail": "doc not found"})},
    )
    report = run_backfill(_client(http))
    assert "404" in report.failed[0].detail


# --- skipping sources that can never have content --------------------------


def test_sources_with_fetching_disabled_are_skipped_not_attempted():
    """YouTube has content_fetch_enabled=false and no registered
    fetcher, so its documents sit in has_content=false permanently. Attempting to fetch them
    would 502 on every run; they are skipped before any network call and
    bucketed separately from real failures."""
    http = FakeHttp(
        sources=[_source("web", fetch=True), _source("youtube", fetch=False)],
        docs=[_doc("d1", source_id="youtube"), _doc("d2", source_id="web")],
    )
    report = run_backfill(_client(http))
    assert http.refetched == ["d2"]  # youtube never attempted
    skipped = report.of(SKIPPED)
    assert [r.doc_id for r in skipped] == ["d1"]
    assert "youtube" in skipped[0].detail
    assert report.failed == []  # a skip is not a failure


def test_unknown_source_is_skipped_rather_than_attempted():
    """A doc whose source isn't in the active fetch-enabled set at all (an
    inactive or newly removed source) is skipped, not blindly refetched."""
    http = FakeHttp(sources=[_source("web", fetch=True)], docs=[_doc("d1", source_id="mystery")])
    report = run_backfill(_client(http))
    assert http.refetched == []
    assert report.of(SKIPPED)[0].doc_id == "d1"


def test_enabled_sources_are_read_from_the_service_not_hardcoded():
    """Enabling a YouTube fetcher later must need no change to this tool."""
    http = FakeHttp(
        sources=[_source("youtube", fetch=True)],
        docs=[_doc("d1", source_id="youtube")],
    )
    report = run_backfill(_client(http))
    assert http.refetched == ["d1"]
    assert report.of(REPAIRED)[0].doc_id == "d1"


def test_inactive_sources_are_excluded_from_the_enabled_set():
    http = FakeHttp(sources=[_source("web", fetch=True)], docs=[])
    run_backfill(_client(http))
    assert http.params_for("/sources")[0]["active_only"] == "true"


# --- dry run ----------------------------------------------------------------


def test_dry_run_makes_no_fetch():
    http = FakeHttp(
        sources=[_source("web", fetch=True), _source("youtube", fetch=False)],
        docs=[_doc("d1"), _doc("d2", source_id="youtube")],
    )
    report = run_backfill(_client(http), dry_run=True)
    assert http.refetched == []
    assert [r.outcome for r in report.results] == [PLANNED, SKIPPED]


# --- fatal errors stop the run ---------------------------------------------


def test_unauthorized_listing_is_fatal_not_an_empty_run():
    """A 403 from a mis-scoped key must not look like "nothing to repair"."""
    http = FakeHttp(sources=FakeResp(403, {"detail": "missing scope: docs:read"}))
    with pytest.raises(BackfillError, match="403"):
        run_backfill(_client(http))


def test_unreachable_service_is_fatal():
    http = FakeHttp(get_raises=httpx.ConnectError("no route"))
    with pytest.raises(BackfillError, match="failed"):
        run_backfill(_client(http))


def test_non_list_listing_body_is_fatal():
    http = FakeHttp(sources=FakeResp(200, {"not": "a list"}))
    with pytest.raises(BackfillError, match="expected a list"):
        run_backfill(_client(http))


# --- report + exit codes ----------------------------------------------------


def test_summary_counts_each_bucket():
    http = FakeHttp(
        sources=[_source("web", fetch=True), _source("youtube", fetch=False)],
        docs=[_doc("d1"), _doc("d2"), _doc("d3", source_id="youtube")],
        refetch={"d2": FakeResp(502, {"detail": "boom"})},
    )
    report = run_backfill(_client(http))
    summary = report.summary()
    assert "3 contentless doc(s)" in summary
    assert "1 repaired" in summary and "1 failed" in summary and "1 skipped" in summary


def test_outcome_line_includes_id_url_and_detail():
    http = FakeHttp(
        sources=[_source("web", fetch=True)],
        docs=[_doc("d1")],
        refetch={"d1": FakeResp(502, {"detail": "connectors unreachable"})},
    )
    line = run_backfill(_client(http)).failed[0].line()
    assert "d1" in line and "https://x.com/d1" in line and "connectors unreachable" in line


def test_main_without_an_api_key_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("DOCS_API_KEY", raising=False)
    assert main([]) == 2
    assert "DOCS_API_KEY" in capsys.readouterr().err


def test_parser_defaults_base_url_from_env(monkeypatch):
    monkeypatch.setenv("DOCS_BASE_URL", "https://docs.internal")
    assert build_parser().parse_args([]).base_url == "https://docs.internal"


def test_parser_base_url_flag_wins_over_env(monkeypatch):
    monkeypatch.setenv("DOCS_BASE_URL", "https://docs.internal")
    args = build_parser().parse_args(["--base-url", "http://localhost:8001"])
    assert args.base_url == "http://localhost:8001"


def test_api_key_is_sent_as_a_header_not_a_query_param():
    """The key must never land in a URL — proxies and access logs keep those."""
    http = FakeHttp(sources=[_source("web", fetch=True)], docs=[])
    captured = {}

    def get(url, headers=None, params=None):
        captured["headers"] = headers
        captured["url"] = url
        return FakeResp(200, [])

    http.get = get
    _client(http).fetch_enabled_source_ids()
    assert captured["headers"]["X-API-Key"] == "doc_test_key"
    assert "doc_test_key" not in captured["url"]


# --- scope preflight --------------------------------------------------------


def test_missing_read_all_scope_is_fatal_before_any_listing():
    """A key with docs:read but not docs:read:all is NOT rejected by the
    service: read_context resolves it to DENY and GET /docs answers 200 [].
    Discovery would succeed and the run would report "nothing to do" with the
    gap untouched, so the scopes are checked before anything is listed."""
    http = FakeHttp(
        sources=[_source("web", fetch=True)],
        docs=[_doc("d1")],
        scopes=["docs:read", "docs:write"],
    )
    with pytest.raises(BackfillError, match="docs:read:all"):
        run_backfill(_client(http))
    assert http.params_for("/docs") == []  # never reached the catalog
    assert http.params_for("/sources") == []
    assert http.refetched == []


def test_missing_read_scope_is_fatal():
    """docs:read:all does not imply docs:read, and GET /sources needs it."""
    http = FakeHttp(docs=[_doc("d1")], scopes=["docs:read:all", "docs:write"])
    with pytest.raises(BackfillError, match="docs:read"):
        run_backfill(_client(http))


def test_missing_write_scope_is_fatal_for_a_repair_run():
    http = FakeHttp(
        sources=[_source("web", fetch=True)],
        docs=[_doc("d1")],
        scopes=["docs:read", "docs:read:all"],
    )
    with pytest.raises(BackfillError, match="docs:write"):
        run_backfill(_client(http))


def test_a_dry_run_does_not_require_the_write_scope():
    """--dry-run makes no fetch, so a read-only key is enough to survey."""
    http = FakeHttp(
        sources=[_source("web", fetch=True)],
        docs=[_doc("d1")],
        scopes=["docs:read", "docs:read:all"],
    )
    report = run_backfill(_client(http), dry_run=True)
    assert [r.outcome for r in report.results] == [PLANNED]


def test_admin_wildcard_satisfies_every_required_scope():
    http = FakeHttp(sources=[_source("web", fetch=True)], docs=[_doc("d1")], scopes=["admin"])
    report = run_backfill(_client(http))
    assert [r.outcome for r in report.results] == [REPAIRED]


def test_a_scope_listing_without_scopes_is_fatal():
    """Never infer "no scopes named, so presumably fine" — that is how the
    empty-listing trap reappears one layer up."""
    http = FakeHttp(sources=[_source("web", fetch=True)], docs=[_doc("d1")])

    def get(url, headers=None, params=None):
        if url.endswith("/api-keys/self"):
            return FakeResp(200, {"name": "content-backfill"})
        raise AssertionError(f"unexpected GET {url}")

    http.get = get
    with pytest.raises(BackfillError, match="scope list"):
        run_backfill(_client(http))


def test_unauthorized_scope_check_is_fatal():
    http = FakeHttp(sources=[_source("web", fetch=True)], docs=[_doc("d1")])
    http.get = lambda url, headers=None, params=None: FakeResp(401, {"detail": "bad key"})
    with pytest.raises(BackfillError, match="401"):
        run_backfill(_client(http))


# --- verifying the repair actually stored content ---------------------------


def test_a_refetch_that_stores_no_content_is_not_reported_repaired():
    """POST /docs/{id}/refetch returns 200 whenever the fetch RAN. The endpoint
    only writes a doc_content row `if result.content`, so a title-only fetcher
    (github, by design) leaves the doc contentless and still answers 200.
    Reporting that as repaired would mark the gap closed on every run."""
    http = FakeHttp(
        sources=[_source("web", fetch=True)],
        docs=[_doc("d1")],
        empty_extraction={"d1"},
    )
    report = run_backfill(_client(http))
    assert http.refetched == ["d1"]
    assert [r.outcome for r in report.results] == [EMPTY]
    assert "no content" in report.of(EMPTY)[0].detail
    assert report.failed == []  # the fetch worked; the source had nothing
    assert [r.doc_id for r in report.unrepaired] == ["d1"]


def test_verification_demotes_only_the_docs_still_contentless():
    http = FakeHttp(
        sources=[_source("web", fetch=True)],
        docs=[_doc("d1"), _doc("d2")],
        empty_extraction={"d1"},
    )
    report = run_backfill(_client(http))
    assert [(r.doc_id, r.outcome) for r in report.results] == [("d1", EMPTY), ("d2", REPAIRED)]


def test_verification_costs_one_listing_for_the_whole_run():
    """Per-doc confirmation would multiply requests against the catalog for no
    extra information: the contentless set IS the question."""
    http = FakeHttp(
        sources=[_source("web", fetch=True)],
        docs=[_doc("d1"), _doc("d2"), _doc("d3")],
        empty_extraction={"d1"},
    )
    run_backfill(_client(http))
    assert len(http.params_for("/docs")) == 2  # discovery + one verification


def test_verification_listing_keeps_the_source_filter():
    http = FakeHttp(
        sources=[_source("gdocs", fetch=True)],
        docs=[_doc("d1", source_id="gdocs")],
        empty_extraction={"d1"},
    )
    run_backfill(_client(http), source_id="gdocs")
    assert [p.get("source_id") for p in http.params_for("/docs")] == ["gdocs", "gdocs"]


def test_a_dry_run_makes_no_verification_listing():
    http = FakeHttp(sources=[_source("web", fetch=True)], docs=[_doc("d1")])
    run_backfill(_client(http), dry_run=True)
    assert len(http.params_for("/docs")) == 1


def test_a_failed_refetch_is_still_reported_failed_not_empty():
    """Both leave the doc contentless; only one of them is the source's fault,
    and the detail an operator acts on differs."""
    http = FakeHttp(
        sources=[_source("web", fetch=True)],
        docs=[_doc("d1")],
        refetch={"d1": FakeResp(502, {"detail": "connectors unreachable"})},
    )
    report = run_backfill(_client(http))
    assert [r.outcome for r in report.results] == [FAILED]
    assert "connectors unreachable" in report.failed[0].detail


def test_summary_counts_empty_apart_from_repaired_and_failed():
    http = FakeHttp(
        sources=[_source("web", fetch=True)],
        docs=[_doc("d1"), _doc("d2"), _doc("d3")],
        refetch={"d3": FakeResp(502, {"detail": "boom"})},
        empty_extraction={"d2"},
    )
    summary = run_backfill(_client(http)).summary()
    assert "1 repaired" in summary and "1 empty" in summary and "1 failed" in summary


def _patch_http_client(monkeypatch, http: FakeHttp) -> None:
    """Make main()'s `with httpx.Client(...)` yield a FakeHttp."""

    class _Ctx:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return http

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("src.backfill.httpx.Client", _Ctx)


def test_main_exits_1_when_a_doc_extracted_no_content(monkeypatch, capsys):
    """Exit 0 means the corpus is complete. A doc that came back empty is
    exactly the gap this tool exists to surface, so it must not exit 0."""
    monkeypatch.setenv("DOCS_API_KEY", "doc_test_key")
    _patch_http_client(
        monkeypatch,
        FakeHttp(
            sources=[_source("web", fetch=True)],
            docs=[_doc("d1")],
            empty_extraction={"d1"},
        ),
    )
    assert main([]) == 1
    out = capsys.readouterr().out
    assert "empty" in out and "d1" in out


def test_main_exits_0_when_every_doc_really_got_content(monkeypatch):
    monkeypatch.setenv("DOCS_API_KEY", "doc_test_key")
    _patch_http_client(
        monkeypatch, FakeHttp(sources=[_source("web", fetch=True)], docs=[_doc("d1")])
    )
    assert main([]) == 0


def test_main_exits_2_on_a_mis_scoped_key(monkeypatch, capsys):
    monkeypatch.setenv("DOCS_API_KEY", "doc_test_key")
    _patch_http_client(
        monkeypatch,
        FakeHttp(
            sources=[_source("web", fetch=True)],
            docs=[_doc("d1")],
            scopes=["docs:read", "docs:write"],
        ),
    )
    assert main([]) == 2
    assert "docs:read:all" in capsys.readouterr().err


# --- integration: the real app, still offline -------------------------------


def test_backfill_repairs_a_contentless_doc_against_the_real_app(monkeypatch):
    """Drive DocsApiClient against the real FastAPI app (in-memory adapter,
    fake fetchers) instead of FakeHttp.

    This is the test that proves FakeHttp is not lying: it exercises the actual
    query-param names, response shapes and status codes of GET /sources,
    GET /docs?has_content=false and POST /docs/{id}/refetch. Still fully
    offline — no real Google, AWS or network call.
    """
    from uuid import UUID

    from fastapi.testclient import TestClient

    from conftest import build_seed_sources
    from contracts.fetcher import FetchError, FetchResult
    from src.api.app import create_app
    from src.api.deps import get_directory, get_fetchers, get_storage
    from src.config import get_settings
    from src.storage.in_memory import InMemoryStorageAdapter

    monkeypatch.setenv("API_KEY", "test-key")
    get_settings.cache_clear()

    class FakeDirectory:
        def get_team_label(self, team_id):
            return "T"

        def get_person_label(self, person_id):
            return "P"

        def get_active_team_ids(self, person_id):
            return frozenset()

    class FailingFetchers:
        def fetch_for(self, source_id, url):
            raise FetchError("connectors unreachable")

    class WorkingFetchers:
        def fetch_for(self, source_id, url):
            return FetchResult(title="Recovered", content="the body", content_snapshot="the body")

    adapter = InMemoryStorageAdapter(seed_sources=build_seed_sources())
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: adapter
    app.dependency_overrides[get_directory] = lambda: FakeDirectory()
    app.dependency_overrides[get_fetchers] = lambda: FailingFetchers()

    with TestClient(app) as http:
        auth = {"X-API-Key": "test-key"}
        # A doc whose fetch fails at ingest: catalogued, but contentless.
        created = http.post("/docs", json={"url": "https://x.com/broken"}, headers=auth)
        assert created.status_code == 201
        doc_id = created.json()["doc"]["id"]
        assert adapter.get_doc_content(UUID(doc_id)) is None

        # A YouTube doc can never be fetched (content_fetch_enabled=false).
        youtube = http.post("/docs", json={"url": "https://youtube.com/watch?v=page"}, headers=auth)
        youtube_id = youtube.json()["doc"]["id"]

        # The source recovers; the backfill should now repair the web doc only.
        app.dependency_overrides[get_fetchers] = lambda: WorkingFetchers()
        report = run_backfill(DocsApiClient(base_url="", api_key="test-key", client=http))

        by_id = {r.doc_id: r for r in report.results}
        assert by_id[doc_id].outcome == REPAIRED
        assert by_id[youtube_id].outcome == SKIPPED
        assert report.failed == []
        assert adapter.get_doc_content(UUID(doc_id)) == "the body"
        # Repaired docs leave the contentless listing.
        remaining = http.get("/docs?has_content=false", headers=auth).json()
        assert [d["id"] for d in remaining] == [youtube_id]


def test_a_title_only_fetch_is_reported_empty_against_the_real_app(monkeypatch):
    """The github case, end to end: `content_fetch_enabled=true` and a fetcher
    that only ever derives a title from the URL. The refetch endpoint answers
    200 and writes no doc_content row, so the doc stays in has_content=false
    run after run. It must be reported as `empty`, not `repaired`, and exit
    non-zero."""
    from uuid import UUID

    from fastapi.testclient import TestClient

    from conftest import build_seed_sources
    from contracts.fetcher import FetchError, FetchResult
    from src.api.app import create_app
    from src.api.deps import get_directory, get_fetchers, get_storage
    from src.config import get_settings
    from src.storage.in_memory import InMemoryStorageAdapter

    monkeypatch.setenv("API_KEY", "test-key")
    get_settings.cache_clear()

    class FakeDirectory:
        def get_team_label(self, team_id):
            return "T"

        def get_person_label(self, person_id):
            return "P"

        def get_active_team_ids(self, person_id):
            return frozenset()

    class FailingFetchers:
        def fetch_for(self, source_id, url):
            raise FetchError("unreachable")

    class TitleOnlyFetchers:
        """GithubFetcher's real shape: a title, no content."""

        def fetch_for(self, source_id, url):
            return FetchResult(title="owner/repo", content_snapshot=None)

    adapter = InMemoryStorageAdapter(seed_sources=build_seed_sources())
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: adapter
    app.dependency_overrides[get_directory] = lambda: FakeDirectory()
    app.dependency_overrides[get_fetchers] = lambda: FailingFetchers()

    with TestClient(app) as http:
        auth = {"X-API-Key": "test-key"}
        created = http.post("/docs", json={"url": "https://github.com/owner/repo"}, headers=auth)
        doc_id = created.json()["doc"]["id"]

        app.dependency_overrides[get_fetchers] = lambda: TitleOnlyFetchers()
        report = run_backfill(DocsApiClient(base_url="", api_key="test-key", client=http))

        assert [r.outcome for r in report.results] == [EMPTY]
        assert report.results[0].doc_id == doc_id
        # The refetch genuinely succeeded — the title landed — but no content did.
        assert adapter.get_doc_content(UUID(doc_id)) is None
        assert http.get("/docs?has_content=false", headers=auth).json()[0]["id"] == doc_id


def test_a_key_without_read_all_is_fatal_against_the_real_app(monkeypatch):
    """The trap this preflight exists for: GET /docs with a docs:read key and
    no X-On-Behalf-Of is not a 403. read_context resolves to DENY, the catalog
    answers 200 [], and a run that trusted it would report "nothing to do"
    while a contentless doc sat there."""
    from fastapi.testclient import TestClient

    from conftest import build_seed_sources
    from contracts.fetcher import FetchError
    from src.api.app import create_app
    from src.api.deps import get_directory, get_fetchers, get_storage
    from src.api.hashing import generate_key
    from src.config import get_settings
    from src.storage.in_memory import InMemoryStorageAdapter

    monkeypatch.setenv("API_KEY", "test-key")
    get_settings.cache_clear()

    class FakeDirectory:
        def get_team_label(self, team_id):
            return "T"

        def get_person_label(self, person_id):
            return "P"

        def get_active_team_ids(self, person_id):
            return frozenset()

    class FailingFetchers:
        def fetch_for(self, source_id, url):
            raise FetchError("unreachable")

    adapter = InMemoryStorageAdapter(seed_sources=build_seed_sources())
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: adapter
    app.dependency_overrides[get_directory] = lambda: FakeDirectory()
    app.dependency_overrides[get_fetchers] = lambda: FailingFetchers()

    plaintext, prefix, key_hash = generate_key()
    adapter.create_api_key(
        name="mis-scoped",
        prefix=prefix,
        key_hash=key_hash,
        scopes=["docs:read", "docs:write"],  # no docs:read:all
        actor="test",
    )

    with TestClient(app) as http:
        created = http.post(
            "/docs", json={"url": "https://x.com/broken"}, headers={"X-API-Key": "test-key"}
        )
        assert created.status_code == 201

        # The trap itself: 200 and an empty list, not 403.
        listed = http.get("/docs?has_content=false", headers={"X-API-Key": plaintext})
        assert listed.status_code == 200
        assert listed.json() == []

        with pytest.raises(BackfillError, match="docs:read:all"):
            run_backfill(DocsApiClient(base_url="", api_key=plaintext, client=http))
