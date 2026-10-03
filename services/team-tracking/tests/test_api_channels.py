import pytest
from fastapi.testclient import TestClient

from conftest import build_seed_providers, build_seed_role_kinds
from src.api.app import create_app
from src.api.deps import get_storage
from src.api.hashing import generate_key
from src.storage.in_memory import InMemoryStorageAdapter

AUTH = {"X-API-Key": "test-key"}
URL = "/channels/111/222/teams"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    from src.config import get_settings

    get_settings.cache_clear()
    adapter = InMemoryStorageAdapter(
        seed_role_kinds=build_seed_role_kinds(),
        seed_providers=build_seed_providers(),
    )
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: adapter
    with TestClient(app) as c:
        yield c


def _team(client, slug):
    return client.post("/teams", json={"slug": slug, "label": slug}, headers=AUTH).json()["id"]


def _key(client, name, scopes):
    adapter = client.app.dependency_overrides[get_storage]()
    plaintext, prefix, key_hash = generate_key()
    adapter.create_api_key(
        name=name, prefix=prefix, key_hash=key_hash, scopes=scopes, actor="admin"
    )
    return {"X-API-Key": plaintext}


def test_unconfigured_channel_is_empty(client):
    resp = client.get(URL, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {
        "guild_id": "111",
        "channel_id": "222",
        "team_ids": [],
        "updated_at": None,
        "updated_by": None,
    }


def test_replace_multiple_teams_then_read(client):
    a, b = _team(client, "a"), _team(client, "b")
    put = client.put(URL, json={"team_ids": [a, b]}, headers=AUTH)
    assert put.status_code == 200
    assert sorted(put.json()["team_ids"]) == sorted([a, b])

    got = client.get(URL, headers=AUTH).json()
    assert sorted(got["team_ids"]) == sorted([a, b])
    assert got["updated_at"] is not None
    # Other channels are unaffected.
    assert client.get("/channels/111/333/teams", headers=AUTH).json()["team_ids"] == []


def test_replace_overwrites_previous_set(client):
    a, b = _team(client, "a"), _team(client, "b")
    client.put(URL, json={"team_ids": [a]}, headers=AUTH)
    client.put(URL, json={"team_ids": [b]}, headers=AUTH)
    assert client.get(URL, headers=AUTH).json()["team_ids"] == [b]


def test_replace_with_empty_list_clears(client):
    a = _team(client, "a")
    client.put(URL, json={"team_ids": [a]}, headers=AUTH)
    resp = client.put(URL, json={"team_ids": []}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["team_ids"] == []
    assert resp.json()["updated_at"] is None


def test_delete_clears_and_is_idempotent(client):
    a = _team(client, "a")
    client.put(URL, json={"team_ids": [a]}, headers=AUTH)
    assert client.delete(URL, headers=AUTH).status_code == 204
    assert client.get(URL, headers=AUTH).json()["team_ids"] == []
    assert client.delete(URL, headers=AUTH).status_code == 204


def test_retired_team_is_dropped_and_bumps_version(client):
    a, b = _team(client, "a"), _team(client, "b")
    before = client.put(URL, json={"team_ids": [a, b]}, headers=AUTH).json()
    client.patch(f"/teams/{a}", json={"active": False}, headers=AUTH)
    after = client.get(URL, headers=AUTH).json()
    assert after["team_ids"] == [b]
    assert after["updated_at"] > before["updated_at"]


def test_retired_team_rejected_on_put(client):
    a = _team(client, "a")
    client.patch(f"/teams/{a}", json={"active": False}, headers=AUTH)
    resp = client.put(URL, json={"team_ids": [a]}, headers=AUTH)
    assert resp.status_code == 400


def test_unknown_team_rejected_and_config_untouched(client):
    a = _team(client, "a")
    client.put(URL, json={"team_ids": [a]}, headers=AUTH)
    resp = client.put(
        URL, json={"team_ids": [a, "00000000-0000-0000-0000-000000000000"]}, headers=AUTH
    )
    assert resp.status_code == 400
    assert client.get(URL, headers=AUTH).json()["team_ids"] == [a]


def test_duplicate_team_ids_422(client):
    a = _team(client, "a")
    resp = client.put(URL, json={"team_ids": [a, a]}, headers=AUTH)
    assert resp.status_code == 422


def test_extra_field_422(client):
    resp = client.put(URL, json={"team_ids": [], "guild_id": "1"}, headers=AUTH)
    assert resp.status_code == 422


@pytest.mark.parametrize("url", ["/channels/abc/222/teams", "/channels/111/x9/teams"])
def test_non_numeric_ids_422(client, url):
    assert client.get(url, headers=AUTH).status_code == 422
    assert client.put(url, json={"team_ids": []}, headers=AUTH).status_code == 422
    assert client.delete(url, headers=AUTH).status_code == 422


def test_read_only_key_cannot_write(client):
    headers = _key(client, "reader", ["channels:read"])
    assert client.get(URL, headers=headers).status_code == 200
    assert client.put(URL, json={"team_ids": []}, headers=headers).status_code == 403
    assert client.delete(URL, headers=headers).status_code == 403


def test_read_requires_channels_read(client):
    headers = _key(client, "teams-only", ["teams:read"])
    assert client.get(URL, headers=headers).status_code == 403


def test_updated_by_is_the_authenticated_key(client):
    a = _team(client, "a")
    headers = _key(client, "discord-bot", ["channels:read", "channels:write"])
    resp = client.put(URL, json={"team_ids": [a]}, headers={**headers, "X-Actor": "mallory"})
    assert resp.json()["updated_by"] == "discord-bot"
