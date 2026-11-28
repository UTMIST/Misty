from uuid import UUID

from fastapi import Depends
from fastapi.testclient import TestClient
import httpx
import pytest
from pydantic import SecretStr

from contracts.documents import DocumentsClient, DocumentsUnavailable
from fake_documents import FakeDocumentsClient
from src.api.app import create_app
from src.api.deps import get_documents
from src.config import DEFAULT_DEV_API_KEY, Settings, get_settings, verify_production_secrets
from src.documents.http_client import HttpDocumentsClient

PERSON = UUID("11111111-1111-1111-1111-111111111111")
OTHER = UUID("22222222-2222-2222-2222-222222222222")
DOC = UUID("00000000-0000-0000-0000-000000000001")

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def mcp_app(monkeypatch):
    def build(enabled: bool):
        monkeypatch.setenv("GATEWAY_ENV", "local")
        monkeypatch.setenv("MCP_ENABLED", "true" if enabled else "false")
        get_settings.cache_clear()
        return create_app()

    yield build
    get_settings.cache_clear()


# --- /mcp mount --------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_mcp_is_absent_when_switched_off(mcp_app, method):
    with TestClient(mcp_app(False)) as client:
        assert client.request(method, "/mcp", headers=MCP_HEADERS).status_code == 404


def test_mcp_is_off_by_default():
    assert Settings(_env_file=None).mcp_enabled is False


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer anything"}, {"Authorization": "Basic Zm9vOmJhcg=="}],
    ids=["no-token", "bearer", "not-bearer"],
)
def test_mcp_rejects_every_request_until_sign_in_exists(mcp_app, headers):
    with TestClient(mcp_app(True)) as client:
        for method in ("GET", "POST", "DELETE"):
            response = client.request(
                method, "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, **headers}
            )
            assert response.status_code == 401
            assert response.headers["www-authenticate"].startswith("Bearer ")


def test_mcp_transport_serves_once_a_token_verifies(mcp_app, monkeypatch):
    # No token can verify until #440. Stand one in to prove the mounted SDK
    # transport actually answers the MCP handshake, and lists no tools yet.
    from mcp.server.auth.provider import AccessToken

    async def accept(self, token):
        if token == "synthetic-good":
            return AccessToken(token=token, client_id="test", scopes=[])
        return None

    monkeypatch.setattr("src.mcp_server.endpoint._NoTokensYet.verify_token", accept)
    headers = {**MCP_HEADERS, "Authorization": "Bearer synthetic-good"}
    with TestClient(mcp_app(True)) as client:
        response = client.post("/mcp", json=INITIALIZE, headers=headers)
        assert response.status_code == 200
        assert response.json()["result"]["serverInfo"]["name"] == "misty"
        tools = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers={**headers, "MCP-Protocol-Version": "2025-11-25"},
        )
        assert tools.status_code == 200
        assert tools.json()["result"]["tools"] == []
        bad = client.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": "Bearer nope"}
        )
        assert bad.status_code == 401


def test_mcp_refuses_the_get_stream_even_with_a_token(mcp_app, monkeypatch):
    # The SDK would hold an idle SSE stream open forever; stateless /mcp never
    # pushes anything, so GET is a 405 rather than a dangling connection.
    from mcp.server.auth.provider import AccessToken

    async def accept(self, token):
        return AccessToken(token=token, client_id="test", scopes=[])

    monkeypatch.setattr("src.mcp_server.endpoint._NoTokensYet.verify_token", accept)
    headers = {"Accept": "text/event-stream", "Authorization": "Bearer synthetic-good"}
    with TestClient(mcp_app(True)) as client:
        response = client.get("/mcp", headers=headers)
        assert response.status_code == 405
        assert response.headers["allow"] == "POST"
        assert response.headers["content-type"].startswith("application/json")


def test_mcp_survives_repeated_lifespans(mcp_app):
    # The SDK's session manager runs once per instance; each lifespan builds a new one.
    app = mcp_app(True)
    for _ in range(2):
        with TestClient(app) as client:
            assert client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS).status_code == 401


def test_mcp_leaves_v1_routes_and_openapi_alone(mcp_app):
    off, on = mcp_app(False), mcp_app(True)
    assert on.openapi()["paths"] == off.openapi()["paths"]
    with TestClient(on) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/v1/resolve/discord/missing").status_code == 401


# --- startup check -----------------------------------------------------------


def _prod(**overrides) -> Settings:
    base = {"gateway_env": "production", "directory_api_key": SecretStr("a-real-issued-key")}
    return Settings(_env_file=None, **{**base, **overrides})


@pytest.mark.parametrize("value", ["", "   ", DEFAULT_DEV_API_KEY])
def test_mcp_on_refuses_blank_or_dev_doc_key(value):
    with pytest.raises(RuntimeError, match="DOC_API_KEY"):
        verify_production_secrets(_prod(mcp_enabled=True, doc_api_key=SecretStr(value)))


def test_mcp_on_refuses_blank_doc_base_url():
    with pytest.raises(RuntimeError, match="DOC_BASE_URL"):
        verify_production_secrets(
            _prod(mcp_enabled=True, doc_api_key=SecretStr("doc_real"), doc_base_url=" ")
        )


def test_mcp_off_ignores_doc_key():
    verify_production_secrets(_prod(mcp_enabled=False))


def test_mcp_on_accepts_a_real_doc_key():
    verify_production_secrets(_prod(mcp_enabled=True, doc_api_key=SecretStr("doc_real")))


def test_local_tolerates_dev_doc_key():
    verify_production_secrets(_prod(gateway_env="local", mcp_enabled=True))


def test_doc_api_key_is_never_printed():
    settings = _prod(doc_api_key=SecretStr("doc_secret_value"))
    assert "doc_secret_value" not in repr(settings)


# --- documentation-system HTTP client ---------------------------------------


def _client(handler):
    seen = []

    def record(request):
        seen.append(request)
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(record))
    return HttpDocumentsClient("http://docs.test/", "doc_synthetic", client=http), seen


def test_search_asserts_the_person_and_sends_only_query_and_k():
    docs, seen = _client(
        lambda r: httpx.Response(200, json={"results": [{"doc_id": str(DOC), "text": "x"}]})
    )
    assert docs.search(PERSON, "CPSIF", k=2) == [{"doc_id": str(DOC), "text": "x"}]
    (request,) = seen
    assert (request.method, str(request.url)) == ("POST", "http://docs.test/search")
    assert request.headers["X-API-Key"] == "doc_synthetic"
    assert request.headers["X-On-Behalf-Of"] == str(PERSON)
    assert request.read() == b'{"query":"CPSIF","k":2}'


def test_get_doc_asserts_the_person():
    docs, seen = _client(lambda r: httpx.Response(200, json={"id": str(DOC), "title": "t"}))
    assert docs.get_doc(PERSON, DOC) == {"id": str(DOC), "title": "t"}
    (request,) = seen
    assert (request.method, str(request.url)) == ("GET", f"http://docs.test/docs/{DOC}")
    assert request.headers["X-On-Behalf-Of"] == str(PERSON)


def test_get_doc_not_found_or_not_visible_is_none():
    docs, _ = _client(lambda r: httpx.Response(404, json={"detail": "doc not found"}))
    assert docs.get_doc(PERSON, DOC) is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, text="Application not found"),
        httpx.Response(404, json={"detail": "Not Found"}),
        httpx.Response(404),
    ],
    ids=["proxy-page", "unknown-route", "empty"],
)
def test_a_404_from_anything_else_is_unavailable_not_invisible(response):
    # A wrong DOC_BASE_URL must fail loudly, not quietly hide every doc.
    docs, _ = _client(lambda r: response)
    with pytest.raises(DocumentsUnavailable):
        docs.get_doc(PERSON, DOC)


@pytest.mark.parametrize(
    "query, k",
    [
        ("", 10),
        ("   ", 10),
        ("a" * 2001, 10),
        ("a\x00b", 10),
        ("q", 0),
        ("q", 51),
        ("q", True),
        ("q", 2.0),
    ],
    ids=["empty", "blank", "too-long", "nul", "k-zero", "k-too-big", "k-bool", "k-float"],
)
def test_search_input_documentation_system_would_reject_never_leaves(query, k):
    # A 422 upstream would surface as "unavailable"; bad input is the caller's error.
    docs, seen = _client(lambda r: httpx.Response(200, json={"results": []}))
    with pytest.raises(ValueError):
        docs.search(PERSON, query, k=k)
    assert seen == []
    with pytest.raises(ValueError):
        FakeDocumentsClient().search(PERSON, query, k=k)


def test_search_accepts_the_limits():
    docs, seen = _client(lambda r: httpx.Response(200, json={"results": []}))
    assert docs.search(PERSON, "a" * 2000, k=50) == []
    assert docs.search(PERSON, "q", k=1) == []
    assert len(seen) == 2


def test_client_timeout_is_about_fifteen_seconds():
    docs = HttpDocumentsClient("http://docs.test", "doc_synthetic")
    try:
        assert docs._client.timeout == httpx.Timeout(15.0)
    finally:
        docs.close()


def test_malformed_person_never_reaches_the_header():
    docs, seen = _client(lambda r: httpx.Response(200, json={"results": []}))
    with pytest.raises(ValueError):
        docs.search("not-a-uuid\r\nX-Other: 1", "q")
    assert seen == []


@pytest.mark.parametrize("status", [400, 401, 403, 422, 500, 502, 503, 504])
def test_upstream_errors_are_unavailable(status):
    docs, _ = _client(lambda r: httpx.Response(status, json={"detail": "upstream detail"}))
    with pytest.raises(DocumentsUnavailable) as search_error:
        docs.search(PERSON, "q")
    assert "upstream detail" not in str(search_error.value)
    with pytest.raises(DocumentsUnavailable):
        docs.get_doc(PERSON, DOC)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"null",
        b"[]",
        b'{"results": {}}',
        b'{"results": [{"text": "no id"}]}',
        b'{"results": [{"doc_id": "not-a-uuid"}]}',
        b'{"results": [{"doc_id": 1}]}',
    ],
)
def test_malformed_search_response_is_unavailable(body):
    docs, _ = _client(lambda r: httpx.Response(200, content=body))
    with pytest.raises(DocumentsUnavailable):
        docs.search(PERSON, "q")


@pytest.mark.parametrize("body", [b"not json", b"null", b"[]", b'{"title": "no id"}'])
def test_malformed_doc_response_is_unavailable(body):
    docs, _ = _client(lambda r: httpx.Response(200, content=body))
    with pytest.raises(DocumentsUnavailable):
        docs.get_doc(PERSON, DOC)


def test_transport_failure_hides_the_url():
    def fail(request):
        raise httpx.ConnectError("connect to http://docs.test/search failed")

    docs, _ = _client(fail)
    with pytest.raises(DocumentsUnavailable) as error:
        docs.search(PERSON, "q")
    assert "docs.test" not in str(error.value)
    assert error.value.__cause__ is None


# --- lifespan and fake --------------------------------------------------------


def test_documents_pool_is_lazy_shared_and_closed_per_lifespan(monkeypatch, mcp_app):
    built = []

    def factory(base_url, api_key):
        fake = FakeDocumentsClient()
        built.append(fake)
        return fake

    monkeypatch.setattr("src.documents.http_client.HttpDocumentsClient", factory)
    app = mcp_app(False)

    @app.get("/documents-probe")
    def probe(documents: DocumentsClient = Depends(get_documents)):
        return {"results": documents.search(PERSON, "q")}

    for run in range(2):
        with TestClient(app) as client:
            assert len(built) == run  # Startup must not construct an unused client.
            for _ in range(2):
                assert client.get("/documents-probe").json() == {"results": []}
            assert len(built) == run + 1
            assert not built[-1].closed
        assert built[-1].closed


def test_fake_only_returns_what_the_person_may_see():
    fake = FakeDocumentsClient(
        [{"id": str(DOC), "title": "Notes", "text": "CPSIF notes", "visible_to": [str(PERSON)]}]
    )
    assert [r["doc_id"] for r in fake.search(PERSON, "cpsif")] == [str(DOC)]
    assert fake.search(OTHER, "cpsif") == []
    assert fake.get_doc(PERSON, DOC)["title"] == "Notes"
    assert "visible_to" not in fake.get_doc(PERSON, DOC)
    assert fake.get_doc(OTHER, DOC) is None
    with pytest.raises(DocumentsUnavailable):
        FakeDocumentsClient(down=True).search(PERSON, "q")
