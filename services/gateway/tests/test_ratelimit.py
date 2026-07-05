import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.middleware import AuditLogMiddleware
from src.api.ratelimit import RateLimitMiddleware


def test_limits_per_key():
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, limit=2, window_s=60)

    @app.get("/x")
    def x():
        return {"ok": True}

    c = TestClient(app)
    h = {"X-API-Key": "k1"}
    assert c.get("/x", headers=h).status_code == 200
    assert c.get("/x", headers=h).status_code == 200
    assert c.get("/x", headers=h).status_code == 429
    # a different key is unaffected
    assert c.get("/x", headers={"X-API-Key": "k2"}).status_code == 200


def test_429_is_still_audited(capsys):
    # Mount in the same order as src.api.app.create_app(): RateLimitMiddleware
    # added first (inner), AuditLogMiddleware added last (outer). Audit must
    # be outermost so it observes the 429 short-circuit from the rate limiter.
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, limit=1, window_s=60)
    app.add_middleware(AuditLogMiddleware, logger_name="gateway.audit")

    @app.get("/x")
    def x():
        return {"ok": True}

    c = TestClient(app)
    h = {"X-API-Key": "k1"}
    assert c.get("/x", headers=h).status_code == 200
    assert c.get("/x", headers=h).status_code == 429

    lines = [line for line in capsys.readouterr().out.strip().splitlines() if line.startswith("{")]
    entries = [json.loads(line) for line in lines]
    statuses = [entry.get("status") for entry in entries]

    assert 429 in statuses  # the 429 response WAS audited (audit is outermost)
    assert 200 in statuses
