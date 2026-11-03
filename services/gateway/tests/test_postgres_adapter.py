import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select

from src import cli
from src.api.app import create_app
from src.api.deps import get_directory, get_storage
from src.storage.postgres import PostgresStorageAdapter
from src.storage.schema import api_keys
from storage_cases import StorageCases
from login_storage_cases import LoginStorageCases

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PG_TESTS") != "1", reason="set RUN_PG_TESTS=1 to run Postgres tests"
)


@pytest.fixture(scope="module")
def engine():
    # Explicitly require a disposable test database; never fall back to .env.
    engine = create_engine(os.environ["GATEWAY_TEST_DATABASE_URL"])
    yield engine
    engine.dispose()


@pytest.fixture
def adapter(engine):
    from src.storage.schema import login_challenges, login_limits

    with engine.begin() as conn:
        conn.execute(delete(api_keys))
        conn.execute(delete(login_challenges))
        conn.execute(delete(login_limits))
    return PostgresStorageAdapter(engine)


class TestPostgresStorage(StorageCases):
    pass


class TestPostgresLoginStorage(LoginStorageCases):
    pass


def test_login_quotas_survive_a_new_adapter(engine, adapter):
    from login_storage_cases import challenge

    assert adapter.reserve_login(challenge())
    restarted = PostgresStorageAdapter(engine)
    assert not restarted.reserve_login(challenge())


def test_repeated_revocation_preserves_audit_metadata(adapter, engine):
    key = adapter.create_api_key(
        name="consumer", prefix="prefix", key_hash="HASH", scopes=[], actor="issuer"
    )
    adapter.revoke_api_key(key.id, actor="first")
    with engine.connect() as conn:
        before = conn.execute(select(api_keys).where(api_keys.c.id == key.id)).one()
    adapter.revoke_api_key(key.id, actor="second")
    with engine.connect() as conn:
        after = conn.execute(select(api_keys).where(api_keys.c.id == key.id)).one()
    assert after.revoked_at == before.revoked_at
    assert after.updated_at == before.updated_at
    assert after.updated_by == "first"


def test_cli_key_authentication_and_revocation(adapter, monkeypatch, capsys):
    class Directory:
        def get_person_by_github(self, login):
            return {"id": "test-person"}

        def list_identifiers(self, person_id):
            return [{"provider": "discord", "external_id": "123456789012345678"}]

    monkeypatch.setattr(cli, "_adapter", lambda: adapter)
    assert cli.main(["issue", "--name", "consumer", "--scopes", "resolve:discord"]) == 0
    key = capsys.readouterr().out.strip()
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: adapter
    app.dependency_overrides[get_directory] = Directory
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        response = client.get("/v1/resolve/discord/octocat", headers={"X-API-Key": key})
        assert response.status_code == 200
        assert response.json() == {"discord_id": "123456789012345678"}
        row = adapter.list_api_keys()[0]
        assert row.last_used_at is not None
        assert cli.main(["revoke", str(row.id)]) == 0
        assert (
            client.get("/v1/resolve/discord/octocat", headers={"X-API-Key": key}).status_code == 401
        )
