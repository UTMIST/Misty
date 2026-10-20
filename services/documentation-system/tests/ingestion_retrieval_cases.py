"""Ingest → real indexing → authorized HTTP retrieval, shared by both stores (#277).

External HTTP responses are deterministic fakes. Chunks and visibility contexts
are never seeded; indexing functions and search results are never substituted.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import partial
import json
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from src.api import deps
from src.api.app import create_app
from src.api.hashing import generate_key
from src.content import content_hash
from src.config import get_settings
from src.directory.http_client import HttpDirectoryClient
from src.embed.http_client import HttpEmbedder
from src.embedding import HttpEmbeddingClient
from src.fetch import registry
from src.fetch.connectors import ConnectorsFetcher, SOURCE_GONE_DETAIL
from tests.retrieval_cases import FIXTURE, P1, P2, P3, T1, T2, expected_response, vector

BOOTSTRAP_KEY = "integration-bootstrap"
EMAILS = {P1: "alice@example.test", P2: "bob@example.test", P3: "other@example.test"}
GROUPS = {T1: "team-a@example.test", T2: "team-b@example.test"}
LONG = "\n\n".join(
    ["# CPSIF 😀"] + [f"## Section {i}\n" + "Obsolete CPSIF funding notes. " * 40 for i in range(6)]
)


def permission(kind, grantee):
    principal = EMAILS[grantee] if kind == "person" else GROUPS[grantee]
    return {
        "permission_id": principal,
        "principal_type": "user" if kind == "person" else "group",
        "principal": principal,
        "role": "reader",
    }


class Providers:
    """Mutable connector/directory/embed HTTP fixtures, never live providers."""

    def __init__(self):
        self.documents = {}
        self.fetch_errors = {}
        self.calls = []
        self.index_batches = []
        self.index_prefixes = {}
        self.query_calls = 0
        self.fail_index_at = None
        self.query_status = 200
        self.query_timeout = False
        self.index_action = None
        self.query_prefix = None
        self.directory_failure = None
        self.memberships = {
            UUID(person): set(map(UUID, scope["team_ids"]))
            for person, scope in FIXTURE["people"].items()
        }
        self.source_memberships = {
            UUID(person): set(map(UUID, scope["source_team_ids"]))
            for person, scope in FIXTURE["people"].items()
        }
        self.channel_teams = {T1, T2}
        self.healthy_groups = {T1, T2}

    def handle(self, request):
        self.calls.append((request.url.host, request.url.path))
        assert request.headers["X-API-Key"] == f"integration-{request.url.host}-key"
        if request.url.host == "connectors":
            assert request.method == "POST" and request.url.path == "/fetch"
            body = json.loads(request.content)
            assert body["source_id"] == "gdocs"
            url = body["url"]
            if url in self.fetch_errors:
                status, detail = self.fetch_errors[url]
                return httpx.Response(status, json={"detail": detail})
            return httpx.Response(200, json=self.documents[url])
        if request.url.host == "llm":
            return self._embed(request)
        assert request.url.host == "directory" and request.method == "GET"
        path = request.url.path
        if self.directory_failure and self.directory_failure in path:
            return httpx.Response(503, json={"detail": "offline"})
        if path == "/memberships":
            assert request.url.params["active_only"] == "true"
            teams = self.memberships[UUID(request.url.params["person_id"])]
            return httpx.Response(200, json=[{"team_id": str(t)} for t in sorted(teams)])
        if path.endswith("/synced-google-teams"):
            teams = self.source_memberships[UUID(path.split("/")[2])]
            return httpx.Response(200, json=[{"team_id": str(t)} for t in sorted(teams)])
        if path.startswith("/people/by-email/"):
            person = next((p for p, email in EMAILS.items() if path.endswith(email)), None)
            return httpx.Response(200, json={"id": str(person)}) if person else httpx.Response(404)
        if path.startswith("/people/by-identifier/email/"):
            # A verified alternate email exercises the real directory client's fallback.
            if path.endswith("alice-alternate@example.test"):
                return httpx.Response(200, json={"id": str(P1)})
            return httpx.Response(404)
        if path.startswith("/teams/by-synced-google-group/"):
            team = next(
                (
                    t
                    for t, email in GROUPS.items()
                    if path.endswith(email) and t in self.healthy_groups
                ),
                None,
            )
            return httpx.Response(200, json={"id": str(team)}) if team else httpx.Response(404)
        if path.startswith("/channels/"):
            _, _, guild, channel, _ = path.split("/")
            return httpx.Response(
                200,
                json={
                    "guild_id": guild,
                    "channel_id": channel,
                    "team_ids": sorted(map(str, self.channel_teams)) if channel == "200" else [],
                },
            )
        if path == "/teams/google-groups":
            return httpx.Response(
                200,
                json=[
                    {
                        "team_id": str(team),
                        "group_email": email,
                        "group_name": f"groups/{team}",
                        "status": "synced" if team in self.healthy_groups else "failed",
                    }
                    for team, email in GROUPS.items()
                ],
            )
        pytest.fail(f"unexpected directory request: {path}")

    def _embed(self, request):
        assert request.method == "POST" and request.url.path == "/embed"
        body = json.loads(request.content)
        assert body["model"] == "openai-embed-3-small"
        inputs = body["inputs"]
        # All fixture index inputs contain the title/context separator; queries do not.
        is_index = "\n\n" in inputs[0]
        if is_index:
            self.index_batches.append(inputs)
            if self.index_action is not None:
                action, self.index_action = self.index_action, None
                action()
            if len(self.index_batches) == self.fail_index_at:
                return httpx.Response(503)
        else:
            self.query_calls += 1
            if self.query_timeout:
                raise httpx.ReadTimeout("offline timeout", request=request)
            if self.query_status != 200:
                return httpx.Response(self.query_status)
        vectors = []
        for value in inputs:
            if is_index:
                item = next(
                    (d for d in FIXTURE["documents"] if value == f"{d['title']}\n\n{d['text']}"),
                    None,
                )
                prefix = (
                    item["embedding_prefix"]
                    if item
                    else self.index_prefixes.get(value.split("\n\n", 1)[0], [1, 0])
                )
            else:
                prefix = (
                    self.query_prefix
                    or FIXTURE[
                        "empty_query_embedding_prefix"
                        if value == "unrelated galaxies"
                        else "query_embedding_prefix"
                    ]
                )
            vectors.append(vector(prefix))
        return httpx.Response(
            200,
            json={
                "model": FIXTURE["model"],
                "dimensions": FIXTURE["dimensions"],
                "embeddings": [{"index": i, "vector": v} for i, v in enumerate(vectors)],
            },
        )


class Flow:
    def __init__(self, client, adapter, providers, keys):
        self.client, self.store, self.providers, self.keys = client, adapter, providers, keys
        self.writer = {"X-API-Key": keys["writer"]}

    def ingest(self, name="lifecycle", *, text=LONG, grants=None, source_grants=None):
        url = f"https://docs.google.com/document/d/{name}/edit"
        self.providers.documents[url] = {
            "title": f"{name} title",
            "content": text,
            "permissions": source_grants if source_grants is not None else [permission("team", T1)],
        }
        response = self.client.post(
            "/docs", headers=self.writer, json={"url": url, "grants": grants or []}
        )
        assert response.status_code == 201, response.text
        self.last_ingest = response.json()
        return response.json()["doc"]

    def headers(self, context=P1):
        if context == "channel":
            return {
                "X-API-Key": self.keys["channel-reader"],
                "X-Guild-ID": "100",
                "X-Channel-ID": "200",
            }
        return {"X-API-Key": self.keys["person-reader"], "X-On-Behalf-Of": str(context)}

    def search(self, query="CPSIF", *, context=P1, k=50):
        response = self.client.post(
            "/search", headers=self.headers(context), json={"query": query, "k": k}
        )
        assert response.status_code == 200, response.text
        hits = response.json()["results"]
        assert len({(h["doc_id"], h["chunk"]["ordinal"]) for h in hits}) == len(hits)
        return hits

    def ids(self, context=P1):
        return {h["doc_id"] for h in self.search(context=context)}

    def refetch(self, doc, status=200):
        response = self.client.post(f"/docs/{doc['id']}/refetch", headers=self.writer)
        assert response.status_code == status, response.text
        return response

    def retry(self, **body):
        response = self.client.post(
            "/docs/index", headers={"X-API-Key": self.keys["indexer"]}, json=body
        )
        assert response.status_code == 200, response.text
        return response.json()

    def citations(self, hits, doc, text):
        assert hits
        stored = {c.ordinal: c for c in self.store.list_doc_chunks(UUID(doc["id"]))}
        for hit in hits:
            assert (hit["doc_id"], hit["title"], hit["url"], hit["source_id"]) == (
                doc["id"],
                doc["title"],
                doc["url"],
                doc["source_id"],
            )
            citation = hit["chunk"]
            assert citation["source_content_hash"] == content_hash(text)
            assert hit["text"] == text[citation["start_offset"] : citation["end_offset"]]
            chunk = stored[citation["ordinal"]]
            assert chunk.chunk_text == hit["text"]
            assert chunk.source_content_hash == citation["source_content_hash"]
            assert (chunk.start_offset, chunk.end_offset) == (
                citation["start_offset"],
                citation["end_offset"],
            )
        catalog = self.client.get(f"/docs/{doc['id']}", headers=self.headers())
        assert catalog.status_code == 200
        assert catalog.json()["url"] == doc["url"]


class IngestionRetrievalCases:
    @pytest.fixture
    def flow(self, adapter, monkeypatch, fake_embedder):
        # Run the production dependency factories, configuration unwrapping,
        # registry, HTTP clients, indexer and router. Override only storage;
        # inject MockTransport into the external clients at their constructors.
        for name, value in {
            "DOCS_ENV": "local",
            "API_KEY": BOOTSTRAP_KEY,
            "DIRECTORY_BASE_URL": "http://directory",
            "DIRECTORY_API_KEY": "integration-directory-key",
            "CONNECTORS_BASE_URL": "http://connectors",
            "CONNECTORS_API_KEY": "integration-connectors-key",
            "LLM_BASE_URL": "http://llm",
            "LLM_API_KEY": "integration-llm-key",
            "SEARCH_EMBED_MODEL": "openai-embed-3-small",
            "EMBED_MODEL": "openai-embed-3-small",
            "SEARCH_MIN_COSINE": "0.3",
            "EMBED_MAX_REQUEST_CHARS": "4000",
            "SOURCE_ACCESS_REFRESH_AFTER_HOURS": "24",
            "SOURCE_ACCESS_MAX_AGE_HOURS": "48",
        }.items():
            monkeypatch.setenv(name, value)
        # The Postgres adapter fixture has already read DATABASE_URL through Settings.
        get_settings.cache_clear()
        providers = Providers()

        def no_network(*args, **kwargs):
            pytest.fail("integration tests must never use a live HTTP transport")

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
        with httpx.Client(transport=httpx.MockTransport(providers.handle)) as outbound:
            for module, name, implementation in (
                (deps, "HttpDirectoryClient", HttpDirectoryClient),
                (deps, "HttpEmbedder", HttpEmbedder),
                (deps, "HttpEmbeddingClient", HttpEmbeddingClient),
                (registry, "ConnectorsFetcher", ConnectorsFetcher),
            ):
                monkeypatch.setattr(module, name, partial(implementation, client=outbound))
            app = create_app()
            app.dependency_overrides[deps.get_storage] = lambda: adapter
            keys = {}
            for name, scopes in {
                **FIXTURE["keys"],
                "indexer": ["docs:index"],
                "writer": ["docs:write"],
            }.items():
                token, prefix, key_hash = generate_key()
                adapter.create_api_key(
                    name=name, prefix=prefix, key_hash=key_hash, scopes=scopes, actor="integration"
                )
                keys[name] = token
            with TestClient(app) as client:
                yield Flow(client, adapter, providers, keys)

    def test_shared_retrieval_exchanges_after_real_ingest(self, flow):
        docs = {}
        for item in FIXTURE["documents"]:
            source = [
                permission(kind, UUID(grantee))
                for kind, grantee, origin in item["grants"]
                if origin == "google_drive"
            ]
            # A temporary personal source share permits content ingestion for
            # manual-only/hidden examples. Reconcile it away through refetch.
            flow.providers.documents[item["url"]] = {
                "title": item["title"],
                "content": item["text"],
                "permissions": source or [permission("person", P3)],
            }
            response = flow.client.post(
                "/docs",
                headers=flow.writer,
                json={
                    "url": item["url"],
                    "source_id": item["source_id"],
                    "grants": [
                        {"grantee_type": kind, "grantee_id": grantee}
                        for kind, grantee, origin in item["grants"]
                        if origin == "manual"
                    ],
                },
            )
            assert response.status_code == 201, response.text
            assert response.json()["warnings"] == []
            doc = response.json()["doc"]
            docs[item["key"]] = flow.store.get_doc(UUID(doc["id"]))
            assert flow.store.list_doc_chunks(UUID(doc["id"]))
            if not source:
                flow.providers.documents[item["url"]]["permissions"] = []
                flow.refetch(doc)
            if not item["active"]:
                assert (
                    flow.client.patch(
                        f"/docs/{doc['id']}", headers=flow.writer, json={"active": False}
                    ).status_code
                    == 200
                )

        for case in FIXTURE["cases"]:
            request = deepcopy(case["request"])
            headers = request["headers"]
            headers["X-API-Key"] = flow.keys[headers["X-API-Key"]]
            flow.providers.directory_failure = (
                "memberships" if case["name"] == "scope-unavailable" else None
            )
            flow.providers.query_status = 500 if case["name"] == "embedding-error" else 200
            calls = len(flow.providers.calls)
            response = flow.client.request(url=request.pop("path"), **request)
            assert response.status_code == case["response"]["status"], case["name"]
            assert response.json() == expected_response(case, docs), case["name"]
            if case["name"] in {"missing-context", "denied-person", "denied-channel"}:
                assert len(flow.providers.calls) == calls

    def test_ingest_dedup_shortening_and_title_citations(self, flow):
        doc = flow.ingest()
        first = flow.search()
        assert len(first) > 1
        flow.citations(first, doc, LONG)
        assert flow.store.get_doc_content(UUID(doc["id"])) == LONG
        calls = len(flow.providers.index_batches)
        for _ in range(2):
            response = flow.client.post("/docs", headers=flow.writer, json={"url": doc["url"]})
            assert response.status_code == 200 and not response.json()["created"]
            assert flow.retry(doc_ids=[doc["id"]])["unchanged"] == 1
            assert flow.search() == first
        assert len(flow.providers.index_batches) == calls

        replacement = "# Replacement 😀\n\nCPSIF renewed."
        flow.providers.documents[doc["url"]].update(content=replacement, title="Renewed title")
        refreshed = flow.refetch(doc).json()
        latest = flow.search()
        assert len(latest) == 1
        flow.citations(latest, refreshed, replacement)
        assert len(flow.store.list_doc_chunks(UUID(doc["id"]))) == 1
        flow.providers.query_prefix = [
            0,
            -1,
        ]  # keyword-only: obsolete text cannot qualify semantically.
        assert flow.search("obsolete") == []
        flow.providers.query_prefix = None
        renamed = flow.client.patch(
            f"/docs/{doc['id']}", headers=flow.writer, json={"title": "New catalog title"}
        )
        assert renamed.status_code == 200
        flow.citations(flow.search(), renamed.json(), replacement)

    @pytest.mark.parametrize("trigger", ["ingest", "refetch", "scheduled"])
    def test_changed_content_uses_every_refresh_trigger(self, flow, trigger):
        doc = flow.ingest()
        replacement = "CPSIF replacement from the upstream source."
        flow.providers.documents[doc["url"]]["content"] = replacement
        if trigger == "ingest":
            response = flow.client.post("/docs", headers=flow.writer, json={"url": doc["url"]})
            assert response.status_code == 200
        elif trigger == "refetch":
            flow.refetch(doc)
        else:
            old = datetime.now(timezone.utc) - timedelta(days=3)
            flow.store.update_doc(
                UUID(doc["id"]),
                {"source_access_synced_at": old, "source_access_attempted_at": old},
                actor="integration",
            )
            response = flow.client.post("/docs/source-access/refresh-due", headers=flow.writer)
            assert response.status_code == 200
            assert response.json() == {
                "attempted": 1,
                "refreshed": 1,
                "failed": 0,
                "index_failed": 0,
            }
        flow.citations(flow.search(), doc, replacement)

    def test_retirement_and_reactivation(self, flow):
        doc = flow.ingest()
        assert flow.ids() == flow.ids("channel") == {doc["id"]}
        assert (
            flow.client.patch(
                f"/docs/{doc['id']}", headers=flow.writer, json={"active": False}
            ).status_code
            == 200
        )
        assert flow.ids() == flow.ids("channel") == set()
        assert flow.store.list_doc_chunks(UUID(doc["id"])) == []
        assert flow.store.get_doc_content(UUID(doc["id"])) == LONG
        assert (
            flow.client.patch(
                f"/docs/{doc['id']}", headers=flow.writer, json={"active": True}
            ).status_code
            == 200
        )
        flow.citations(flow.search(), doc, LONG)

    @pytest.mark.parametrize(
        "status,detail", [(503, "temporary"), (403, "forbidden"), (404, "Not Found")]
    )
    def test_transient_fetch_failure_retains_search_then_confirmed_gone_clears(
        self, flow, status, detail
    ):
        doc = flow.ingest()
        before = flow.search()
        flow.providers.fetch_errors[doc["url"]] = (status, detail)
        flow.refetch(doc, status=502)
        assert flow.search() == before
        flow.providers.fetch_errors[doc["url"]] = (404, SOURCE_GONE_DETAIL)
        flow.refetch(doc, status=502)
        assert flow.ids() == flow.ids("channel") == set()
        assert flow.store.get_doc_content(UUID(doc["id"])) is None
        assert flow.store.list_doc_chunks(UUID(doc["id"])) == []
        assert flow.retry()["attempted"] == 0
        del flow.providers.fetch_errors[doc["url"]]
        flow.refetch(doc)
        assert flow.search() == before

    def test_acl_reconciliation_manual_grants_and_verified_personal_share(self, flow):
        personal = permission("person", P1)
        personal["principal"] = "alice-alternate@example.test"
        doc = flow.ingest(source_grants=[permission("team", T1), personal])
        assert flow.ids() == flow.ids("channel") == {doc["id"]}
        assert flow.ids(P2) == set()
        # Revoke team share, retain the direct verified personal share.
        flow.providers.documents[doc["url"]]["permissions"] = [personal]
        flow.refetch(doc)
        assert flow.ids() == {doc["id"]}
        assert flow.ids("channel") == set()
        flow.providers.documents[doc["url"]]["permissions"] = [permission("person", P2)]
        flow.refetch(doc)
        assert flow.ids() == set() and flow.ids(P2) == {doc["id"]}
        manual = {"grantee_type": "person", "grantee_id": str(P1)}
        assert (
            flow.client.post(
                f"/docs/{doc['id']}/grants", headers=flow.writer, json=manual
            ).status_code
            == 200
        )
        flow.providers.documents[doc["url"]]["permissions"] = []
        flow.refetch(doc)
        assert flow.ids() == {doc["id"]}
        assert flow.ids(P2) == flow.ids("channel") == set()
        assert (
            flow.client.request(
                "DELETE", f"/docs/{doc['id']}/grants", headers=flow.writer, json=manual
            ).status_code
            == 200
        )
        assert flow.ids() == set()

    def test_membership_group_health_and_channel_union_are_live(self, flow):
        a = flow.ingest("a", text="CPSIF team A")
        b = flow.ingest("b", text="CPSIF team B", source_grants=[permission("team", T2)])
        assert flow.ids() == {a["id"]} and flow.ids(P2) == {b["id"]}
        assert flow.ids("channel") == {a["id"], b["id"]}
        flow.providers.source_memberships[P1] = set()
        assert flow.ids() == set()  # Ordinary team membership cannot stand in for Google sync.
        manual = {"grantee_type": "team", "grantee_id": str(T1)}
        assert (
            flow.client.post(
                f"/docs/{a['id']}/grants", headers=flow.writer, json=manual
            ).status_code
            == 200
        )
        assert flow.ids() == {a["id"]}
        flow.providers.memberships[P1] = set()
        assert flow.ids() == set()
        flow.providers.channel_teams = {T2}
        assert flow.ids("channel") == {b["id"]}
        flow.providers.healthy_groups = {T1}
        assert flow.ids("channel") == set()
        flow.providers.healthy_groups = {T1, T2}
        assert flow.ids("channel") == {b["id"]}

    def test_partial_acl_lookup_failure_retains_complete_snapshot_until_expiry(
        self, flow, monkeypatch
    ):
        from src.api.routers import docs

        doc = flow.ingest()
        flow.providers.documents[doc["url"]]["permissions"] = [
            permission("person", P2),
            permission("team", T2),
        ]
        flow.providers.directory_failure = "by-synced-google-group"
        flow.refetch(doc, status=502)
        assert flow.ids() == flow.ids("channel") == {doc["id"]}
        assert flow.ids(P2) == set()  # The earlier resolved user must not become a partial ACL.
        flow.providers.directory_failure = None
        # Give a reconciled snapshot an elapsed deadline without sleeping or
        # seeding grants. Both stores must enforce that deadline during search.
        with monkeypatch.context() as clock:
            clock.setattr(
                docs,
                "source_grant_expiry",
                lambda *args: datetime.now(timezone.utc) - timedelta(seconds=1),
            )
            flow.refetch(doc)
        assert flow.ids() == flow.ids("channel") == set()
        flow.refetch(doc)
        assert flow.ids(P2) == flow.ids("channel") == {doc["id"]}

    @pytest.mark.parametrize("failure", ["embedding", "storage"])
    def test_failed_replacement_retains_one_version_and_retry_recovers(
        self, flow, monkeypatch, failure
    ):
        doc = flow.ingest()
        before = flow.search()
        chunks = flow.store.list_doc_chunks(UUID(doc["id"]))
        replacement = "# Renewed 😀\n\nCPSIF replacement."
        flow.providers.documents[doc["url"]]["content"] = replacement
        if failure == "embedding":
            flow.providers.fail_index_at = len(flow.providers.index_batches) + 1
        else:
            original = flow.store.replace_doc_chunks_if_current

            def fail(*args, **kwargs):
                raise SQLAlchemyError("private storage diagnostics")

            monkeypatch.setattr(flow.store, "replace_doc_chunks_if_current", fail)
        flow.refetch(doc)
        assert flow.store.get_doc_content(UUID(doc["id"])) == replacement
        assert flow.store.list_doc_chunks(UUID(doc["id"])) == chunks
        assert flow.search() == before
        flow.citations(before, doc, LONG)  # The retained hits explicitly name the OLD version.
        assert UUID(doc["id"]) in flow.store.list_docs_for_indexing()
        if failure == "storage":
            monkeypatch.setattr(flow.store, "replace_doc_chunks_if_current", original)
        result = flow.retry()
        assert result["indexed"] == 1 and result["failed"] == 0
        flow.citations(flow.search(), doc, replacement)
        assert flow.retry()["attempted"] == 0

    def test_initial_embedding_failure_and_later_batch_failure_are_retryable(self, flow):
        # 4,000 characters/request forces a multi-request index. Fail AFTER a
        # successful provider batch, before any chunk set can be committed.
        flow.providers.fail_index_at = 2
        doc = flow.ingest()
        assert any(w.startswith("indexing failed") for w in flow.last_ingest["warnings"])
        assert len(flow.providers.index_batches) == 2
        assert flow.store.list_doc_chunks(UUID(doc["id"])) == []
        assert flow.ids() == flow.ids("channel") == set()
        assert flow.store.get_doc_content(UUID(doc["id"])) == LONG
        assert flow.retry()["indexed"] == 1
        flow.citations(flow.search(), doc, LONG)

    def test_initial_fetch_failure_retains_catalog_and_refetch_recovers(self, flow):
        url = "https://docs.google.com/document/d/lifecycle/edit"
        flow.providers.fetch_errors[url] = (503, "temporary")
        doc = flow.ingest()
        assert any(w.startswith("content fetch failed") for w in flow.last_ingest["warnings"])
        assert flow.store.get_doc(UUID(doc["id"])) is not None
        assert flow.store.get_doc_content(UUID(doc["id"])) is None
        assert flow.store.list_doc_chunks(UUID(doc["id"])) == []
        assert flow.providers.index_batches == []
        assert flow.ids() == flow.ids("channel") == set()
        del flow.providers.fetch_errors[url]
        refreshed = flow.refetch(doc).json()
        flow.citations(flow.search(), refreshed, LONG)

    def test_revoked_access_applies_even_when_new_content_cannot_be_indexed(self, flow):
        doc = flow.ingest()
        old = flow.store.list_doc_chunks(UUID(doc["id"]))
        flow.providers.documents[doc["url"]].update(
            content="CPSIF replacement for Bob", permissions=[permission("person", P2)]
        )
        flow.providers.fail_index_at = len(flow.providers.index_batches) + 1
        flow.refetch(doc)
        assert flow.store.list_doc_chunks(UUID(doc["id"])) == old
        assert flow.ids() == flow.ids("channel") == set()
        assert flow.ids(P2) == {doc["id"]}
        assert flow.retry()["indexed"] == 1
        assert flow.ids() == flow.ids("channel") == set()
        assert {h["text"] for h in flow.search(context=P2)} == {"CPSIF replacement for Bob"}

    def test_revocation_during_embedding_is_checked_before_search(self, flow):
        doc = flow.ingest()
        flow.providers.documents[doc["url"]]["content"] = "CPSIF refreshed content"

        def revoke():
            # A completed reconciliation removes access while the outer request
            # awaits embeddings. Perform it through the real API.
            flow.providers.documents[doc["url"]]["permissions"] = []
            flow.refetch(doc)

        flow.providers.index_action = revoke
        flow.refetch(doc)
        assert flow.ids() == flow.ids("channel") == set()
        assert {c.chunk_text for c in flow.store.list_doc_chunks(UUID(doc["id"]))} == {
            "CPSIF refreshed content"
        }

    def test_newer_content_during_embedding_wins_without_mixed_citations(self, flow):
        doc = flow.ingest()
        flow.providers.documents[doc["url"]]["content"] = LONG + "\nIntermediate version."
        latest = "CPSIF newest source version 😀."

        def refetch_newer():
            flow.providers.documents[doc["url"]]["content"] = latest
            flow.refetch(doc)

        flow.providers.index_action = refetch_newer
        flow.refetch(doc)
        assert flow.store.get_doc_content(UUID(doc["id"])) == latest
        flow.citations(flow.search(), doc, latest)
        assert flow.retry()["attempted"] == 0

    @pytest.mark.parametrize("context", [P1, "channel"])
    @pytest.mark.parametrize("dependency", ["scope", "embedding", "timeout"])
    def test_retrieval_dependency_errors_fail_closed_and_recover(self, flow, context, dependency):
        doc = flow.ingest()
        if dependency == "scope":
            flow.providers.directory_failure = (
                "synced-google-teams" if context == P1 else "google-groups"
            )
            status, detail = 503, "retrieval scope unavailable"
        else:
            flow.providers.query_status = 500
            flow.providers.query_timeout = dependency == "timeout"
            status, detail = (
                (504 if dependency == "timeout" else 502),
                "query embedding unavailable",
            )
        calls = flow.providers.query_calls
        response = flow.client.post(
            "/search", headers=flow.headers(context), json={"query": "CPSIF"}
        )
        assert response.status_code == status
        assert response.json() == {"detail": detail}
        if dependency == "scope":
            assert flow.providers.query_calls == calls
        flow.providers.directory_failure = None
        flow.providers.query_status, flow.providers.query_timeout = 200, False
        assert flow.ids(context) == {doc["id"]}

    @pytest.mark.parametrize("path", ["keyword", "vector", "hybrid"])
    def test_ingested_hidden_chunks_cannot_fill_candidate_limits(self, flow, path):
        flow.providers.index_prefixes["allowed title"] = [0.8, 0.6]
        allowed = flow.ingest("allowed", text="CPSIF permitted funding notes")
        # One disallowed document supplies more than either branch's 50-row cap.
        hidden = flow.ingest(
            "hidden",
            text="\n\n".join(["CPSIF " * 330] * 60),
            source_grants=[permission("person", P2)],
        )
        assert len(flow.store.list_doc_chunks(UUID(hidden["id"]))) > 50
        if path == "keyword":
            flow.providers.query_prefix = [0, -1]
        hits = flow.search("semantic" if path == "vector" else "CPSIF", k=1)
        assert [h["doc_id"] for h in hits] == [allowed["id"]]
