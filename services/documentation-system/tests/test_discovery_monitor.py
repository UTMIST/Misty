import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from src.discovery import DiscoveryError, DiscoveryMonitor, Settings, State, checkpoint, main


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
    outcome = {"import": "indexed", "monitor_failure": None, "listing_failure": False}

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
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"file_id": name, "kind": "file", "source_id": "gdocs"}
                        for name in ("one", "two")
                    ]
                },
            )
        assert request.url.path == "/docs/discovery"
        assert request.headers["x-api-key"] == "catalog-secret"
        return httpx.Response(
            200,
            json={
                "status": outcome["import"],
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
    assert main(["--require-monitor"]) == 2
    assert events == ["/run-secret/start", "/run-secret/fail"]
    events.clear()
    assert main(["--require-monitor"]) == 0
    assert events == ["/run-secret/start", "/complete-secret", "/run-secret"]
    assert sum(r.url.path == "/discover/google" for r in requests) == 1
    assert sum(r.url.path == "/docs/discovery" for r in requests) == 2


@pytest.mark.parametrize("result", ["registered", "index_failed"])
def test_completed_traversal_with_gaps_does_not_reset_clean_scan_deadline(monitored_cli, result):
    _, _, events, outcome = monitored_cli
    outcome["import"] = result
    assert main(["--require-monitor"]) == 1
    assert events == ["/run-secret/start", "/run-secret/fail"]


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
    if failed == "/run-secret/start":
        assert "/complete-secret" not in events
        assert events[-1] == "/run-secret/fail"
    elif failed == "/complete-secret":
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
