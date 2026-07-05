import json
import time

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


def test_evicts_expired_entries():
    # Small window + low sweep threshold makes it easy to force a sweep
    # without needing to spray thousands of distinct keys.
    app = FastAPI()

    @app.get("/x")
    def x():
        return {"ok": True}

    # Wrap the app directly with the middleware instance (BaseHTTPMiddleware
    # is itself a valid ASGI app) so we can inspect/seed its internal state.
    mw = RateLimitMiddleware(app, limit=60, window_s=1)
    mw._SWEEP_THRESHOLD = 3

    # Seed several distinct keys with an already-expired window.
    stale_start = time.monotonic() - 10
    for i in range(5):
        mw._hits[f"stale{i}"] = (1, stale_start)
    assert len(mw._hits) == 5

    c = TestClient(mw)
    # A fresh request for a new key pushes len(_hits) >= threshold and
    # triggers the sweep, which should clear all the expired stale entries.
    assert c.get("/x", headers={"X-API-Key": "fresh"}).status_code == 200

    assert all(not k.startswith("stale") for k in mw._hits)
    assert "fresh" in mw._hits
