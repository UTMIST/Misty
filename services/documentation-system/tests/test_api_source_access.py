from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from contracts.fetcher import FetchResult, SourcePermission
from src.api.app import create_app
from src.api.deps import get_directory, get_fetchers, get_storage
from src.api.hashing import generate_key
from src.storage.in_memory import InMemoryStorageAdapter
from tests.conftest import build_seed_sources

ADMIN = {"X-API-Key": "test-key"}
P1 = UUID("11111111-1111-1111-1111-111111111111")
P2 = UUID("22222222-2222-2222-2222-222222222222")
T1 = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _result(email, content="old content"):
    return FetchResult(
        title="Private",
        content=content,
        content_snapshot=content,
        permissions=[
            SourcePermission(
                permission_id=email,
                principal_type="user",
                principal=email,
                role="reader",
            )
        ]
        if email
        else [],
    )


class FakeFetchers:
    def __init__(self):
        self.result = _result("alice@example.com")
        self.calls = []

    def fetch_for(self, source_id, url):
        self.calls.append((source_id, url))
        return self.result


class FakeDirectory:
    def get_person_label(self, person_id):
        return "Person"

    def get_team_label(self, team_id):
        return "Team"

    def get_active_team_ids(self, person_id):
        return frozenset({T1})

    def get_source_access_team_ids(self, person_id):
        return frozenset()

    def get_person_id_by_verified_email(self, email):
        return {"alice@example.com": P1, "bob@example.com": P2}.get(email)


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    sources = [
        source.model_copy(update={"content_fetch_enabled": source.id == "gdocs"})
        for source in build_seed_sources()
    ]
    store = InMemoryStorageAdapter(seed_sources=sources)
    fetchers = FakeFetchers()
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_fetchers] = lambda: fetchers
    app.dependency_overrides[get_directory] = lambda: FakeDirectory()
    plaintext, prefix, key_hash = generate_key()
    store.create_api_key(
        name="bot",
        prefix=prefix,
        key_hash=key_hash,
        scopes=["docs:read", "docs:write", "act-as-user"],
        actor="test",
    )
    obo = {"X-API-Key": plaintext, "X-On-Behalf-Of": str(P1)}
    with TestClient(app) as client:
        yield client, store, fetchers, obo


@pytest.mark.parametrize("owner_field", ["owning_person_id", "owning_team_id"])
def test_ingest_ownership_does_not_bypass_another_readers_source_acl(ctx, owner_field):
    client, store, fetchers, obo = ctx
    fetchers.result = _result("bob@example.com")
    response = client.post(
        "/docs",
        headers=ADMIN,
        json={
            "url": "https://docs.google.com/document/d/owned/edit",
            owner_field: str(P1 if owner_field == "owning_person_id" else T1),
        },
    )
    assert response.status_code == 201
    doc_id = response.json()["doc"]["id"]
    assert store.get_doc_content(UUID(doc_id)) == "old content"
    assert client.get(f"/docs/{doc_id}", headers=obo).status_code == 404
    assert client.get("/docs", headers=obo).json() == []


@pytest.mark.parametrize("remaining_email", ["bob@example.com", None])
@pytest.mark.parametrize("owner_field", [None, "owning_person_id", "owning_team_id"])
def test_refetch_rechecks_access_after_successful_source_revocation(
    ctx, remaining_email, owner_field
):
    client, store, fetchers, obo = ctx
    payload = {"url": "https://docs.google.com/document/d/revoked/edit"}
    if owner_field:
        payload[owner_field] = str(P1 if owner_field == "owning_person_id" else T1)
    response = client.post("/docs", headers=ADMIN, json=payload)
    assert response.status_code == 201
    doc_id = response.json()["doc"]["id"]
    assert client.get(f"/docs/{doc_id}", headers=obo).status_code == 200
    fetchers.result = _result(remaining_email, content="new restricted content")
    response = client.post(f"/docs/{doc_id}/refetch", headers=obo)
    assert response.status_code == 404
    assert response.json() == {"detail": "doc not found"}
    assert client.get(f"/docs/{doc_id}", headers=obo).status_code == 404
    grants = store.list_grants(UUID(doc_id))
    assert [grant.grantee_id for grant in grants] == ([P2] if remaining_email else [])
    assert store.get_doc_content(UUID(doc_id)) == (
        "new restricted content" if remaining_email else "old content"
    )
