import asyncio
import json

import httpx
import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from platform_auth import AuditLogMiddleware, telemetry


def test_emits_one_json_line_per_request(capsys):
    app = FastAPI()
    app.add_middleware(AuditLogMiddleware, logger_name="test.audit")

    @app.get("/ping")
    def ping():
        return {"ok": True}

    TestClient(app).get("/ping")
    out = capsys.readouterr().out.strip().splitlines()
    entry = json.loads(out[-1])
    assert entry["method"] == "GET"
    assert entry["path"] == "/ping"
    assert entry["status"] == 200
    assert entry["key_name"] is None


def test_audit_extra_merges_into_line(capsys):
    app = FastAPI()
    app.add_middleware(AuditLogMiddleware, logger_name="test.audit.extra")

    @app.get("/x")
    def x(request: Request):
        request.state.audit_extra = {"model": "claude-sonnet-4-6", "input_tokens": 5}
        return {"ok": True}

    TestClient(app).get("/x")
    entry = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert entry["model"] == "claude-sonnet-4-6"
    assert entry["input_tokens"] == 5
    assert entry["status"] == 200  # core field still present


def test_audit_extra_cannot_override_reserved_keys(capsys):
    app = FastAPI()
    app.add_middleware(AuditLogMiddleware, logger_name="test.audit.reserved")

    @app.get("/y")
    def y(request: Request):
        request.state.audit_extra = {"status": 999, "path": "/spoofed"}
        return {"ok": True}

    TestClient(app).get("/y")
    entry = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert entry["status"] == 200  # real status wins
    assert entry["path"] == "/y"  # real path wins


def test_audit_extra_non_serializable_value_does_not_drop_line(capsys):
    app = FastAPI()
    app.add_middleware(AuditLogMiddleware, logger_name="test.audit.nonserializable")

    @app.get("/z")
    def z(request: Request):
        request.state.audit_extra = {"blob": {1, 2, 3}}
        return {"ok": True}

    TestClient(app).get("/z")
    out = capsys.readouterr().out.strip().splitlines()
    assert out, "audit line was dropped entirely"
    entry = json.loads(out[-1])
    assert entry["status"] == 200
    assert entry["path"] == "/z"
    assert "blob" in entry
    assert entry["blob"] != {1, 2, 3}
    assert isinstance(entry["blob"], str)


def test_audit_extra_non_string_key_does_not_drop_line(capsys):
    # A key json.dumps cannot serialize (a tuple) must not sink the whole line;
    # keys are normalized to str, mirroring the default=str handling of values.
    app = FastAPI()
    app.add_middleware(AuditLogMiddleware, logger_name="test.audit.nonstrkey")

    @app.get("/k")
    def k(request: Request):
        request.state.audit_extra = {("a", "b"): "v"}
        return {"ok": True}

    TestClient(app).get("/k")
    out = capsys.readouterr().out.strip().splitlines()
    assert out, "audit line was dropped entirely"
    entry = json.loads(out[-1])
    assert entry["status"] == 200
    assert entry["path"] == "/k"
    assert entry[str(("a", "b"))] == "v"  # key stringified, value preserved


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize(
    "error_type,status,result",
    [(asyncio.CancelledError, 499, "cancelled"), (RuntimeError, 500, "failed")],
)
def test_interrupted_request_preserves_exception_and_closes_span_once(
    monkeypatch, capsys, enabled, error_type, status, result
):
    monkeypatch.setenv("TELEMETRY_ENABLED", str(enabled).lower())
    monkeypatch.setenv("TELEMETRY_ENV", "local")
    rows = []
    monkeypatch.setattr(telemetry._emitter, "offer", rows.append)
    app = FastAPI()
    app.add_middleware(AuditLogMiddleware, logger_name="gateway.audit")

    async def exercise():
        started = asyncio.Event()

        @app.get("/health")
        async def handler(request: Request):
            request.state.audit_extra = {"outcome": "running"}
            request.state.audit_cancel_extra = {"outcome": "cancelled", "status": 200}
            started.set()
            if error_type is asyncio.CancelledError:
                await asyncio.Future()
            raise error_type("private-cause")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fixture"
        ) as client:
            task = asyncio.create_task(client.get("/health"))
            await started.wait()
            if error_type is asyncio.CancelledError:
                task.cancel()
            with pytest.raises(error_type):
                await task

    asyncio.run(exercise())
    entry = json.loads(capsys.readouterr().out)
    assert entry["status"] == status
    assert entry["outcome"] == ("cancelled" if status == 499 else "running")
    assert [(r["event_name"], r["result"]) for r in rows] == (
        [("start", "started"), ("end", result)] if enabled else []
    )
    assert all(r["operation"] == "http.request" for r in rows)
    assert "private-cause" not in json.dumps([entry, rows])
