"""Offline tests for the doc-backfill CLI.

No real network: every test injects a FakeHttp standing in for httpx.Client,
so the suite exercises the real DocsApiClient request/response handling without
reaching documentation-system, connectors, Google or AWS.
"""

import httpx
import pytest

from src.backfill import (
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
    """Routes the three calls a backfill makes. `refetch` maps a doc id to the
    response it gets, so a test can make one doc succeed and another fail."""

    def __init__(self, *, sources=None, docs=None, refetch=None, get_raises=None):
        self._sources = sources if sources is not None else []
        self._docs = docs if docs is not None else []
        self._refetch = refetch or {}
        self._get_raises = get_raises
        self.refetched: list[str] = []
        self.get_params: list[dict] = []

    def get(self, url, headers=None, params=None):
        if self._get_raises is not None:
            raise self._get_raises
        self.get_params.append(dict(params or {}))
        if url.endswith("/sources"):
            return _as_resp(self._sources)
        if url.endswith("/docs"):
            return _as_resp(self._docs)
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
    """notion/youtube have content_fetch_enabled=false and no registered
    fetcher, so they sit in has_content=false permanently. Attempting them
    would 502 on every run; they are skipped before any network call and
    bucketed separately from real failures."""
    http = FakeHttp(
        sources=[_source("web", fetch=True), _source("notion", fetch=False)],
        docs=[_doc("d1", source_id="notion"), _doc("d2", source_id="web")],
    )
    report = run_backfill(_client(http))
    assert http.refetched == ["d2"]  # notion never attempted
    skipped = report.of(SKIPPED)
    assert [r.doc_id for r in skipped] == ["d1"]
    assert "notion" in skipped[0].detail
    assert report.failed == []  # a skip is not a failure


def test_unknown_source_is_skipped_rather_than_attempted():
    """A doc whose source isn't in the active fetch-enabled set at all (an
    inactive or newly removed source) is skipped, not blindly refetched."""
    http = FakeHttp(sources=[_source("web", fetch=True)], docs=[_doc("d1", source_id="mystery")])
    report = run_backfill(_client(http))
    assert http.refetched == []
    assert report.of(SKIPPED)[0].doc_id == "d1"


def test_enabled_sources_are_read_from_the_service_not_hardcoded():
    """Enabling a Notion fetcher later must need no change to this tool."""
    http = FakeHttp(
        sources=[_source("notion", fetch=True)],
        docs=[_doc("d1", source_id="notion")],
    )
    report = run_backfill(_client(http))
    assert http.refetched == ["d1"]
    assert report.of(REPAIRED)[0].doc_id == "d1"


def test_inactive_sources_are_excluded_from_the_enabled_set():
    http = FakeHttp(sources=[_source("web", fetch=True)], docs=[])
    run_backfill(_client(http))
    assert http.get_params[0]["active_only"] == "true"


# --- dry run ----------------------------------------------------------------


def test_dry_run_makes_no_fetch():
    http = FakeHttp(
        sources=[_source("web", fetch=True), _source("notion", fetch=False)],
        docs=[_doc("d1"), _doc("d2", source_id="notion")],
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
        sources=[_source("web", fetch=True), _source("notion", fetch=False)],
        docs=[_doc("d1"), _doc("d2"), _doc("d3", source_id="notion")],
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

        # A notion doc can never be fetched (content_fetch_enabled=false).
        notion = http.post("/docs", json={"url": "https://notion.so/page"}, headers=auth)
        notion_id = notion.json()["doc"]["id"]

        # The source recovers; the backfill should now repair the web doc only.
        app.dependency_overrides[get_fetchers] = lambda: WorkingFetchers()
        report = run_backfill(DocsApiClient(base_url="", api_key="test-key", client=http))

        by_id = {r.doc_id: r for r in report.results}
        assert by_id[doc_id].outcome == REPAIRED
        assert by_id[notion_id].outcome == SKIPPED
        assert report.failed == []
        assert adapter.get_doc_content(UUID(doc_id)) == "the body"
        # Repaired docs leave the contentless listing.
        remaining = http.get("/docs?has_content=false", headers=auth).json()
        assert [d["id"] for d in remaining] == [notion_id]
