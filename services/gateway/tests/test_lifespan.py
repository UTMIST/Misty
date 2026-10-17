from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi import Depends
from fastapi.testclient import TestClient
import httpx
import pytest

from contracts.directory import DirectoryClient
from src.api.app import create_app
from src.api.deps import get_directory, get_storage
from src.api.hashing import generate_key
from src.directory.http_client import HttpDirectoryClient
from src.storage.in_memory import InMemoryStorageAdapter


@pytest.fixture
def pools(monkeypatch):
    clients = []

    def directory(base_url, api_key):
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
        clients.append(client)
        return HttpDirectoryClient("http://directory.test", "synthetic", client=client)

    monkeypatch.setattr("src.directory.http_client.HttpDirectoryClient", directory)
    yield clients
    for client in clients:
        client.close()


def _app():
    app = create_app()

    @app.get("/pool-probe")
    def probe(directory: DirectoryClient = Depends(get_directory)):
        return {"person": directory.get_person_by_github("missing")}

    return app


def test_directory_pool_is_reused_and_closed_on_each_lifespan(pools):
    app = _app()
    for run in range(2):
        with TestClient(app) as client:
            assert len(pools) == run  # Startup must not construct an unused client.
            for _ in range(2):
                assert client.get("/pool-probe").json() == {"person": None}
            assert len(pools) == run + 1
            assert not pools[-1].is_closed
        assert pools[-1].is_closed


def test_shutting_down_one_app_does_not_close_another_apps_pool(pools):
    with TestClient(_app()) as first:
        assert first.get("/pool-probe").status_code == 200
        with TestClient(_app()) as second:
            assert second.get("/pool-probe").status_code == 200
            assert len(pools) == 2
        assert pools[1].is_closed
        assert not pools[0].is_closed
        assert first.get("/pool-probe").status_code == 200
    assert pools[0].is_closed


def test_concurrent_first_requests_share_one_pool(pools):
    ready = Barrier(4)
    with TestClient(_app()) as client:

        def request(_):
            ready.wait(timeout=5)
            return client.get("/pool-probe").status_code

        with ThreadPoolExecutor(max_workers=4) as workers:
            assert list(workers.map(request, range(4))) == [200] * 4
        assert len(pools) == 1
    assert pools[0].is_closed


def test_rejected_requests_never_construct_a_directory_pool(pools):
    store = InMemoryStorageAdapter()
    token, prefix, key_hash = generate_key()
    store.create_api_key(name="consumer", prefix=prefix, key_hash=key_hash, scopes=[], actor="test")
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: store
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/v1/resolve/discord/missing").status_code == 401
        response = client.get("/v1/resolve/discord/missing", headers={"X-API-Key": token})
        assert response.status_code == 403
    assert pools == []
