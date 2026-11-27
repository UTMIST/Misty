import pytest
from fastapi.testclient import TestClient

from conftest import build_seed_role_kinds
from src.api.app import create_app
from src.api.deps import get_storage
from src.storage.in_memory import InMemoryStorageAdapter

AUTH = {"X-API-Key": "test-key"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    # clear the lru_cache on get_settings so the new env value takes effect
    from src.config import get_settings

    get_settings.cache_clear()

    adapter = InMemoryStorageAdapter(seed_role_kinds=build_seed_role_kinds())
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: adapter
    with TestClient(app) as c:
        yield c


def test_create_person_returns_201(client):
    resp = client.post(
        "/people",
        json={"display_name": "Alex", "primary_email": "alex@utmist.ca"},
        headers=AUTH,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["display_name"] == "Alex"
    assert body["primary_email"] == "alex@utmist.ca"
    assert "id" in body


def test_create_person_requires_api_key(client):
    resp = client.post(
        "/people",
        json={"display_name": "Alex", "primary_email": "alex@utmist.ca"},
    )
    assert resp.status_code == 401


def test_get_person(client):
    created = client.post(
        "/people",
        json={"display_name": "Alex", "primary_email": "alex@utmist.ca"},
        headers=AUTH,
    ).json()
    resp = client.get(f"/people/{created['id']}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Alex"


def test_get_person_not_found(client):
    resp = client.get("/people/00000000-0000-0000-0000-000000000000", headers=AUTH)
    assert resp.status_code == 404


def test_list_people(client):
    client.post(
        "/people",
        json={"display_name": "A", "primary_email": "a@utmist.ca"},
        headers=AUTH,
    )
    client.post(
        "/people",
        json={"display_name": "B", "primary_email": "b@utmist.ca"},
        headers=AUTH,
    )
    resp = client.get("/people", headers=AUTH)
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_update_person(client):
    created = client.post(
        "/people",
        json={"display_name": "Alex", "primary_email": "alex@utmist.ca"},
        headers=AUTH,
    ).json()
    resp = client.patch(
        f"/people/{created['id']}",
        json={"display_name": "Alexandra"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Alexandra"


def test_duplicate_email_returns_409(client):
    client.post(
        "/people",
        json={"display_name": "A", "primary_email": "a@utmist.ca"},
        headers=AUTH,
    )
    resp = client.post(
        "/people",
        json={"display_name": "B", "primary_email": "A@utmist.ca"},
        headers=AUTH,
    )
    assert resp.status_code == 409


def test_x_actor_header_used_for_created_by(client):
    created = client.post(
        "/people",
        json={"display_name": "Alex", "primary_email": "alex@utmist.ca"},
        headers={**AUTH, "X-Actor": "discord-bot"},
    ).json()
    assert created["created_by"] == "discord-bot"


def test_people_write_denied_without_scope(client):
    """A DB-issued key with only people:read scope gets 403 on POST /people."""
    from src.api.hashing import generate_key

    adapter = client.app.dependency_overrides[get_storage]()
    plaintext, prefix, key_hash = generate_key()
    adapter.create_api_key(
        name="reader",
        prefix=prefix,
        key_hash=key_hash,
        scopes=["people:read"],
        actor="admin",
    )
    resp = client.post(
        "/people",
        json={"display_name": "Alex", "primary_email": "alex@utmist.ca"},
        headers={"X-API-Key": plaintext},
    )
    assert resp.status_code == 403
    assert "people:write" in resp.json()["detail"]


def test_get_person_by_email(client):
    client.post(
        "/people",
        json={"display_name": "Alex", "primary_email": "alex@utmist.ca"},
        headers=AUTH,
    )
    resp = client.get("/people/by-email/alex@utmist.ca", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Alex"


def test_body_lookup_uses_primary_email_and_requires_auth(client):
    client.post(
        "/people", json={"display_name": "Alex", "primary_email": "alex@example.org"}, headers=AUTH
    )
    lookup = "/people/by-email"
    body = {"email": "Alex@EXAMPLE.ORG"}
    expected = client.get("/people/by-email/alex@example.org", headers=AUTH).json()
    assert client.post(lookup, json=body, headers=AUTH).json() == expected
    assert (
        client.post(lookup, json={"email": "absent@example.org"}, headers=AUTH).status_code == 404
    )
    assert client.post(lookup, json=body).status_code == 401
    assert client.post(lookup, json=body | {"extra": True}, headers=AUTH).status_code == 422


def test_get_person_by_email_not_found(client):
    resp = client.get("/people/by-email/nobody@utmist.ca", headers=AUTH)
    assert resp.status_code == 404


def test_get_person_by_email_is_case_insensitive(client):
    client.post(
        "/people",
        json={"display_name": "Alex", "primary_email": "alex@utmist.ca"},
        headers=AUTH,
    )
    resp = client.get("/people/by-email/Alex@UTMIST.ca", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["primary_email"] == "alex@utmist.ca"


def test_get_person_by_email_requires_api_key(client):
    resp = client.get("/people/by-email/alex@utmist.ca")
    assert resp.status_code == 401


def test_create_person_default_access_level(client):
    resp = client.post(
        "/people",
        json={"display_name": "A", "primary_email": "a@utmist.ca"},
        headers=AUTH,
    )
    assert resp.status_code == 201
    assert resp.json()["access_level"] == "member"


def test_create_person_with_access_level(client):
    resp = client.post(
        "/people",
        json={"display_name": "B", "primary_email": "b@utmist.ca", "access_level": "superuser"},
        headers=AUTH,
    )
    assert resp.status_code == 201
    assert resp.json()["access_level"] == "superuser"


def test_patch_person_access_level(client):
    created = client.post(
        "/people",
        json={"display_name": "C", "primary_email": "c@utmist.ca"},
        headers=AUTH,
    ).json()
    resp = client.patch(
        f"/people/{created['id']}",
        json={"access_level": "admin"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["access_level"] == "admin"


def test_create_person_rejects_unknown_access_level(client):
    resp = client.post(
        "/people",
        json={"display_name": "D", "primary_email": "d@utmist.ca", "access_level": "root"},
        headers=AUTH,
    )
    assert resp.status_code == 422


# --- access_level escalation guard (privilege-escalation fix) ---


def _issue_key(client, scopes):
    """Issue a DB-backed API key with the given scopes; return the plaintext."""
    from src.api.hashing import generate_key

    adapter = client.app.dependency_overrides[get_storage]()
    plaintext, prefix, key_hash = generate_key()
    adapter.create_api_key(
        name="k",
        prefix=prefix,
        key_hash=key_hash,
        scopes=scopes,
        actor="admin",
    )
    return plaintext


def test_patch_access_level_denied_for_write_only_key(client):
    """A people:write-only key cannot change access_level (403), and the value
    is not silently applied."""
    created = client.post(
        "/people",
        json={"display_name": "C", "primary_email": "c@utmist.ca"},
        headers=AUTH,
    ).json()
    key = _issue_key(client, ["people:read", "people:write"])
    resp = client.patch(
        f"/people/{created['id']}",
        json={"access_level": "superuser"},
        headers={"X-API-Key": key},
    )
    assert resp.status_code == 403
    assert "people:elevate" in resp.json()["detail"]
    # Not silently applied: value stays "member".
    after = client.get(f"/people/{created['id']}", headers=AUTH).json()
    assert after["access_level"] == "member"


def test_patch_access_level_self_escalation_denied(client):
    """A write-only key cannot escalate its own linked record to superuser."""
    me = client.post(
        "/people",
        json={"display_name": "Me", "primary_email": "me@utmist.ca"},
        headers=AUTH,
    ).json()
    key = _issue_key(client, ["people:read", "people:write"])
    resp = client.patch(
        f"/people/{me['id']}",
        json={"access_level": "superuser"},
        headers={"X-API-Key": key},
    )
    assert resp.status_code == 403
    after = client.get(f"/people/{me['id']}", headers=AUTH).json()
    assert after["access_level"] == "member"


def test_patch_access_level_allowed_with_elevate_scope(client):
    """A key holding people:elevate may change access_level."""
    created = client.post(
        "/people",
        json={"display_name": "C", "primary_email": "c@utmist.ca"},
        headers=AUTH,
    ).json()
    key = _issue_key(client, ["people:write", "people:elevate"])
    resp = client.patch(
        f"/people/{created['id']}",
        json={"access_level": "admin"},
        headers={"X-API-Key": key},
    )
    assert resp.status_code == 200
    assert resp.json()["access_level"] == "admin"


def test_patch_access_level_allowed_with_admin_key(client):
    """The admin/bootstrap key (wildcard scope) may still promote (seed flow)."""
    created = client.post(
        "/people",
        json={"display_name": "C", "primary_email": "c@utmist.ca"},
        headers=AUTH,
    ).json()
    resp = client.patch(
        f"/people/{created['id']}",
        json={"access_level": "superuser"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["access_level"] == "superuser"


def test_patch_non_access_level_still_works_with_write_scope(client):
    """A plain people:write key can still edit display_name/email."""
    created = client.post(
        "/people",
        json={"display_name": "C", "primary_email": "c@utmist.ca"},
        headers=AUTH,
    ).json()
    key = _issue_key(client, ["people:write"])
    resp = client.patch(
        f"/people/{created['id']}",
        json={"display_name": "Cassandra", "primary_email": "cass@utmist.ca"},
        headers={"X-API-Key": key},
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Cassandra"
    assert resp.json()["primary_email"] == "cass@utmist.ca"


def test_create_privileged_person_denied_for_write_only_key(client):
    """Creating a person with a privileged access_level requires people:elevate."""
    key = _issue_key(client, ["people:write"])
    resp = client.post(
        "/people",
        json={
            "display_name": "E",
            "primary_email": "e@utmist.ca",
            "access_level": "superuser",
        },
        headers={"X-API-Key": key},
    )
    assert resp.status_code == 403
    assert "people:elevate" in resp.json()["detail"]


def test_create_member_person_allowed_for_write_only_key(client):
    """Creating an ordinary (member) person needs only people:write."""
    key = _issue_key(client, ["people:write"])
    resp = client.post(
        "/people",
        json={"display_name": "F", "primary_email": "f@utmist.ca"},
        headers={"X-API-Key": key},
    )
    assert resp.status_code == 201
    assert resp.json()["access_level"] == "member"


def test_create_privileged_person_allowed_with_elevate_scope(client):
    """people:elevate permits creating a privileged person (seed flow)."""
    key = _issue_key(client, ["people:write", "people:elevate"])
    resp = client.post(
        "/people",
        json={
            "display_name": "G",
            "primary_email": "g@utmist.ca",
            "access_level": "superuser",
        },
        headers={"X-API-Key": key},
    )
    assert resp.status_code == 201
    assert resp.json()["access_level"] == "superuser"
