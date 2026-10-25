import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from src.discovery import (
    DiscoveryError,
    DiscoveryMonitor,
    Item,
    Listing,
    Settings,
    State,
    checkpoint,
    main,
    save_state,
)


@pytest.mark.parametrize(
    "status,body,accepted",
    [
        (200, "OK", True),
        (200, "OK (not found)", False),
        (200, "rate limited", False),
        (302, "OK", False),
        (503, "upstream secret", False),
    ],
)
def test_monitor_requires_ack_and_sends_no_credentials_or_source_data(status, body, accepted):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monitor = DiscoveryMonitor(
            SecretStr("https://monitor.test/secret"), client=client, run_id="test-run"
        )
        assert monitor.ping("start") is accepted
        assert monitor.ping("success") is accepted
        assert monitor.ping("fail") is accepted
    assert [r.url.path for r in requests] == ["/secret/start", "/secret", "/secret/fail"]
    for request in requests:
        assert request.method == "POST"
        assert not request.content
        assert "x-api-key" not in request.headers
        assert "authorization" not in request.headers
        assert dict(request.url.params) == {"rid": "test-run"}
        assert set(request.extensions["timeout"].values()) == {10}


@pytest.mark.parametrize(
    "url",
    [
        "http://monitor.test/secret",
        "https://user:secret@monitor.test/ping",
        "https://monitor.test/secret?token=secret",
        "https://monitor.test/secret#fragment",
        "not-a-url",
    ],
)
def test_invalid_monitor_urls_never_leak_secrets(url):
    with pytest.raises(DiscoveryError) as exc:
        DiscoveryMonitor(SecretStr(url), client=None, run_id="test-run")
    assert url not in str(exc.value)
    assert "secret" not in str(exc.value)


@pytest.fixture
def monitored_cli(monkeypatch, tmp_path):
    config = {
        "DISCOVERY_ENABLED": "true",
        "DISCOVERY_STATE_PATH": str(tmp_path / "state.json"),
        "DISCOVERY_MONITOR_URL": "https://monitor.test/run-secret",
        "DISCOVERY_COMPLETION_MONITOR_URL": "https://monitor.test/complete-secret",
        "DOCS_BASE_URL": "https://docs.test",
        "DOCS_API_KEY": "catalog-secret",
        "CONNECTORS_BASE_URL": "https://connectors.test",
        "CONNECTORS_API_KEY": "connector-secret",
    }
    for name, value in config.items():
        monkeypatch.setenv(name, value)
    requests = []
    monitor_events = []
    outcome = {
        "import": "indexed",
        "monitor_failure": None,
        "listing_failure": False,
        "incomplete": False,
        "warnings": 0,
        "listing_denied": False,
        "import_rejected": False,
    }

    def handler(request):
        requests.append(request)
        if request.url.host == "monitor.test":
            monitor_events.append(request.url.path)
            assert not request.content
            assert "x-api-key" not in request.headers
            UUID(request.url.params["rid"])
            if request.url.path == outcome["monitor_failure"]:
                raise httpx.ReadTimeout("secret network detail", request=request)
            if request.url.path == "/complete-secret":
                # Success is sent only after durable completion.
                state = State.model_validate_json(Path(config["DISCOVERY_STATE_PATH"]).read_text())
                assert state.completed_at and not state.pending and not state.frontier
            return httpx.Response(200, text="OK")
        if request.url.path == "/api-keys/self":
            assert request.headers["x-api-key"] == "catalog-secret"
            return httpx.Response(200, json={"scopes": ["docs:write"]})
        if request.url.path == "/discover/google":
            assert request.headers["x-api-key"] == "connector-secret"
            if outcome["listing_failure"]:
                return httpx.Response(503, text="secret upstream content")
            if outcome["listing_denied"]:
                return httpx.Response(403, json={"detail": "source denied listing"})
            return httpx.Response(
                200,
                json={
                    "incomplete": outcome["incomplete"],
                    "items": [
                        {"file_id": name, "kind": "file", "source_id": "gdocs"}
                        for name in ("one", "two")
                    ],
                },
            )
        assert request.url.path == "/docs/discovery"
        assert request.headers["x-api-key"] == "catalog-secret"
        if outcome["import_rejected"]:
            return httpx.Response(422, text="secret upstream detail")
        return httpx.Response(
            200,
            json={
                "status": outcome["import"],
                "warnings": outcome["warnings"],
                "doc_id": "11111111-1111-1111-1111-111111111111",
            },
        )

    client_type = httpx.Client
    monkeypatch.setattr(
        "src.discovery.httpx.Client",
        lambda: client_type(transport=httpx.MockTransport(handler)),
    )
    return config, requests, monitor_events, outcome


def test_clean_scan_pings_both_checks_after_checkpoint(monitored_cli, capsys):
    _, requests, events, _ = monitored_cli
    assert main(["--require-monitor"]) == 0
    assert events == ["/run-secret/start", "/complete-secret", "/run-secret"]
    assert len({r.url.params["rid"] for r in requests if r.url.host == "monitor.test"}) == 1
    output = capsys.readouterr()
    assert "secret" not in output.out + output.err
    assert json.loads(output.out.splitlines()[-1])["monitor_failed"] is False


def test_continuation_cannot_reset_completion_deadline_and_next_run_recovers(
    monitored_cli, monkeypatch
):
    _, requests, events, _ = monitored_cli
    monkeypatch.setenv("DISCOVERY_MAX_ITEMS", "1")
    assert main(["--require-monitor"]) == 0
    assert events == ["/run-secret/start", "/run-secret"]
    events.clear()
    assert main(["--require-monitor"]) == 0
    assert events == ["/run-secret/start", "/complete-secret", "/run-secret"]
    assert sum(r.url.path == "/discover/google" for r in requests) == 1
    assert sum(r.url.path == "/docs/discovery" for r in requests) == 2


@pytest.mark.parametrize(
    "result", ["registered", "index_failed", "import_rejected", "listing_denied", "incomplete"]
)
def test_completed_traversal_with_gaps_does_not_reset_clean_scan_deadline(monitored_cli, result):
    _, _, events, outcome = monitored_cli
    if result in {"registered", "index_failed"}:
        outcome["import"] = result
    else:
        outcome[result] = True
    assert main(["--require-monitor"]) == 0
    assert events == ["/run-secret/start", "/run-secret"]


def test_api_failure_reports_failure_without_replay_or_sensitive_logs(monitored_cli, capsys):
    _, requests, events, outcome = monitored_cli
    outcome["listing_failure"] = True
    assert main(["--require-monitor"]) == 2
    assert events == ["/run-secret/start", "/run-secret/fail"]
    assert sum(r.url.path == "/discover/google" for r in requests) == 1
    output = capsys.readouterr()
    assert "secret" not in output.out + output.err


@pytest.mark.parametrize("failed", ["/run-secret/start", "/complete-secret", "/run-secret"])
def test_monitor_failure_preserves_work_and_never_replays_it(monitored_cli, failed, capsys):
    _, requests, events, outcome = monitored_cli
    outcome["monitor_failure"] = failed
    assert main(["--require-monitor"]) == 2
    assert sum(r.url.path == "/docs/discovery" for r in requests) == 2
    # A clean scan still resets the completion deadline, but any earlier
    # delivery failure must keep the run check failed.
    assert "/complete-secret" in events
    if failed != "/run-secret":
        assert events[-1] == "/run-secret/fail"
    output = capsys.readouterr()
    assert "secret" not in output.out + output.err
    assert json.loads(output.out.splitlines()[-1])["monitor_failed"] is True


def test_overlap_reports_failure_without_start_or_completion(monitored_cli):
    _, requests, events, _ = monitored_cli
    with checkpoint(Settings()):
        assert main(["--require-monitor"]) == 2
    assert events == ["/run-secret/fail"]
    assert all(r.url.host == "monitor.test" for r in requests)


@pytest.mark.parametrize("missing", ["DISCOVERY_MONITOR_URL", "DISCOVERY_COMPLETION_MONITOR_URL"])
def test_scheduled_run_requires_both_monitors_before_api_work(monitored_cli, monkeypatch, missing):
    _, requests, _, _ = monitored_cli
    monkeypatch.delenv(missing)
    assert main(["--require-monitor"]) == 2
    assert all(r.url.host == "monitor.test" for r in requests)


def test_run_and_completion_must_be_distinct_checks(monitored_cli, monkeypatch):
    _, requests, events, _ = monitored_cli
    monkeypatch.setenv("DISCOVERY_COMPLETION_MONITOR_URL", "https://monitor.test/run-secret/")
    assert main(["--require-monitor"]) == 2
    assert events == ["/run-secret/fail"]
    assert all(r.url.host == "monitor.test" for r in requests)


def test_disabled_and_status_cannot_reset_monitor_deadlines(monitored_cli, monkeypatch):
    _, requests, _, _ = monitored_cli
    monkeypatch.setenv("DISCOVERY_ENABLED", "false")
    assert main(["--require-monitor"]) == 0
    assert main(["--status", "--require-monitor"]) == 0
    assert requests == []


def test_checkpoint_lock_is_held_through_final_monitor_delivery(monitored_cli, monkeypatch):
    original_ping = DiscoveryMonitor.ping

    def ping(self, event):
        with pytest.raises(DiscoveryError, match="checkpoint lock"):
            with checkpoint(Settings()):
                pass
        return original_ping(self, event)

    monkeypatch.setattr(DiscoveryMonitor, "ping", ping)
    assert main(["--require-monitor"]) == 0


def test_railway_only_schedules_monitored_staging_runs():
    config = json.loads((Path(__file__).parents[1] / "railway.discovery.json").read_text())
    assert config["deploy"]["cronSchedule"] is None
    assert "src.discovery" not in config["deploy"]["startCommand"]
    staging = config["environments"]["staging"]["deploy"]
    assert staging["startCommand"] == "python -m src.discovery --require-monitor"
    assert staging["cronSchedule"] == "*/15 * * * *"
    preview = config["environments"]["pr"]["deploy"]
    assert preview["cronSchedule"] is None
    assert "src.discovery" not in preview["startCommand"]


@pytest.mark.parametrize("limit", ["items", "pages", "seconds"])
def test_normal_limits_warn_resume_and_preserve_previous_completion(
    monitored_cli, monkeypatch, capsys, limit
):
    config, requests, events, _ = monitored_cli
    path = Path(config["DISCOVERY_STATE_PATH"])
    previous_completion = "2026-10-24T00:00:00+00:00"
    state = State(
        identity="default",
        started_at="2026-10-25T00:00:00+00:00",
        completed_at=previous_completion,
        last_counts={"indexed": 10},
        frontier=[Listing(folder_id="first"), Listing(folder_id="second")],
    )
    if limit != "pages":
        state.frontier = []
        state.pending = [
            Item(file_id=name, kind="file", source_id="gdocs") for name in ("one", "two")
        ]
    save_state(path, state)
    monkeypatch.setenv(f"DISCOVERY_MAX_{limit.upper()}", "1")
    clock = [0.0]
    if limit == "seconds":
        from src.discovery import DiscoveryClient

        original_import = DiscoveryClient.import_file

        def slow_import(self, item, timeout):
            result = original_import(self, item, timeout)
            clock[0] += 2
            return result

        monkeypatch.setattr("src.discovery.time.monotonic", lambda: clock[0])
        monkeypatch.setattr(DiscoveryClient, "import_file", slow_import)

    assert main(["--require-monitor"]) == 0
    assert events == ["/run-secret/start", "/run-secret"]
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary["level"] == "warning"
    assert (
        summary["outcome"]
        == {"items": "item_budget", "pages": "page_budget", "seconds": "time_budget"}[limit]
    )
    assert summary["scan_status"] == "in_progress"
    persisted = State.model_validate_json(path.read_text())
    assert persisted.completed_at == previous_completion
    assert persisted.last_counts == {"indexed": 10}
    assert persisted.started_at == state.started_at
    assert persisted.pending or persisted.frontier
    assert persisted.counts["indexed"] == (2 if limit == "pages" else 1)
    events.clear()
    assert main(["--require-monitor"]) == 0
    assert events == ["/run-secret/start", "/complete-secret", "/run-secret"]
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary["scan_status"] == "completed"
    assert summary["outcome"] == "completed"
    assert summary["level"] == "info"
    assert sum(r.url.path == "/docs/discovery" for r in requests) == (4 if limit == "pages" else 2)


def test_completed_gaps_are_visible_in_finish_and_checkpoint_status(monitored_cli, capsys):
    _, _, events, outcome = monitored_cli
    outcome["import"] = "registered"
    assert main(["--require-monitor"]) == 0
    assert events == ["/run-secret/start", "/run-secret"]
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary["level"] == "warning"
    assert summary["outcome"] == "completed_with_gaps"
    assert summary["scan_status"] == "completed_with_warnings"
    assert summary["counts"]["registered"] == 2
    assert main(["--status"]) == 0
    assert json.loads(capsys.readouterr().out)["scan_status"] == "completed_with_warnings"


def test_audience_warnings_stay_visible_without_changing_clean_scan_contract(monitored_cli, capsys):
    _, _, events, outcome = monitored_cli
    outcome["warnings"] = 3
    assert main(["--require-monitor"]) == 0
    assert events == ["/run-secret/start", "/complete-secret", "/run-secret"]
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary["level"] == "warning"
    assert summary["scan_status"] == "completed_with_warnings"
    assert summary["outcome"] == "completed"
    assert summary["counts"]["warnings"] == 6


def test_failed_checkpoint_write_cannot_report_clean_completion(monitored_cli, monkeypatch, capsys):
    _, requests, events, _ = monitored_cli
    from src.discovery import save_state as original_save

    def fail_final_write(path, state):
        if state.completed_at:
            raise OSError("secret storage detail")
        original_save(path, state)

    monkeypatch.setattr("src.discovery.save_state", fail_final_write)
    assert main(["--require-monitor"]) == 2
    assert events == ["/run-secret/start", "/run-secret/fail"]
    assert sum(r.url.path == "/docs/discovery" for r in requests) == 2
    output = capsys.readouterr()
    assert "secret" not in output.out + output.err
    summary = json.loads(output.out.splitlines()[-1])
    assert summary["level"] == "error"
    assert summary["outcome"] == "failed"
