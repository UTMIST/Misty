from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from contracts.fetcher import FetchResult, SourcePermission
from src.api.app import create_app
from src.api.deps import get_directory, get_fetchers, get_storage
from src.api.hashing import generate_key
from src.api.routers import docs as docs_router
from src.directory.http_client import HttpDirectoryClient
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


def test_source_refresh_guard_http_contract(ctx):
    from tests.test_api_indexing import _key

    client, store, fetchers, _ = ctx
    writer = _key(store, "docs:write")
    reader = _key(store, "docs:read")
    path = f"/docs/source-access/refresh-runs/{uuid4()}"
    other = f"/docs/source-access/refresh-runs/{uuid4()}"
    for method in [client.post, client.delete]:
        assert method(path).status_code == 401
        assert method(path, headers=reader).status_code == 403
        assert method("/docs/source-access/refresh-runs/invalid", headers=writer).status_code == 422
    assert client.post(path, headers=writer).status_code == 200
    assert client.post(path, headers=writer).status_code == 409
    assert client.post(other, headers=writer).status_code == 409
    assert client.delete(other, headers=writer).status_code == 409
    assert client.delete(path, headers=writer).status_code == 200
    assert client.delete(path, headers=writer).status_code == 409
    assert client.post(other, headers=writer).status_code == 200
    assert client.delete(other, headers=writer).status_code == 200
    assert fetchers.calls == []


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


def test_scheduled_refresh_skips_fetch_disabled_sources_before_applying_limit(ctx):
    client, store, fetchers, obo = ctx
    disabled = client.post(
        "/docs",
        headers=ADMIN,
        json={"url": "https://drive.google.com/file/d/disabled"},
    ).json()["doc"]
    enabled = client.post(
        "/docs",
        headers=ADMIN,
        json={"url": "https://docs.google.com/document/d/enabled/edit"},
    ).json()["doc"]
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    store.update_doc(
        UUID(enabled["id"]),
        {
            "source_access_synced_at": old,
            "source_access_attempted_at": old,
        },
        actor="test",
    )
    fetchers.calls.clear()
    response = client.post("/docs/source-access/refresh-due?limit=1", headers=ADMIN)
    assert response.status_code == 200
    assert response.json() == {"attempted": 1, "refreshed": 1, "failed": 0, "index_failed": 0}
    assert fetchers.calls == [("gdocs", enabled["url"])]
    assert store.get_doc(UUID(disabled["id"])).content_snapshot is None
    assert store.get_doc_content(UUID(disabled["id"])) is None
    assert store.get_doc(UUID(disabled["id"])).source_access_attempted_at is None
    assert client.post("/docs/source-access/refresh-due", headers=ADMIN).json()["attempted"] == 0


def _due_docs(ctx, count):
    client, store, fetchers, _ = ctx
    doc_ids = []
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    for number in range(count):
        response = client.post(
            "/docs",
            headers=ADMIN,
            json={"url": f"https://docs.google.com/document/d/cached-{number}/edit"},
        )
        assert response.status_code == 201
        doc_id = UUID(response.json()["doc"]["id"])
        store.update_doc(
            doc_id,
            {"source_access_synced_at": old, "source_access_attempted_at": old},
            actor="test",
        )
        doc_ids.append(doc_id)
    fetchers.calls.clear()
    return doc_ids


def test_scheduled_refresh_caches_identity_results_only_within_batch(ctx, monkeypatch):
    from collections import Counter

    client, store, fetchers, _ = ctx
    doc_ids = _due_docs(ctx, 10)
    people = {"alice@example.com": P1}
    alternate_people = {"alternate@mail.utoronto.ca": P2}
    groups = {"alice@example.com": T1}
    requests = []

    def directory_request(request):
        path = request.url.path
        requests.append(path)
        for prefix, identities in [
            ("/people/by-email/", people),
            ("/people/by-identifier/email/", alternate_people),
            ("/teams/by-synced-google-group/", groups),
        ]:
            if path.startswith(prefix):
                identity = identities.get(path.removeprefix(prefix))
                return (
                    httpx.Response(200, json={"id": str(identity)})
                    if identity
                    else httpx.Response(404)
                )
        pytest.fail("unexpected directory request")

    def fetch(source_id, url):
        fetchers.calls.append((source_id, url))
        # A different file's empty ACL must still revoke its previous access.
        if "cached-9/" in url:
            return _result(None)
        permissions = [
            SourcePermission(
                permission_id=f"{url}-{kind}-{email}",
                principal_type=kind,
                principal=email.upper(),
                role="reader",
            )
            for kind, email in [
                ("user", "alice@example.com"),
                ("user", "alternate@mail.utoronto.ca"),
                ("user", "unknown@example.com"),
                # Same email, separate user/group identity namespaces.
                ("group", "alice@example.com"),
                ("group", "unmapped@googlegroups.com"),
            ]
        ]
        return FetchResult(title="Private", content="new content", permissions=permissions)

    monkeypatch.setattr(fetchers, "fetch_for", fetch)
    with httpx.Client(transport=httpx.MockTransport(directory_request)) as http:
        directory = HttpDirectoryClient("http://directory.test", "offline-key", client=http)
        client.app.dependency_overrides[get_directory] = lambda: directory
        response = client.post("/docs/source-access/refresh-due?limit=10", headers=ADMIN)
        assert response.status_code == 200
        assert response.json() == {"attempted": 10, "refreshed": 10, "failed": 0, "index_failed": 0}
        assert len(fetchers.calls) == 10
        # Seven HTTP lookups instead of seven per document (63).
        assert len(requests) == 7
        assert set(Counter(requests).values()) == {1}
        for doc_id in doc_ids[:-1]:
            grants = store.list_grants(doc_id)
            assert {grant.grantee_id for grant in grants} == {P1, P2, T1}
            assert all(store.get_doc(doc_id).url in grant.source_permission_id for grant in grants)
        assert store.list_grants(doc_ids[-1]) == []

        # Neither successful nor negative results survive into another batch.
        people.clear()
        people["unknown@example.com"] = P2
        alternate_people.clear()
        groups.clear()
        old = datetime.now(timezone.utc) - timedelta(hours=25)
        for doc_id in doc_ids:
            store.update_doc(
                doc_id,
                {"source_access_synced_at": old, "source_access_attempted_at": old},
                actor="test",
            )
        requests.clear()
        response = client.post("/docs/source-access/refresh-due?limit=10", headers=ADMIN)
        assert response.status_code == 200 and response.json()["refreshed"] == 10
        assert len(requests) == 7
        assert set(Counter(requests).values()) == {1}
        assert len(fetchers.calls) == 20
        for doc_id in doc_ids[:-1]:
            assert [grant.grantee_id for grant in store.list_grants(doc_id)] == [P2]
        assert store.list_grants(doc_ids[-1]) == []


@pytest.mark.parametrize("principal_type", ["user", "group"])
@pytest.mark.parametrize("failure", ["http", "timeout"])
def test_scheduled_refresh_does_not_cache_directory_outages(
    ctx, monkeypatch, principal_type, failure
):
    client, store, fetchers, _ = ctx
    doc_ids = _due_docs(ctx, 2)
    original_grants = {doc_id: store.list_grants(doc_id) for doc_id in doc_ids}
    fetchers.result = FetchResult(
        title="Private",
        content="new content",
        permissions=[
            SourcePermission(
                permission_id="known",
                principal_type="user",
                principal="alice@example.com",
                role="reader",
            ),
            SourcePermission(
                permission_id="flaky",
                principal_type=principal_type,
                principal="flaky@example.com",
                role="reader",
            ),
        ],
    )
    attempts = []

    def directory_request(request):
        if request.url.path.endswith("alice@example.com"):
            return httpx.Response(200, json={"id": str(P1)})
        attempts.append(request.url.path)
        if len(attempts) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("offline timeout")
            return httpx.Response(503)
        return httpx.Response(200, json={"id": str(P2 if principal_type == "user" else T1)})

    with httpx.Client(transport=httpx.MockTransport(directory_request)) as http:
        directory = HttpDirectoryClient("http://directory.test", "offline-key", client=http)
        client.app.dependency_overrides[get_directory] = lambda: directory
        response = client.post("/docs/source-access/refresh-due", headers=ADMIN)
    assert response.status_code == 200
    assert response.json() == {"attempted": 2, "refreshed": 1, "failed": 1, "index_failed": 0}
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    failed_doc = next(
        doc_id for doc_id in doc_ids if store.get_doc_content(doc_id) == "old content"
    )
    assert store.list_grants(failed_doc) == original_grants[failed_doc]
    assert len(fetchers.calls) == 2


def test_hourly_schedule_refreshes_delayed_document_without_hourly_downloads(ctx, monkeypatch):
    client, store, fetchers, obo = ctx
    doc = client.post(
        "/docs",
        headers=ADMIN,
        json={"url": "https://docs.google.com/document/d/delayed/edit"},
    ).json()["doc"]
    job_start = datetime.now(timezone.utc)
    synced_at = job_start + timedelta(minutes=2)
    store.update_doc(
        UUID(doc["id"]),
        {
            "source_access_synced_at": synced_at,
            "source_access_attempted_at": synced_at,
        },
        actor="test",
    )
    clock = [job_start + timedelta(hours=24)]

    class ControlledDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(docs_router, "datetime", ControlledDatetime)
    fetchers.calls.clear()
    # Next day's midnight job is two minutes too early; the next hourly check is due.
    assert client.post("/docs/source-access/refresh-due", headers=ADMIN).json()["attempted"] == 0
    assert fetchers.calls == []
    clock[0] = job_start + timedelta(hours=25)
    assert client.post("/docs/source-access/refresh-due", headers=ADMIN).json() == {
        "attempted": 1,
        "refreshed": 1,
        "failed": 0,
        "index_failed": 0,
    }
    refreshed = store.get_doc(UUID(doc["id"]))
    assert refreshed.source_access_synced_at == clock[0]
    assert clock[0] < synced_at + timedelta(hours=48)
    assert len(fetchers.calls) == 1
    clock[0] = job_start + timedelta(hours=26)
    assert client.post("/docs/source-access/refresh-due", headers=ADMIN).json()["attempted"] == 0
    assert len(fetchers.calls) == 1
