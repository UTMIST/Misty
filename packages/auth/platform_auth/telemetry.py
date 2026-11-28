"""Content-free operational spans shared by internal services and the log reader."""

import asyncio
import atexit
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from queue import Full, Queue
import re
from secrets import token_hex
import sys
from threading import Lock, Thread
import time

CONTRACT = json.loads(Path(__file__).with_name("telemetry_contract.json").read_text())
ENVIRONMENTS = {"local", "staging", "production"}
TRAFFIC = {"member", "synthetic", "background"}
_context = ContextVar("misty_span", default=None)


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def enabled():
    return (
        os.getenv("TELEMETRY_ENABLED") == "true"
        and os.getenv("TELEMETRY_ENV") in ENVIRONMENTS
        and os.getenv("TELEMETRY_TRAFFIC", "member") in TRAFFIC
    )


def identity(value, width):
    return (
        isinstance(value, str)
        and len(value) == width
        and re.fullmatch("[0-9a-f]+", value) is not None
        and value != "0" * width
    )


def parse_parent(value):
    if not isinstance(value, str) or len(value) != 55:
        return None
    parts = value.split("-")
    if (
        len(parts) == 4
        and parts[0] == "00"
        and identity(parts[1], 32)
        and identity(parts[2], 16)
        and re.fullmatch("[0-9a-f]{2}", parts[3])
    ):
        return parts[1], parts[2]
    return None


def deployment():
    value = os.getenv("RAILWAY_DEPLOYMENT_ID", "")
    return (
        value
        if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{40}", value)
        else "unknown"
    )


def stdout_write(event):
    sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
    sys.stdout.flush()


class Emitter:
    """Only the worker touches stdout. Lost writes are counted on the next receipt."""

    def __init__(self, write=stdout_write, capacity=256):
        self.q = Queue(capacity)
        self.write = write
        self.lock = Lock()
        self.dropped = 0
        self.process_id = token_hex(8)
        self.process_started_at = stamp()
        self.offered = 0
        self.worker = None

    def offer(self, event):
        with self.lock:
            self.offered += 1
            event = dict(event, sequence=self.offered, process_started_at=self.process_started_at)
            if self.worker is None:
                self.worker = Thread(target=self._run, name="misty-telemetry", daemon=True)
                self.worker.start()
            try:
                self.q.put_nowait(event)
            except Full:
                self.dropped += 1

    def _run(self):
        while True:
            event = self.q.get()
            with self.lock:
                lost = self.dropped
                self.dropped = 0
            try:
                self.write(dict(event, dropped_before=lost, process_id=self.process_id))
            except Exception:
                with self.lock:
                    self.dropped += lost + 1
            finally:
                self.q.task_done()


_emitter = Emitter()


def flush(timeout=0.5):
    """Bound normal CLI shutdown even if the sink stalls forever."""
    with _emitter.q.all_tasks_done:
        return _emitter.q.all_tasks_done.wait_for(lambda: _emitter.q.unfinished_tasks == 0, timeout)


atexit.register(flush)


class Span:
    def __init__(
        self,
        service=None,
        operation=None,
        parent=None,
        trace_id=None,
        link=None,
        route=None,
        traffic=None,
        started=None,
        start_ts=None,
    ):
        self.traceparent = None
        self.service = service
        self.operation = operation
        self.result = "success"
        self.measures = {}
        if operation is None:
            return
        trace = parent[0] if parent else trace_id if identity(trace_id, 32) else token_hex(16)
        sid = token_hex(8)
        self.traceparent = f"00-{trace}-{sid}-01"
        self.ids = {"trace_id": trace, "span_id": sid}
        if parent:
            self.ids["parent_span_id"] = parent[1]
        if link:
            self.ids.update(link_trace_id=link[0], link_span_id=link[1])
        self.base = dict(
            self.ids,
            schema="misty.telemetry.v1",
            service=service,
            operation=operation,
            environment=os.environ["TELEMETRY_ENV"],
            traffic=traffic or os.getenv("TELEMETRY_TRAFFIC", "member"),
            deployment=deployment(),
        )
        if route is not None:
            self.base["route"] = route if route in CONTRACT["routes"] else "other"
        self.started = started if started is not None else time.monotonic()
        self._emit("start", "started", ts=start_ts)

    def outcome(self, result):
        if result in CONTRACT["result"]:
            self.result = result

    def measure(self, name, value):
        if (
            name in CONTRACT["measures"].get(self.operation, [])
            and type(value) is int
            and 0 <= value <= 2**53 - 1
        ):
            self.measures[name] = value

    def mark(self, name):
        if name in CONTRACT["marks"].get(self.operation, []):
            self._emit(name, "success")

    def _emit(self, name, result, ts=None):
        if not self.traceparent:
            return
        try:
            event = dict(
                self.base,
                event_id=token_hex(16),
                event_name=name,
                result=result,
                ts=ts or stamp(),
                elapsed_ms=0 if name == "start" else int((time.monotonic() - self.started) * 1000),
            )
            if name == "end" and self.measures:
                event["measures"] = self.measures.copy()
            _emitter.offer(event)
        except Exception:
            # A telemetry failure cannot change application behavior.
            pass


def active_span():
    value = _context.get()
    return value.get() if isinstance(value, RequestSpan) else value


def trace_headers():
    current = active_span()
    if not current or not current.traceparent:
        return {}
    return {"traceparent": current.traceparent, "x-misty-traffic": current.base["traffic"]}


@contextmanager
def span(operation, *, service=None, parent=None, trace_id=None, link=None, traffic=None):
    current = active_span() if enabled() else None
    service = service or (current and current.service) or os.getenv("TELEMETRY_SERVICE")
    if (
        not enabled()
        or service not in CONTRACT["operations"]
        or (traffic is not None and traffic not in TRAFFIC)
        or service == "common"
        or operation not in CONTRACT["operations"][service] + CONTRACT["operations"]["common"]
    ):
        yield Span()
        return
    incoming = (
        parse_parent(parent)
        if parent is not None
        else parse_parent(current.traceparent)
        if current and trace_id is None
        else None
    )
    call = Span(
        service,
        operation,
        incoming,
        trace_id,
        parse_parent(link),
        traffic=traffic or (current and current.base["traffic"]),
    )
    token = _context.set(call)
    try:
        yield call
    except BaseException as exc:
        if call.result == "success":
            call.outcome("cancelled" if isinstance(exc, asyncio.CancelledError) else "failed")
        raise
    finally:
        _context.reset(token)
        call._emit("end", call.result)


class RequestSpan:
    """Resolve the incoming parent only after service auth has attested the caller."""

    def __init__(self, request, service):
        self.request = request
        self.service = service
        self.call = None
        self.started = time.monotonic()
        self.start_ts = stamp()
        self.lock = Lock()

    def get(self):
        with self.lock:
            if self.call is None:
                parents = self.request.headers.getlist("traceparent")
                trusted = self.service != "gateway" and getattr(
                    self.request.state, "auth_key", None
                )
                parent = parse_parent(parents[0]) if trusted and len(parents) == 1 else None
                traffic = None
                labels = self.request.headers.getlist("x-misty-traffic")
                if parent and len(labels) == 1 and labels[0] in TRAFFIC:
                    names = os.getenv("TELEMETRY_TRUSTED_TRAFFIC_KEYS", "").split(",")
                    if not getattr(trusted, "is_bootstrap", True) and getattr(
                        trusted, "name", None
                    ) in {n.strip() for n in names if n.strip()}:
                        traffic = labels[0]
                route = getattr(self.request.scope.get("route"), "path", "other")
                self.call = Span(
                    self.service,
                    "http.request",
                    parent,
                    route=route,
                    traffic=traffic,
                    started=self.started,
                    start_ts=self.start_ts,
                )
            return self.call


@contextmanager
def request_span(request, logger_name):
    service = logger_name.split(".")[0].replace("_", "-")
    if not enabled() or service not in CONTRACT["operations"] or service == "common":
        yield None
        return
    ctx = RequestSpan(request, service)
    token = _context.set(ctx)
    try:
        yield ctx
    finally:
        _context.reset(token)


def internal_request(send, *args, headers, **kwargs):
    """Wrap one configured, authenticated internal HTTP call, including its response."""
    with span("http.client") as call:
        if not call.traceparent:
            return send(*args, headers=headers, **kwargs)
        response = send(
            *args, headers={**headers, **trace_headers()}, **{**kwargs, "follow_redirects": False}
        )
        code = response.status_code
        if code in (401, 403):
            call.outcome("denied")
        elif code == 429:
            call.outcome("rate_limited")
        elif code >= 300:
            call.outcome("failed")
        return response
