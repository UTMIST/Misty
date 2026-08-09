from fastapi.testclient import TestClient

from src.api.app import create_app


def test_openapi_served_and_lists_health_and_fetch():
    client = TestClient(create_app())
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert "/health" in paths
    assert "/fetch" in paths
