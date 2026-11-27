import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from traceback import format_exception
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from conftest import build_seed_sources
from contracts.directory import DirectoryUnavailable
from contracts.retrieval import EmbeddingError, RetrievalUnavailable
from src.api.app import create_app
from src.api.deps import get_directory, get_embeddings, get_storage
from src.api.hashing import generate_key
from src.config import get_settings
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


@pytest.mark.parametrize(
    "case_name,outcome",
    [
        ("alice", "results"),
        ("empty-relevance", "no_hits"),
        ("denied-person", "denied"),
        ("scope-unavailable", "directory_unavailable"),
        ("embedding-error", "embedding_error"),
        ("missing-context", "invalid_request"),
        ("unconfigured-channel", "no_channel_teams"),
        ("storage-failure", "storage_unavailable"),
        ("disabled", None),
    ],
)
def test_search_outcome_in_actual_request_audit(ctx, monkeypatch, capsys, case_name, outcome):
    client, adapter, docs, directory, embeddings, keys = ctx
    monkeypatch.setattr(get_settings(), "search_outcome_audit_enabled", outcome is not None)
    name = "alice" if case_name in {"storage-failure", "disabled"} else case_name
    case = next(c for c in FIXTURE["cases"] if c["name"] == name)
    request = deepcopy(case["request"])
    request["url"] = request.pop("path")
    headers = request["headers"]
    headers["X-API-Key"] = keys[headers["X-API-Key"]]
    if case_name == "unconfigured-channel":
        directory.channel_teams = frozenset()
    directory.unavailable = case_name == "scope-unavailable"
    if case_name == "embedding-error":
        embeddings.error = 502
    if case_name == "storage-failure":

        def fail(**kwargs):
            raise RetrievalUnavailable("private dependency canary")

        monkeypatch.setattr(adapter, "search_chunks", fail)
    response = client.request(**request)
    assert response.status_code == (
        503 if case_name == "storage-failure" else case["response"]["status"]
    )
    captured = capsys.readouterr().out
    [audit] = search_audits(captured)
    assert audit.get("search_outcome") == outcome
    assert audit["status"] == response.status_code
    for private in (
        request["json"]["query"],
        headers["X-API-Key"],
        str(P1),
        "private dependency canary",
    ):
        assert private not in json.dumps(audit)


def search_audits(captured):
    return [
        record
        for line in captured.splitlines()
        if line.startswith("{")
        and (record := json.loads(line)).get("path") == "/search"
        and "duration_ms" in record
    ]


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize(
    "case,status,outcome",
    [
        ("missing_key", 401, "denied"),
        ("invalid_encoding", 400, "invalid_request"),
        ("invalid_query", 422, "invalid_request"),
        ("malformed_hits", 500, "internal_error"),
        ("unexpected_failure", 500, "internal_error"),
    ],
)
def test_search_error_outcomes(ctx, monkeypatch, capsys, enabled, case, status, outcome):
    client, adapter, _, directory, embeddings, keys = ctx
    monkeypatch.setattr(get_settings(), "search_outcome_audit_enabled", enabled)
    private = "private-query-canary"
    headers = {} if case == "missing_key" else person_headers(keys)
    payload = json.dumps({"query": private, **({"k": 0} if case == "invalid_query" else {})})
    if case == "invalid_encoding":
        payload = b"\xff"
    if case == "malformed_hits":
        monkeypatch.setattr(adapter, "search_chunks", lambda **kwargs: [{"text": private}])
    if case == "unexpected_failure":

        def fail(**kwargs):
            raise RuntimeError(private)

        monkeypatch.setattr(adapter, "search_chunks", fail)
    with TestClient(client.app, raise_server_exceptions=False) as observer:
        response = observer.post(
            "/search", content=payload, headers={**headers, "Content-Type": "application/json"}
        )
    assert response.status_code == status
    if status < 500:
        assert directory.calls == embeddings.calls == 0
    else:
        assert response.text == "Internal Server Error"
    [audit] = search_audits(capsys.readouterr().out)
    assert audit["status"] == status
    assert audit.get("search_outcome") == (outcome if enabled else None)
    assert private not in json.dumps(audit) and keys["person-reader"] not in json.dumps(audit)


def test_overlapping_searches_keep_their_own_audit_outcome(ctx, monkeypatch, capsys):
    client, _, _, _, embeddings, keys = ctx
    monkeypatch.setattr(get_settings(), "search_outcome_audit_enabled", True)
    barrier = Barrier(2, timeout=5)

    def embed(query):
        barrier.wait()
        if query == "private-temporary-failure":
            raise EmbeddingError(503)
        return embedding([1, 0])

    monkeypatch.setattr(embeddings, "embed_query", embed)

    def search_for(key, query):
        return client.post(
            "/search",
            json={"query": query},
            headers={
                **person_headers(keys),
                "X-API-Key": keys[key],
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as workers:
        ok = workers.submit(search_for, "person-reader", "private-success")
        failed = workers.submit(search_for, "both", "private-temporary-failure")
        assert (ok.result(), failed.result()) == (200, 503)
    rows = search_audits(capsys.readouterr().out)
    assert {(r["key_name"], r["status"], r["search_outcome"]) for r in rows} == {
        ("person-reader", 200, "results"),
        ("both", 503, "embedding_error"),
    }
    assert len(rows) == len({r["request_id"] for r in rows}) == 2
    assert "private-" not in json.dumps(rows)


@pytest.mark.parametrize("malformed_hits", [True, False])
def test_unhandled_search_error_keeps_stack_and_redacts_values(ctx, monkeypatch, malformed_hits):
    client, adapter, _, _, _, keys = ctx
    private = "synthetic-private-excerpt"

    def broken_search(**kwargs):
        if malformed_hits:
            return [{"text": private}]
        raise RuntimeError(private)

    monkeypatch.setattr(adapter, "search_chunks", broken_search)
    with pytest.raises(RuntimeError, match="catalog search failed") as caught:
        client.post("/search", json={"query": "hello"}, headers=person_headers(keys))
    traceback = "".join(format_exception(caught.value))
    assert private not in traceback
    assert "routers/search.py" in traceback
    if not malformed_hits:
        assert "in broken_search" in traceback


@pytest.mark.parametrize(
    "case_name,expected",
    [
        ("alice", "results"),
        ("empty-relevance", "no_hits"),
        ("denied-person", "denied"),
        ("scope-unavailable", "directory_unavailable"),
    ],
)
def test_search_trace_does_not_depend_on_legacy_audit(ctx, monkeypatch, case_name, expected):
    from platform_auth import telemetry

    client, _, _, directory, _, keys = ctx
    events = []
    monkeypatch.setattr(telemetry._emitter, "offer", events.append)
    monkeypatch.setenv("TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("TELEMETRY_ENV", "local")
    monkeypatch.setattr(get_settings(), "search_outcome_audit_enabled", False)
    case = next(c for c in FIXTURE["cases"] if c["name"] == case_name)
    req = deepcopy(case["request"])
    req["url"] = req.pop("path")
    req["headers"]["X-API-Key"] = keys[req["headers"]["X-API-Key"]]
    parent = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"
    req["headers"]["traceparent"] = parent
    directory.unavailable = case_name == "scope-unavailable"
    resp = client.request(**req)
    assert resp.status_code == case["response"]["status"]
    [end] = [e for e in events if e["operation"] == "catalog.search" and e["event_name"] == "end"]
    assert end["result"] == expected
    assert end["trace_id"] == "ab" * 16
    assert req["json"]["query"] not in json.dumps(events)


def test_search_ends_after_framework_serialization(ctx, monkeypatch, capsys):
    from platform_auth import telemetry
    import fastapi.routing

    client, _, _, _, _, keys = ctx
    events = []
    monkeypatch.setenv("TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("TELEMETRY_ENV", "local")
    monkeypatch.setattr(telemetry._emitter, "offer", events.append)
    monkeypatch.setattr(get_settings(), "search_outcome_audit_enabled", True)

    async def serialization_failure(**kwargs):
        raise ValueError("private returned excerpt")

    monkeypatch.setattr(fastapi.routing, "serialize_response", serialization_failure)
    with TestClient(client.app, raise_server_exceptions=False) as http:
        response = http.post("/search", json={"query": "CPSIF"}, headers=person_headers(keys))
    assert response.status_code == 500
    [terminal] = [
        e for e in events if e["operation"] == "catalog.search" and e["event_name"] == "end"
    ]
    assert terminal["result"] == "internal_error"
    assert search_audits(capsys.readouterr().out)[-1]["search_outcome"] == "internal_error"
    assert "private returned excerpt" not in str(events)
