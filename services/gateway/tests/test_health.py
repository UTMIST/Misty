from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from src.api.app import create_app
from src.api.deps import get_storage
from src.storage.in_memory import InMemoryStorageAdapter
from src.storage.postgres import PostgresStorageAdapter


def test_health_ok():
    client = TestClient(create_app())
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readiness_with_healthy_storage():
    app = create_app()
    app.dependency_overrides[get_storage] = InMemoryStorageAdapter
    with TestClient(app) as client:
        assert client.get("/health/ready").json() == {"status": "ok"}


def test_readiness_fails_without_leaking_database_details():
    class UnreachableEngine:
        def connect(self):
            raise OperationalError("connect", {}, Exception("private database details"))

    app = create_app()
    app.dependency_overrides[get_storage] = lambda: PostgresStorageAdapter(UnreachableEngine())
    with TestClient(app) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"detail": "gateway database unavailable"}
        assert client.get("/health").status_code == 200
