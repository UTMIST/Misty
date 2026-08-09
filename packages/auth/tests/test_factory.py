from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from platform_auth.factory import build_auth
from platform_auth.hashing import generate_key


@dataclass
class _Row:
    id: UUID
    name: str
    scopes: list[str]
    active: bool = True
    revoked_at: datetime | None = None


class _Store:
    def __init__(self):
        self.rows: dict[str, tuple[_Row, str]] = {}  # prefix -> (row, hash)
        self.touched: list[UUID] = []

    def add(self, name, scopes, envelope="tt_"):
        plaintext, prefix, key_hash = generate_key(envelope)
        self.rows[prefix] = (_Row(id=uuid4(), name=name, scopes=scopes), key_hash)
        return plaintext

    def get_api_key_hash(self, prefix):
        r = self.rows.get(prefix)
        return r[1] if r else None

    def get_api_key_by_prefix(self, prefix):
        r = self.rows.get(prefix)
        return r[0] if r else None

    def touch_api_key_last_used(self, api_key_id):
        self.touched.append(api_key_id)


def _app(store, **kw):
    deps = build_auth(lambda: store, envelope="tt_", get_env_key=lambda: "", **kw)
    app = FastAPI()

    @app.get("/read")
    def read(_=Depends(deps.require_scope("people:read"))):
        return {"ok": True}

    @app.get("/whoami")
    def whoami(actor: str = Depends(deps.get_actor)):
        return {"actor": actor}

    return app


def test_valid_key_passes_scope_and_touches_last_used():
    store = _Store()
    key = store.add("bot", ["people:read"])
    client = TestClient(_app(store))
    r = client.get("/read", headers={"X-API-Key": key})
    assert r.status_code == 200
    assert len(store.touched) == 1


def test_missing_scope_403_and_missing_key_401():
    store = _Store()
    key = store.add("bot", ["teams:read"])
    client = TestClient(_app(store))
    assert client.get("/read", headers={"X-API-Key": key}).status_code == 403
    assert client.get("/read").status_code == 401


def test_env_bootstrap_key_grants_admin():
    store = _Store()
    deps = build_auth(lambda: store, envelope="tt_", get_env_key=lambda: "env-secret")
    app = FastAPI()

    @app.get("/read")
    def read(_=Depends(deps.require_scope("people:read"))):
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/read", headers={"X-API-Key": "env-secret"}).status_code == 200


def test_actor_is_key_name():
    store = _Store()
    key = store.add("discord-bot", ["people:read"])
    client = TestClient(_app(store))
    assert client.get("/whoami", headers={"X-API-Key": key}).json() == {"actor": "discord-bot"}


def test_dev_spoof_rejected_in_prod():
    store = _Store()
    key = store.add("spoofer", ["dev:spoof", "people:read"])
    client = TestClient(_app(store, is_prod=lambda: True, enable_dev_spoof=True))
    assert client.get("/read", headers={"X-API-Key": key}).status_code == 403


def test_dev_spoof_reject_log_includes_injected_fields(caplog):
    import json
    import logging

    store = _Store()
    key = store.add("playground", ["dev:spoof", "people:read"])
    deps = build_auth(
        lambda: store,
        envelope="tt_",
        get_env_key=lambda: "",
        is_prod=lambda: True,
        enable_dev_spoof=True,
        dev_spoof_reject_log_fields={"tt_env": "production"},
        audit_logger_name="test.spoof.audit",
    )
    app = FastAPI()

    @app.get("/read")
    def read(_=Depends(deps.require_scope("people:read"))):
        return {"ok": True}

    client = TestClient(app)
    with caplog.at_level(logging.WARNING, logger="test.spoof.audit"):
        r = client.get("/read", headers={"X-API-Key": key})
    assert r.status_code == 403
    warnings = [rec for rec in caplog.records if rec.levelname == "WARNING"]
    assert warnings, "expected a WARNING audit record"
    parsed = json.loads(warnings[0].message)
    assert parsed["event"] == "dev_spoof_key_rejected"
    assert parsed["scope"] == "dev:spoof"
    assert parsed["tt_env"] == "production"
    assert parsed["key_name"] == "playground"


def _app_with_actor(store, **kw):
    from platform_auth.factory import build_auth

    deps = build_auth(lambda: store, envelope="tt_", get_env_key=lambda: "", **kw)
    app = FastAPI()

    @app.get("/actor")
    def actor(a=Depends(deps.get_on_behalf_actor)):
        return {"actor": str(a) if a is not None else None}

    return app


def test_on_behalf_actor_returned_with_act_as_user_scope():
    store = _Store()
    key = store.add("bot", ["act-as-user", "people:read"])
    client = TestClient(_app_with_actor(store))
    pid = "11111111-1111-1111-1111-111111111111"
    r = client.get("/actor", headers={"X-API-Key": key, "X-On-Behalf-Of": pid})
    assert r.status_code == 200
    assert r.json() == {"actor": pid}


def test_on_behalf_actor_absent_returns_none():
    store = _Store()
    key = store.add("bot", ["act-as-user"])
    client = TestClient(_app_with_actor(store))
    r = client.get("/actor", headers={"X-API-Key": key})
    assert r.status_code == 200
    assert r.json() == {"actor": None}


def test_on_behalf_actor_without_scope_is_403():
    store = _Store()
    key = store.add("bot", ["people:read"])  # no act-as-user
    client = TestClient(_app_with_actor(store))
    r = client.get(
        "/actor",
        headers={"X-API-Key": key, "X-On-Behalf-Of": "11111111-1111-1111-1111-111111111111"},
    )
    assert r.status_code == 403


def test_on_behalf_actor_admin_wildcard_does_not_grant_act_as_user():
    store = _Store()
    key = store.add("bot", ["admin"])  # wildcard must NOT bypass literal act-as-user
    client = TestClient(_app_with_actor(store))
    r = client.get(
        "/actor",
        headers={"X-API-Key": key, "X-On-Behalf-Of": "11111111-1111-1111-1111-111111111111"},
    )
    assert r.status_code == 403


def test_on_behalf_actor_malformed_uuid_is_400():
    store = _Store()
    key = store.add("bot", ["act-as-user"])
    client = TestClient(_app_with_actor(store))
    r = client.get("/actor", headers={"X-API-Key": key, "X-On-Behalf-Of": "not-a-uuid"})
    assert r.status_code == 400


def test_on_behalf_actor_rejected_without_scope_is_audit_logged(caplog):
    import json
    import logging

    store = _Store()
    key = store.add("bot", ["people:read"])  # no act-as-user
    client = TestClient(_app_with_actor(store, audit_logger_name="test.obo.audit"))
    with caplog.at_level(logging.WARNING, logger="test.obo.audit"):
        r = client.get(
            "/actor",
            headers={"X-API-Key": key, "X-On-Behalf-Of": "11111111-1111-1111-1111-111111111111"},
        )
    assert r.status_code == 403
    warnings = [rec for rec in caplog.records if rec.levelname == "WARNING"]
    assert warnings, "expected a WARNING audit record"
    parsed = json.loads(warnings[0].message)
    assert parsed["event"] == "on_behalf_of_rejected"
    assert parsed["key_name"] == "bot"


def test_on_behalf_actor_success_is_audit_logged(caplog):
    import json
    import logging

    store = _Store()
    key = store.add("bot", ["act-as-user"])
    client = TestClient(_app_with_actor(store, audit_logger_name="test.obo.audit"))
    pid = "11111111-1111-1111-1111-111111111111"
    with caplog.at_level(logging.INFO, logger="test.obo.audit"):
        r = client.get("/actor", headers={"X-API-Key": key, "X-On-Behalf-Of": pid})
    assert r.status_code == 200
    infos = [rec for rec in caplog.records if rec.levelname == "INFO"]
    assert infos, "expected an INFO audit record"
    parsed = json.loads(infos[0].message)
    assert parsed["event"] == "on_behalf_of_asserted"
    assert parsed["key_name"] == "bot"
    assert parsed["actor"] == pid
