import asyncio
from pathlib import Path
from types import SimpleNamespace
import json

import pytest
from starlette.datastructures import Headers
from platform_auth import telemetry as t


@pytest.fixture
def events(monkeypatch):
    monkeypatch.setenv("TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("TELEMETRY_ENV", "local")
    received = []
    monkeypatch.setattr(
        t, "_emitter", SimpleNamespace(offer=lambda record: received.append(record))
    )
    return received


def test_concurrent_children_and_detached_link(events):
    async def workflow():
        with t.span("login.confirm", service="gateway") as root:
            await asyncio.sleep(0)
            with t.span("login.check") as child:
                parent = t.trace_headers()["traceparent"]
                assert child.ids["parent_span_id"] == root.ids["span_id"]
            with t.span(
                "login.delivery", trace_id="b" * 32, link=parent, traffic="background"
            ) as detached:
                assert detached.ids["link_span_id"] == child.ids["span_id"]
                assert "parent_span_id" not in detached.ids
        return root.ids["trace_id"]

    async def run():
        return await asyncio.gather(workflow(), workflow())

    assert len(set(asyncio.run(run()))) == 2
    assert not t.trace_headers()
    assert events[-1]["result"] == "success"


@pytest.mark.parametrize(
    "service,authenticated,linked",
    [("meeting", True, True), ("meeting", False, False), ("gateway", True, False)],
)
def test_request_parent_trust(service, authenticated, linked, events):
    parent = "00-" + "1" * 32 + "-" + "2" * 16 + "-01"
    request = SimpleNamespace(
        headers=Headers({"traceparent": parent}),
        state=SimpleNamespace(),
        scope={"route": SimpleNamespace(path="/private-canary")},
    )
    with t.request_span(request, service + ".audit") as ctx:
        request.state.auth_key = authenticated
        with t.span("http.client") as call:
            assert call.ids["parent_span_id"] == ctx.get().ids["span_id"]
        assert (ctx.get().ids["trace_id"] == "1" * 32) is linked
    assert "private-canary" not in json.dumps(events)


def test_measure_and_unsupported_outcomes_are_content_free(events):
    with pytest.raises(RuntimeError):
        with t.span("catalog.freshness", service="documentation-system") as call:
            call.measure("eligible", 3)
            call.measure("content_unknown", -4)
            call.measure("private payload", 2)
            call.outcome("private reason")
            call.mark("private payload")
            raise RuntimeError("private exception")
    assert events[-1]["result"] == "failed" and events[-1]["measures"] == {"eligible": 3}
    assert "private" not in json.dumps(events)
    assert not t.trace_headers()


def test_node_and_python_share_the_contract():
    node = Path(__file__).resolve().parents[3] / "discord-bot/src/telemetryContract.json"
    assert json.loads(node.read_text()) == t.CONTRACT


def test_sink_stall_and_failure_leave_caller_unblocked():
    from threading import Event

    entered, release, recovered = Event(), Event(), Event()
    rows = []

    def write(event):
        rows.append(event)
        if len(rows) == 1:
            entered.set()
            assert release.wait(2)
            raise RuntimeError("fixture sink")
        recovered.set()

    emitter = t.Emitter(write, capacity=1)
    emitter.offer({})
    assert entered.wait(2)
    try:
        emitter.offer({})
        emitter.offer({})
    finally:
        release.set()
    assert recovered.wait(2)
    assert rows[1]["dropped_before"] == 2 and rows[1]["sequence"] == 2
    assert rows[0]["process_started_at"] == rows[1]["process_started_at"]


def test_authenticated_client_parent_and_external_redirect_isolation(events):
    import httpx

    seen = []

    def receive(request):
        seen.append((request.url.host, request.headers.get("traceparent")))
        return httpx.Response(302, headers={"Location": "https://external.invalid/"})

    with httpx.Client(transport=httpx.MockTransport(receive), follow_redirects=True) as http:
        with t.span("catalog.search", service="documentation-system") as parent:
            response = t.internal_request(
                http.get, "https://internal.invalid/private-canary", headers={"X-API-Key": "canary"}
            )
        assert response.status_code == 302
    client = next(
        row for row in events if row["operation"] == "http.client" and row["event_name"] == "end"
    )
    assert client["parent_span_id"] == parent.ids["span_id"]
    assert seen == [
        ("internal.invalid", "00-" + client["trace_id"] + "-" + client["span_id"] + "-01")
    ]
    assert client["result"] == "failed"
    assert "canary" not in json.dumps(events)
