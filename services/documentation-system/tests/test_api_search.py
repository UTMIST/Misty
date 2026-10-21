from copy import deepcopy
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from conftest import build_seed_sources
from contracts.directory import DirectoryUnavailable
from contracts.retrieval import EmbeddingError, RetrievalUnavailable
from src.api.app import create_app
from src.api.deps import get_directory, get_embeddings, get_storage
from src.api.hashing import generate_key
from src.storage.in_memory import InMemoryStorageAdapter
from tests.retrieval_cases import FIXTURE, P1, T1, T2, embedding, expected_response, seed


class FakeDirectory:
    def __init__(self):
        self.unavailable = False
        self.calls = 0
        self.channel_teams = frozenset({T1, T2})
        self.person_teams = None

    def _check(self):
        self.calls += 1
        if self.unavailable:
            raise DirectoryUnavailable("offline")

    def get_active_team_ids(self, person_id):
        self._check()
        return (
            self.person_teams
            if self.person_teams is not None
            else frozenset(map(UUID, FIXTURE["people"][str(person_id)]["team_ids"]))
        )

    def get_source_access_team_ids(self, person_id):
        self._check()
        return (
            self.person_teams
            if self.person_teams is not None
            else frozenset(map(UUID, FIXTURE["people"][str(person_id)]["source_team_ids"]))
        )

    def get_channel_source_team_ids(self, guild_id, channel_id):
        self._check()
        return self.channel_teams if channel_id == "200" else frozenset()


class FakeEmbeddings:
    def __init__(self):
        self.error = None
        self.calls = 0

    def embed_query(self, query):
        self.calls += 1
        if self.error:
            raise EmbeddingError(self.error)
        return embedding([0, -1] if query == "unrelated galaxies" else [1, 0])


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    adapter = InMemoryStorageAdapter(seed_sources=build_seed_sources())
    docs = seed(adapter)
    app = create_app()
    directory, embeddings = FakeDirectory(), FakeEmbeddings()
    app.dependency_overrides[get_storage] = lambda: adapter
    app.dependency_overrides[get_directory] = lambda: directory
    app.dependency_overrides[get_embeddings] = lambda: embeddings
    keys = {}
    for name, scopes in {
        **FIXTURE["keys"],
        "both": ["docs:read", "act-as-user", "act-as-channel"],
    }.items():
        token, prefix, key_hash = generate_key()
        adapter.create_api_key(
            name=name, prefix=prefix, key_hash=key_hash, scopes=scopes, actor="test"
        )
        keys[name] = token
    with TestClient(app) as client:
        yield client, adapter, docs, directory, embeddings, keys


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
def test_portable_http_exchanges(ctx, case):
    client, adapter, docs, directory, embeddings, keys = ctx
    request = deepcopy(case["request"])
    request["url"] = request.pop("path")
    headers = request["headers"]
    headers["X-API-Key"] = keys[headers["X-API-Key"]]
    if case["name"] == "scope-unavailable":
        directory.unavailable = True
    if case["name"] == "embedding-error":
        embeddings.error = 502
    response = client.request(**request)
    assert response.status_code == case["response"]["status"]
    assert response.json() == expected_response(case, docs)


def person_headers(keys):
    return {"X-API-Key": keys["person-reader"], "X-On-Behalf-Of": str(P1)}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"query": " "},
        {"query": "a" * 2001},
        {"query": "\x00"},
        {"query": "\ud800"},
        {"query": "CPSIF", "k": 0},
        {"query": "CPSIF", "k": 51},
        {"query": "CPSIF", "k": True},
        {"query": "CPSIF", "k": "2"},
        {"query": "CPSIF", "allowed_team_ids": [str(T1)]},
        {"query": "CPSIF", "person_id": str(P1)},
        {"query": "CPSIF", "model": "text-embedding-3-large"},
    ],
)
def test_invalid_body_precedes_scope_and_embedding_calls(ctx, body):
    client, _, _, directory, embeddings, keys = ctx
    # Encode escaped surrogates manually; httpx's normal encoder rejects them locally.
    import json

    response = client.post(
        "/search",
        content=json.dumps(body),
        headers={
            **person_headers(keys),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 422
    assert directory.calls == embeddings.calls == 0


@pytest.mark.parametrize(
    "headers,status",
    [
        ({}, 401),
        ({"X-API-Key": "invalid"}, 401),
        ({"X-API-Key": "person-reader"}, 400),
        ({"X-API-Key": "channel-reader", "X-Guild-ID": "100"}, 400),
        ({"X-API-Key": "channel-reader", "X-Guild-ID": "100", "X-Channel-ID": "-2"}, 400),
        (
            {
                "X-API-Key": "both",
                "X-On-Behalf-Of": str(P1),
                "X-Guild-ID": "100",
                "X-Channel-ID": "200",
            },
            400,
        ),
        ({"X-API-Key": "person-reader", "X-On-Behalf-Of": "invalid"}, 400),
    ],
)
def test_rejected_context_never_resolves_scope_or_calls_provider(ctx, headers, status):
    client, _, _, directory, embeddings, keys = ctx
    headers = {name: keys.get(value, value) for name, value in headers.items()}
    assert client.post("/search", json={"query": "CPSIF"}, headers=headers).status_code == status
    assert directory.calls == embeddings.calls == 0


def test_channel_changes_and_person_membership_changes_apply_next_request(ctx):
    client, adapter, docs, directory, embeddings, keys = ctx
    headers = {"X-API-Key": keys["channel-reader"], "X-Guild-ID": "100", "X-Channel-ID": "200"}
    first = client.post("/search", json={"query": "CPSIF"}, headers=headers).json()["results"]
    assert {hit["url"] for hit in first} == {docs["team-a"].url, docs["team-b"].url}
    directory.channel_teams = frozenset({T2})
    later = client.post("/search", json={"query": "CPSIF"}, headers=headers).json()["results"]
    assert [hit["url"] for hit in later] == [docs["team-b"].url]
    directory.channel_teams = frozenset()
    assert client.post("/search", json={"query": "CPSIF"}, headers=headers).json() == {
        "results": []
    }
    directory.person_teams = frozenset()
    later = client.post("/search", json={"query": "CPSIF"}, headers=person_headers(keys)).json()[
        "results"
    ]
    assert {hit["url"] for hit in later} == {docs["personal-alice"].url, docs["org"].url}


@pytest.mark.parametrize("status", [502, 503, 504])
def test_embedding_errors_never_fall_back_to_keyword(ctx, status):
    client, _, _, _, embeddings, keys = ctx
    embeddings.error = status
    response = client.post("/search", json={"query": "CPSIF"}, headers=person_headers(keys))
    assert response.status_code == status
    assert response.json() == {"detail": "query embedding unavailable"}


def test_storage_error_is_safe_and_unavailable(ctx, monkeypatch):
    client, adapter, _, _, _, keys = ctx

    def fail(**kwargs):
        raise RetrievalUnavailable("private dependency diagnostics")

    monkeypatch.setattr(adapter, "search_chunks", fail)
    response = client.post("/search", json={"query": "CPSIF"}, headers=person_headers(keys))
    assert response.status_code == 503
    assert response.json() == {"detail": "retrieval storage unavailable"}


def test_source_membership_failure_does_not_return_partial_person_scope(ctx, monkeypatch):
    client, _, _, directory, embeddings, keys = ctx

    def fail(person_id):
        raise DirectoryUnavailable("cannot confirm group membership")

    monkeypatch.setattr(directory, "get_source_access_team_ids", fail)
    response = client.post("/search", json={"query": "CPSIF"}, headers=person_headers(keys))
    assert response.status_code == 503
    assert response.json() == {"detail": "retrieval scope unavailable"}
    assert embeddings.calls == 0


def test_channel_key_cannot_read_private_doc_via_direct_endpoint(ctx):
    client, _, docs, _, _, keys = ctx
    headers = {"X-API-Key": keys["channel-reader"], "X-Guild-ID": "100", "X-Channel-ID": "200"}
    assert client.get(f"/docs/{docs['personal-alice'].id}", headers=headers).status_code == 404


def test_openapi_includes_strict_contract(ctx):
    schema = ctx[0].get("/openapi.json").json()
    assert "/search" in schema["paths"]
    assert schema["components"]["schemas"]["SearchRequest"]["additionalProperties"] is False
