import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, event, select

from src import cli
from src.api.app import create_app
from src.api.deps import get_directory, get_storage, get_verification
from src.api.hashing import generate_key
from src.config import Settings, get_settings
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


def test_final_attempt_is_atomic_without_advisory_lock(engine, adapter, monkeypatch):
    from login_storage_cases import NOW, PERSON_ID, challenge
    from src.storage.schema import login_challenges

    item = challenge()
    assert adapter.reserve_login(item)
    assert adapter.activate_login(item.id, PERSON_ID, NOW)
    for _ in range(2):
        assert adapter.admit_login_attempt(item.id, item.owner, NOW)

    # Exercise the SQL guard independently of the production advisory lock.
    # Both callers reach their UPDATE before either executes it; a separate
    # eligibility SELECT would therefore let both observe two spent attempts.
    monkeypatch.setattr(adapter, "_login_transaction", engine.begin)
    ready = Barrier(2, timeout=10)

    def before_update(conn, cursor, statement, parameters, context, executemany):
        if context.compiled is not None and context.compiled.statement.is_update:
            ready.wait()

    event.listen(engine, "before_cursor_execute", before_update)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = list(
                pool.map(lambda _: adapter.admit_login_attempt(item.id, item.owner, NOW), range(2))
            )
    finally:
        event.remove(engine, "before_cursor_execute", before_update)

    assert [result.attempts for result in attempts if result is not None] == [3]
    with engine.connect() as conn:
        assert (
            conn.execute(
                select(login_challenges.c.attempts).where(login_challenges.c.id == item.id)
            ).scalar_one()
            == 3
        )


def test_login_quotas_survive_a_new_adapter(engine, adapter):
    from login_storage_cases import challenge

    assert adapter.reserve_login(challenge())
    restarted = PostgresStorageAdapter(engine)
    assert not restarted.reserve_login(challenge())


def test_hourly_client_quota_survives_a_new_adapter(engine, adapter):
    from login_storage_cases import NOW

    for minute in range(4):
        for _ in range(5):
            assert adapter.allow_login_request(
                "compute", "start", "192.0.2.1", NOW + timedelta(minutes=minute)
            )
    restarted = PostgresStorageAdapter(engine)
    at = NOW + timedelta(minutes=5)
    assert not restarted.allow_login_request("compute", "start", "192.0.2.1", at)
    assert restarted.allow_login_request("compute", "start", "192.0.2.99", at)


def test_nul_email_returns_422_without_persisting_login_state(engine, adapter, monkeypatch):
    from src.storage.schema import login_challenges, login_limits

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("GATEWAY_ENV", "local")
    monkeypatch.setenv("EMAIL_LOGIN_ENABLED", "true")
    get_settings.cache_clear()
    token, prefix, key_hash = generate_key()
    adapter.create_api_key(
        name="compute", prefix=prefix, key_hash=key_hash, scopes=["auth:email"], actor="test"
    )

    class UnexpectedUpstream:
        def get_person_by_email(self, email):
            pytest.fail("invalid email must not reach the directory")

        def request_code(self, subject, email):
            pytest.fail("invalid email must not reach verification")

    app = create_app()
    app.dependency_overrides[get_storage] = lambda: adapter
    app.dependency_overrides[get_directory] = UnexpectedUpstream
    app.dependency_overrides[get_verification] = UnexpectedUpstream
    try:
        with TestClient(app, headers={"X-API-Key": token}) as client:
            response = client.post(
                "/v1/auth/email/start",
                json={"email": "member\x00@example.com", "client_ip": "192.0.2.1"},
            )
        assert response.status_code == 422
        assert response.json() == {"detail": "invalid login request"}
        assert response.headers["cache-control"] == "no-store"
        with engine.connect() as conn:
            assert conn.execute(select(login_challenges)).first() is None
            assert conn.execute(select(login_limits)).first() is None
    finally:
        get_settings.cache_clear()


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
