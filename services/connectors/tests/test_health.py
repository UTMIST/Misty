from fastapi.testclient import TestClient

from src.api.app import create_app


def test_health_ok():
    with TestClient(create_app()) as c:
        resp = c.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
