"""Offline scheduler tests: real HTTP serialization, no provider calls."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from src import source_refresh
from src.source_refresh import DocsApiClient, RefreshError, RunMonitor, main, run_refresh


@pytest.fixture(autouse=True)
def no_live_monitor(monkeypatch):
    monkeypatch.delenv("SOURCE_REFRESH_MONITOR_URL", raising=False)


def counts(attempted=0, failed=0, index_failed=0):
    return {
        "attempted": attempted,
        "refreshed": attempted - failed,
        "failed": failed,
        "index_failed": index_failed,
    }


@contextmanager
def api(responses, *, scopes=None):
    requests = []
    replies = iter(responses)

    def handle(request):
        requests.append(request)
        assert request.headers["X-API-Key"] == "offline-key"
        assert "X-On-Behalf-Of" not in request.headers
        if request.url.path == "/api-keys/self":
            assert request.method == "GET"
            return httpx.Response(
                200, json={"scopes": ["docs:write"] if scopes is None else scopes}
            )
        if request.url.path.startswith("/docs/source-access/refresh-runs/"):
            assert request.method in {"POST", "DELETE"}
            return httpx.Response(200, json={"run_id": request.url.path.rsplit("/", 1)[1]})
        assert request.method == "POST"
        assert request.url.path == "/docs/source-access/refresh-due"
        result = next(replies)
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        yield (
            DocsApiClient(
                base_url="http://docs.test/", api_key=SecretStr("offline-key"), client=http
            ),
            requests,
        )


def test_drains_full_batches_then_stops_at_short_batch():
    with api([counts(2), counts(2), counts(1)]) as (client, requests):
        report = run_refresh(client, batch_size=2)
    assert (report.batches, report.attempted, report.refreshed) == (3, 5, 5)
    assert report.drained and report.exit_code == 0
    assert all(r.url.params["limit"] == "2" for r in requests[1:])


def test_exact_multiple_requires_empty_probe():
    with api([counts(2), counts()]) as (client, _):
        report = run_refresh(client, batch_size=2)
    assert report.batches == 2
    assert report.drained


@pytest.mark.parametrize("scopes", [["docs:read"], [], "admin", [None]])
def test_scope_preflight_prevents_any_refresh(scopes):
    with api([], scopes=scopes) as (client, requests):
        report = run_refresh(client)
    assert len(requests) == 1
    assert report.exit_code == 2 and not report.drained


@pytest.mark.parametrize("scopes", [["docs:write"], ["admin"]])
def test_empty_queue_succeeds_with_write_or_admin_scope(scopes):
    with api([counts()], scopes=scopes) as (client, _):
        report = run_refresh(client)
    assert report.drained and report.attempted == 0 and report.exit_code == 0


def test_document_and_index_failures_do_not_stop_batch_progress():
    with api([counts(2, failed=1, index_failed=1), counts()]) as (client, _):
        report = run_refresh(client, batch_size=2)
    assert report.drained and report.exit_code == 1
    assert (report.failed, report.index_failed, report.batches) == (1, 1, 2)


@pytest.mark.parametrize(
    "reply",
    [
        httpx.ReadTimeout("secret response and private URL"),
        httpx.Response(503, text="secret response and document content"),
        httpx.Response(307, headers={"Location": "http://untrusted.test"}),
        httpx.Response(200, text="secret invalid JSON"),
        [],
        {},
        {"attempted": 0, "refreshed": 0, "failed": 0},
        {**counts(), "attempted": True},
        {**counts(), "attempted": "0"},
        {**counts(), "failed": -1},
        {**counts(1), "refreshed": 0},
        counts(3),
        counts(1, index_failed=2),
    ],
)
def test_bad_batch_stops_without_retry_or_sensitive_output(reply):
    with api([counts(2), reply]) as (client, requests):
        report = run_refresh(client, batch_size=2)
    assert len(requests) == 3
    assert report.exit_code == 2 and not report.drained
    assert report.attempted == 2  # Preserve confirmed progress before the failed request.
    assert "secret" not in report.error
    assert "untrusted" not in report.error


def test_batch_budget_reports_incomplete_even_if_every_attempt_succeeded():
    with api([counts(2), counts(2)]) as (client, requests):
        report = run_refresh(client, batch_size=2, max_batches=2)
    assert len(requests) == 3
    assert report.attempted == 4
    assert not report.drained and report.exit_code == 2


def test_time_budget_prevents_next_post_and_caps_timeouts(monkeypatch):
    ticks = iter([100, 100, 102, 111])
    monkeypatch.setattr(source_refresh.time, "monotonic", lambda: next(ticks))
    with api([counts(2)]) as (client, requests):
        report = run_refresh(client, batch_size=2, max_seconds=10)
    assert len(requests) == 2
    assert requests[0].extensions["timeout"]["read"] == 10
    assert requests[1].extensions["timeout"]["read"] == 8
    assert report.attempted == 2 and report.exit_code == 2


@pytest.mark.parametrize(
    "options",
    [
        {"batch_size": 0},
        {"batch_size": 101},
        {"max_batches": 0},
        {"timeout": 0},
        {"timeout": float("inf")},
        {"max_seconds": float("nan")},
    ],
)
def test_invalid_limits_fail_before_network(options):
    with api([]) as (client, requests):
        with pytest.raises(RefreshError):
            run_refresh(client, **options)
    assert requests == []


@pytest.mark.parametrize(
    "url", ["docs.test", "ftp://docs.test", "http://secret@docs.test", "http://["]
)
def test_invalid_target_is_rejected_without_echoing_url(url):
    with httpx.Client() as http, pytest.raises(RefreshError) as caught:
        DocsApiClient(base_url=url, api_key=SecretStr("offline-key"), client=http)
    assert "secret" not in str(caught.value)


def test_cli_missing_key_fails_before_network(monkeypatch, capsys):
    monkeypatch.delenv("DOCS_API_KEY", raising=False)
    monkeypatch.setattr(
        source_refresh.httpx.Client, "request", lambda *a, **kw: pytest.fail("no network needed")
    )
    assert main([]) == 2
    assert "DOCS_API_KEY is not set" in capsys.readouterr().err


@pytest.mark.parametrize("failed,index_failed,exit_code", [(0, 0, 0), (1, 0, 1), (0, 1, 1)])
def test_cli_env_and_sanitized_summary(monkeypatch, capsys, failed, index_failed, exit_code):
    with api([counts(1, failed=failed, index_failed=index_failed)]) as (client, _):
        monkeypatch.setenv("DOCS_API_KEY", "offline-key")
        monkeypatch.setenv("DOCS_BASE_URL", "http://docs.test")

        def injected_client(**kwargs):
            assert kwargs["base_url"] == "http://docs.test"
            assert kwargs["api_key"].get_secret_value() == "offline-key"
            return client

        monkeypatch.setattr(source_refresh, "DocsApiClient", injected_client)
        assert main([]) == exit_code
    output = capsys.readouterr()
    assert f"failed={failed} index_failed={index_failed} drained=true" in output.out
    assert "offline-key" not in output.out + output.err


def test_runner_advances_past_failed_documents_through_real_api(monkeypatch, fake_embedder):
    """Prove the API's attempted-at semantics actually let the runner drain."""
    monkeypatch.setenv("API_KEY", "test-key")
    from fastapi.testclient import TestClient

    from contracts.fetcher import FetchError
    from src.api.app import create_app
    from src.api.deps import get_directory, get_fetchers, get_storage
    from src.api.hashing import generate_key
    from src.storage.in_memory import InMemoryStorageAdapter
    from tests.conftest import build_seed_sources
    from tests.test_api_indexing import _key
    from tests.test_api_source_access import ADMIN, P1, P2, FakeDirectory, _result

    store = InMemoryStorageAdapter(seed_sources=build_seed_sources())
    app = create_app()

    class Fetchers:
        fail = False
        calls = []
        result = _result("alice@example.com", content="Synthetic scheduler test.")

        def fetch_for(self, source, url):
            self.calls.append(url)
            if self.fail and "failed" in url:
                raise FetchError("offline failure")
            return self.result

    fetchers = Fetchers()
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_directory] = lambda: FakeDirectory()
    app.dependency_overrides[get_fetchers] = lambda: fetchers
    with TestClient(app) as http:
        doc_ids = {}
        for name in ("failed", "successful", "last"):
            response = http.post(
                "/docs",
                headers=ADMIN,
                json={"url": f"https://docs.google.com/document/d/{name}/edit"},
            )
            assert response.status_code == 201
            doc_ids[name] = UUID(response.json()["doc"]["id"])
            old = datetime.now(timezone.utc) - timedelta(days=2)
            store.update_doc(
                UUID(response.json()["doc"]["id"]),
                {"source_access_synced_at": old, "source_access_attempted_at": old},
                actor="test",
            )
        fetchers.fail = True
        fetchers.calls.clear()
        fetchers.result = _result("bob@example.com", content="Updated synthetic content.")
        fake_embedder.fail_on_call = len(fake_embedder.calls) + 1
        token, prefix, key_hash = generate_key()
        store.create_api_key(
            name="source-refresh-test",
            prefix=prefix,
            key_hash=key_hash,
            scopes=["docs:write"],
            actor="test",
        )
        client = DocsApiClient(base_url="http://testserver", api_key=SecretStr(token), client=http)
        report = run_refresh(client, batch_size=1, max_batches=5)
        assert (report.attempted, report.refreshed, report.failed) == (3, 2, 1)
        assert report.index_failed == 1
        assert report.drained and report.exit_code == 1
        assert len(fetchers.calls) == 3
        assert all(doc.updated_by == "source-refresh-test" for doc in store.list_docs())
        assert run_refresh(client).attempted == 0
        assert store.get_doc_content(doc_ids["failed"]) == "Synthetic scheduler test."
        assert [grant.grantee_id for grant in store.list_grants(doc_ids["failed"])] == [P1]
        for name in ("successful", "last"):
            assert store.get_doc_content(doc_ids[name]) == "Updated synthetic content."
            assert [grant.grantee_id for grant in store.list_grants(doc_ids[name])] == [P2]

        # A refresh is now fresh even if indexing failed. Only the separate
        # docs:index recovery path repairs those chunks, without re-fetching.
        assert store.list_docs_for_indexing() == [doc_ids["successful"]]
        recovered = http.post("/docs/index", headers=_key(store, "docs:index"), json={})
        assert recovered.status_code == 200
        assert recovered.json()["indexed"] == 1
        assert store.list_docs_for_indexing() == []
        assert len(fetchers.calls) == 3

        # Failed refresh attempts wait for the next eligibility window.
        fetchers.fail = False
        store.update_doc(doc_ids["failed"], {"source_access_attempted_at": old}, actor="test")
        retry = run_refresh(client)
        assert retry.exit_code == 0 and retry.attempted == 1
        assert store.get_doc_content(doc_ids["failed"]) == "Updated synthetic content."


@pytest.mark.parametrize(
    "reply,expected",
    [
        (httpx.Response(200, text="OK"), True),
        (httpx.Response(200, text="OK (not found)"), False),
        (httpx.Response(503, text="private upstream body"), False),
        (httpx.Response(302, headers={"Location": "https://other.test"}), False),
        (httpx.ReadTimeout("private ping URL"), False),
    ],
)
def test_monitor_delivery_is_checked_without_catalog_credentials(reply, expected):
    requests = []

    def handle(request):
        requests.append(request)
        assert "X-API-Key" not in request.headers
        assert request.content == b""
        if isinstance(reply, Exception):
            raise reply
        return reply

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        monitor = RunMonitor(SecretStr("https://monitor.test/private-token"), client=http)
        assert monitor.ping("start") is expected
        assert monitor.ping("success") is expected
        assert monitor.ping("fail") is expected
    assert [r.url.path for r in requests] == [
        "/private-token/start",
        "/private-token",
        "/private-token/fail",
    ]
    assert len({r.url.params["rid"] for r in requests}) == 1
    assert UUID(requests[0].url.params["rid"])


@pytest.mark.parametrize(
    "url",
    [
        "http://monitor.test/token",
        "https://secret@monitor.test/token",
        "private-token",
        "https://monitor.test/token?secret=yes",
        "https://monitor.test/token#secret",
    ],
)
def test_monitor_rejects_invalid_url_without_disclosing_it(url):
    with httpx.Client() as http, pytest.raises(RefreshError) as caught:
        RunMonitor(SecretStr(url), client=http)
    assert "secret" not in str(caught.value)
    assert "private-token" not in str(caught.value)


@pytest.mark.parametrize(
    "reply,expected",
    [
        (counts(), 0),
        (counts(1, failed=1), 1),
        (counts(1, index_failed=1), 1),
        (httpx.ReadTimeout("private body"), 2),
    ],
)
def test_cli_signals_success_only_after_clean_drain(monkeypatch, capsys, reply, expected):
    events = []
    monkeypatch.setenv("DOCS_API_KEY", "offline-key")
    monkeypatch.setenv("SOURCE_REFRESH_MONITOR_URL", "https://monitor.test/private-token")

    def ping(self, event):
        events.append(event)
        return True

    monkeypatch.setattr(RunMonitor, "ping", ping)
    with api([reply]) as (client, _):
        monkeypatch.setattr(source_refresh, "DocsApiClient", lambda **kw: client)
        assert main(["--require-monitor"]) == expected
    assert events == ["start", "success" if expected == 0 else "fail"]
    output = capsys.readouterr()
    assert f"exit_code={expected}" in output.out
    assert all(secret not in output.out + output.err for secret in ["private", "offline-key"])


@pytest.mark.parametrize("fail_event", ["start", "success"])
def test_monitor_outage_never_replays_refresh_or_reports_success(monkeypatch, capsys, fail_event):
    events = []
    monkeypatch.setenv("DOCS_API_KEY", "offline-key")
    monkeypatch.setenv("SOURCE_REFRESH_MONITOR_URL", "https://monitor.test/private-token")

    def ping(self, event):
        events.append(event)
        return event != fail_event

    monkeypatch.setattr(RunMonitor, "ping", ping)
    with api([counts()]) as (client, requests):
        monkeypatch.setattr(source_refresh, "DocsApiClient", lambda **kw: client)
        assert main([]) == 2
    assert sum(r.url.path == source_refresh.REFRESH_PATH for r in requests) == 1
    assert all(r.method != "DELETE" for r in requests)  # Delivery is uncertain; retain ownership.
    assert events == ["start", "fail" if fail_event == "start" else "success"]
    assert "monitor_failed=true" in capsys.readouterr().out


def test_cron_requires_monitor_before_api_call(monkeypatch, capsys):
    monkeypatch.setenv("DOCS_API_KEY", "offline-key")
    monkeypatch.setattr(httpx.Client, "request", lambda *a, **kw: pytest.fail("no network"))
    assert main(["--require-monitor"]) == 2
    assert "SOURCE_REFRESH_MONITOR_URL is required" in capsys.readouterr().err


def test_configuration_failure_signals_failure(monkeypatch):
    events = []
    monkeypatch.delenv("DOCS_API_KEY", raising=False)
    monkeypatch.setenv("SOURCE_REFRESH_MONITOR_URL", "https://monitor.test/private-token")
    monkeypatch.setattr(RunMonitor, "ping", lambda self, event: events.append(event) or True)
    assert main([]) == 2
    assert events == ["fail"]


def test_later_run_recovers_after_api_unavailability():
    with api([httpx.Response(503), counts(1)]) as (client, requests):
        failed = run_refresh(client)
        assert failed.exit_code == 2 and not failed.drained
        assert len(requests) == 2
        recovered = run_refresh(client)
    assert recovered.exit_code == 0 and recovered.attempted == 1
    assert len(requests) == 4


def test_railway_config_only_schedules_staging_and_never_runs_preview_jobs():
    config = json.loads((Path(__file__).parents[1] / "railway.source-refresh.json").read_text())
    base = config["deploy"]
    staging = base | config["environments"]["staging"]["deploy"]
    # PR overrides take precedence over the preview's base environment.
    preview = staging | config["environments"]["pr"]["deploy"]
    assert staging["cronSchedule"] == "0 * * * *"
    assert staging["startCommand"] == "python -m src.source_refresh --require-monitor"
    for disabled in [base, preview]:
        assert disabled["cronSchedule"] is None
        assert "src.source_refresh" not in disabled["startCommand"]
    for deploy in [base, staging, preview]:
        assert deploy["preDeployCommand"] is None
        assert deploy["healthcheckPath"] is None
        assert deploy["restartPolicyType"] == "NEVER"
        assert deploy["numReplicas"] == 1


@pytest.mark.parametrize("blocked_event", ["start", "success"])
def test_overlapping_runner_cannot_mask_or_release_a_stuck_run(monkeypatch, blocked_event):
    """A second process must fail before replacing the first heartbeat's start."""
    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.api.deps import get_storage
    from src.storage.in_memory import InMemoryStorageAdapter
    from tests.conftest import build_seed_sources
    from tests.test_api_indexing import _key

    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("SOURCE_REFRESH_MONITOR_URL", "https://monitor.test/private-token")
    store = InMemoryStorageAdapter(seed_sources=build_seed_sources())
    headers = _key(store, "docs:write")
    token = headers["X-API-Key"]
    monkeypatch.setenv("DOCS_API_KEY", token)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: store
    events = []
    duplicate_exits = []

    def ping(self, event):
        events.append((self.run_id, event))
        if event == blocked_event and not duplicate_exits:
            duplicate_exits.append(main([]))
            # The owning process dies after its start, without cleanup.
            raise RuntimeError("simulated process death")
        return True

    monkeypatch.setattr(RunMonitor, "ping", ping)
    with TestClient(app) as http:
        monkeypatch.setattr(
            source_refresh,
            "DocsApiClient",
            lambda **kw: DocsApiClient(
                base_url="http://testserver", api_key=SecretStr(token), client=http
            ),
        )
        with pytest.raises(RuntimeError, match="simulated process death"):
            main([])
        assert duplicate_exits == [2]
        owner_events = ["start"] if blocked_event == "start" else ["start", "success"]
        assert [event for _, event in events] == owner_events + ["fail"]
        assert main([]) == 2  # Process death must not free the shared guard.
        assert [event for _, event in events] == owner_events + ["fail", "fail"]
        owner = events[0][0]
        loser = events[-1][0]
        path = "/docs/source-access/refresh-runs/"
        assert http.delete(path + loser, headers=headers).status_code == 409
        # An operator releases only the recorded owner, after stopping its work.
        assert http.delete(path + owner, headers=headers).status_code == 200
        assert main([]) == 0
        assert [event for _, event in events][-2:] == ["start", "success"]


@pytest.mark.parametrize(
    "fault",
    ["acquire_timeout", "acquire_ack", "api_timeout", "start", "success", "release_timeout"],
)
def test_uncertain_completion_retains_guard_until_operator_recovery(monkeypatch, fault):
    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.api.deps import get_storage
    from src.storage.in_memory import InMemoryStorageAdapter
    from tests.conftest import build_seed_sources
    from tests.test_api_indexing import _key

    store = InMemoryStorageAdapter(seed_sources=build_seed_sources())
    headers = _key(store, "docs:write")
    token = headers["X-API-Key"]
    monkeypatch.setenv("DOCS_API_KEY", token)
    monkeypatch.setenv("SOURCE_REFRESH_MONITOR_URL", "https://monitor.test/private-token")
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: store
    events = []

    def ping(self, event):
        events.append((self.run_id, event))
        return event != fault

    monkeypatch.setattr(RunMonitor, "ping", ping)
    with TestClient(app) as http:
        original_request = http.request

        def request(method, url, **kwargs):
            if fault == "release_timeout" and method == "DELETE":
                raise httpx.ReadTimeout("private request detail")
            response = original_request(method, url, **kwargs)
            if method == "POST" and "refresh-runs/" in url and response.status_code == 200:
                if fault == "acquire_timeout":
                    raise httpx.ReadTimeout("private response detail")
                if fault == "acquire_ack":
                    return httpx.Response(200, json={"run_id": "invalid acknowledgement"})
            if fault == "api_timeout" and source_refresh.REFRESH_PATH in url:
                raise httpx.ReadTimeout("private response detail")
            return response

        monkeypatch.setattr(http, "request", request)
        monkeypatch.setattr(
            source_refresh,
            "DocsApiClient",
            lambda **kw: DocsApiClient(
                base_url="http://testserver", api_key=SecretStr(token), client=http
            ),
        )
        assert main([]) == 2
        owner = events[0][0]
        assert events[-1][1] == ("success" if fault == "success" else "fail")
        boundary = len(events)
        assert main([]) == 2
        assert [event for _, event in events[boundary:]] == ["fail"]
        # Recovery after inspection allows the next run, including after ping failure.
        monkeypatch.setattr(http, "request", original_request)
        assert (
            http.delete(f"/docs/source-access/refresh-runs/{owner}", headers=headers).status_code
            == 200
        )
        monkeypatch.setattr(RunMonitor, "ping", lambda self, event: True)
        assert main([]) == 0
