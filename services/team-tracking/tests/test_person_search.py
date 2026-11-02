import pytest
from fastapi.testclient import TestClient

from conftest import build_seed_providers
from contracts.types import PersonCreate
from person_search_contract import check_person_search_contract
from src.api.app import create_app
from src.api.deps import get_storage
from src.api.hashing import generate_key
from src.config import get_settings
from src.storage.in_memory import InMemoryStorageAdapter

AUTH = {"X-API-Key": "test-key"}


def test_in_memory_search_contract():
    check_person_search_contract(InMemoryStorageAdapter(seed_providers=build_seed_providers()))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    get_settings.cache_clear()
    adapter = InMemoryStorageAdapter(seed_providers=build_seed_providers())
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: adapter
    with TestClient(app) as client:
        yield client, adapter


def issue_key(adapter, scopes):
    plaintext, prefix, key_hash = generate_key()
    adapter.create_api_key(
        name="search-reader", prefix=prefix, key_hash=key_hash, scopes=scopes, actor="t"
    )
    return {"X-API-Key": plaintext}


def test_search_http_contract_and_legacy_lookup(client):
    client, adapter = client
    person = adapter.create_person(
        PersonCreate(display_name="Alex Chen", primary_email="alex@example.org"), actor="t"
    )
    adapter.add_person_email(person.id, "alternate@example.org", actor="t")
    response = client.post(
        "/people/search", json={"name": "  ALEX ", "email": " ALTERNATE@EXAMPLE.ORG "}, headers=AUTH
    )
    assert response.status_code == 200
    assert response.json() == {
        "matches": [
            {
                "id": str(person.id),
                "display_name": "Alex Chen",
                "primary_email": "alex@example.org",
                "active": True,
            }
        ],
        "has_more": False,
        "needs_disambiguation": False,
    }
    assert client.get("/people", headers=AUTH).json()[0]["id"] == str(person.id)
    assert client.get("/people/by-email/alex@example.org", headers=AUTH).status_code == 200
    assert client.get("/people/by-email/alternate@example.org", headers=AUTH).status_code == 404
    assert client.get("/people/by-identifier/email/alternate@example.org", headers=AUTH).json()[
        "id"
    ] == str(person.id)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": None, "email": None},
        {"name": ""},
        {"name": " \t\n "},
        {"name": "a" * 201},
        {"email": ""},
        {"email": " \t\n "},
        {"email": "a" * 321},
        {"name": "Alex", "email": " "},
        {"name": "\x00"},
        {"email": "a\x00@example.org"},
        {"name": "Alex", "limit": 0},
        {"name": "Alex", "limit": 11},
        {"name": "Alex", "limit": 1.5},
        {"name": "Alex", "limit": True},
        {"name": "Alex", "limit": "2"},
        {"name": "Alex", "created_by": "admin"},
    ],
)
def test_invalid_search_never_calls_storage(client, monkeypatch, payload):
    client, adapter = client

    def unexpected_search(_query):
        pytest.fail("invalid query reached search storage")

    monkeypatch.setattr(adapter, "search_people", unexpected_search)
    assert client.post("/people/search", json=payload, headers=AUTH).status_code == 422


@pytest.mark.parametrize(
    ("scopes", "payload", "expected"),
    [
        (None, {"name": "Alex"}, 401),
        ([], {"name": "Alex"}, 403),
        (["people:write"], {"name": "Alex"}, 403),
        (["identifiers:read"], {"email": "alex@example.org"}, 403),
        (["people:read"], {"email": "alex@example.org"}, 403),
        (["people:read"], {"name": "Alex", "email": "alex@example.org"}, 403),
        (["people:read"], {"name": "Alex"}, 200),
        (["people:read", "identifiers:read"], {"email": "alex@example.org"}, 200),
        (["admin"], {"name": "Alex", "email": "alex@example.org"}, 200),
    ],
)
def test_search_scopes_before_storage(client, monkeypatch, scopes, payload, expected):
    client, adapter = client
    headers = {} if scopes is None else issue_key(adapter, scopes)
    calls = []
    search = adapter.search_people

    def tracked_search(query):
        calls.append(query)
        return search(query)

    monkeypatch.setattr(adapter, "search_people", tracked_search)
    response = client.post("/people/search", json=payload, headers=headers)
    assert response.status_code == expected
    assert len(calls) == (1 if expected == 200 else 0)


def test_search_filters_and_truncation_over_http(client):
    client, adapter = client
    for index in range(3):
        adapter.create_person(
            PersonCreate(display_name="Alex", primary_email=f"alex{index}@example.org"), actor="t"
        )
    response = client.post("/people/search", json={"name": "Alex", "limit": 1}, headers=AUTH)
    assert response.status_code == 200
    assert len(response.json()["matches"]) == 1
    assert response.json()["needs_disambiguation"] is True
    assert response.json()["has_more"] is True
    assert client.post(
        "/people/search", json={"name": "Zoe", "email": "alex0@example.org"}, headers=AUTH
    ).json() == {"matches": [], "has_more": False, "needs_disambiguation": False}
