from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from src.api.app import create_app
from src.api.deps import get_storage
from src.api.ratelimit import IP_LIMIT
from src.api.readiness import READINESS_CACHE_SECONDS, ReadinessProbe
from src.storage.in_memory import InMemoryStorageAdapter
from src.storage.postgres import PostgresStorageAdapter


class CountingStore(InMemoryStorageAdapter):
    def __init__(self, ready=True):
        super().__init__()
        self.ready = ready
        self.calls = 0

    def is_ready(self):
        self.calls += 1
        return self.ready


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("src.api.readiness.monotonic", lambda: now[0])
    return now


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


def test_public_readiness_requests_do_not_repeat_database_work_after_ip_limit(clock):
    store = CountingStore()
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: store
    with TestClient(app) as client:
        for _ in range(IP_LIMIT):
            assert client.get("/v1/resolve/discord/example").status_code == 401
        assert client.get("/v1/resolve/discord/example").status_code == 429
        for _ in range(IP_LIMIT + 1):
            assert client.get("/health/ready").status_code == 200
        assert store.calls == 1


@pytest.mark.parametrize("initial_ready", [True, False])
def test_readiness_caches_both_outcomes_and_refreshes_after_expiry(initial_ready, clock):
    store = CountingStore(ready=initial_ready)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: store
    initial_status = 200 if initial_ready else 503
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == initial_status
        store.ready = not initial_ready
        for _ in range(10):
            assert client.get("/health/ready").status_code == initial_status
        assert store.calls == 1
        clock[0] += READINESS_CACHE_SECONDS
        assert client.get("/health/ready").status_code == (503 if initial_ready else 200)
        assert store.calls == 2


def test_readiness_cache_is_not_shared_between_app_instances(clock):
    for ready in (True, False):
        store = CountingStore(ready=ready)
        app = create_app()
        app.dependency_overrides[get_storage] = lambda: store
        with TestClient(app) as client:
            assert client.get("/health/ready").status_code == (200 if ready else 503)
            assert store.calls == 1


@pytest.mark.parametrize("previous_ready", [None, True, False])
def test_concurrent_probes_do_not_wait_or_serve_expired_success(previous_ready, clock, monkeypatch):
    probe = ReadinessProbe()
    store = CountingStore(ready=previous_ready)
    if previous_ready is not None:
        assert probe.is_ready(store) is previous_ready
        clock[0] += READINESS_CACHE_SECONDS
    calls_before = store.calls
    started, release = Event(), Event()

    def slow_failure():
        store.calls += 1
        started.set()
        assert release.wait(timeout=5)
        return False

    monkeypatch.setattr(store, "is_ready", slow_failure)
    with ThreadPoolExecutor(max_workers=2) as pool:
        refresh = pool.submit(probe.is_ready, store)
        try:
            assert started.wait(timeout=5)
            # Simulate a check that takes longer than the cache TTL.
            clock[0] += READINESS_CACHE_SECONDS + 1
            concurrent = pool.submit(lambda: [probe.is_ready(store) for _ in range(100)])
            assert concurrent.result(timeout=2) == [False] * 100
        finally:
            release.set()
        assert refresh.result(timeout=5) is False
    # The failure is cached from completion; these calls must not probe again.
    for _ in range(10):
        assert probe.is_ready(store) is False
    assert store.calls == calls_before + 1
