from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from src.api.app import create_app
from src.api.deps import get_storage
from src.storage.in_memory import InMemoryStorageAdapter
from src.storage.postgres import PostgresStorageAdapter


class HealthyConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def execute(self, statement):
        assert str(statement) == "SELECT 1"


class HealthyEngine:
    def connect(self):
        return HealthyConnection()


class UnreachableEngine:
    def connect(self):
        raise OperationalError("connect", {}, Exception("database unavailable"))


def test_readiness_succeeds_when_database_responds():
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: PostgresStorageAdapter(HealthyEngine())
    with TestClient(app) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_fails_but_liveness_survives_database_outage():
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: PostgresStorageAdapter(UnreachableEngine())
    with TestClient(app) as client:
        readiness = client.get("/health/ready")
        liveness = client.get("/health")
    assert readiness.status_code == 503
    assert readiness.json() == {"detail": "documentation-system database unavailable"}
    assert liveness.status_code == 200
    assert liveness.json() == {"status": "ok"}


def test_in_memory_adapter_reports_ready():
    assert InMemoryStorageAdapter().is_ready() is True
