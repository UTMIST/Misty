from fastapi import FastAPI
from fastapi.testclient import TestClient

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
