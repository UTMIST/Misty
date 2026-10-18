import httpx
import pytest

from contracts.retrieval import EmbeddingError
from src.embedding import HttpEmbeddingClient
from tests.retrieval_cases import vector


def response_body(**updates):
    return {
        "model": "text-embedding-3-small",
        "dimensions": 1536,
        "embeddings": [{"index": 0, "vector": vector([1, 0])}],
        **updates,
    }


def make_client(handler, api_key="fake-key", model="openai-embed-3-small"):
    return HttpEmbeddingClient(
        "https://llm.test",
        api_key,
        model,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_authenticated_single_query_and_model_compatibility():
    def handler(request):
        import json

        assert request.url.path == "/embed"
        assert request.headers["X-API-Key"] == "fake-key"
        assert json.loads(request.content) == {"inputs": ["CPSIF"], "model": "openai-embed-3-small"}
        return httpx.Response(200, json=response_body())

    result = make_client(handler).embed_query("CPSIF")
    assert result.model == "text-embedding-3-small"
    assert result.vector == tuple(vector([1, 0]))


@pytest.mark.parametrize(
    "updates",
    [
        {"model": "text-embedding-3-large"},
        {"model": "openai-embed-3-small"},
        {"dimensions": 3072},
        {"dimensions": "1536"},
        {"embeddings": []},
        {"embeddings": [{"index": 1, "vector": vector([1])}]},
        {"embeddings": [{"index": False, "vector": vector([1])}]},
        {"embeddings": [{"index": 0, "vector": [1]}]},
        {"embeddings": [{"index": 0, "vector": vector(["1"])}]},
        {"embeddings": [{"index": 0, "vector": vector([True])}]},
        {"embeddings": [{"index": 0, "vector": vector([1e39])}]},
        {"embeddings": [{"index": 0, "vector": vector([0])}]},
        {"embeddings": "invalid"},
    ],
)
def test_malformed_and_incompatible_embedding_is_dependency_error(updates):
    client = make_client(lambda request: httpx.Response(200, json=response_body(**updates)))
    with pytest.raises(EmbeddingError) as exc:
        client.embed_query("CPSIF")
    assert exc.value.status_code == 502


@pytest.mark.parametrize(
    "body",
    [
        b'{"dimensions":1536,"dimensions":1536}',
        b"not json",
        b"null",
        b'{"model":"text-embedding-3-small","dimensions":1536,"embeddings":[{"index":0,"vector":[NaN]}]}',
    ],
)
def test_invalid_json_response_is_dependency_error(body):
    client = make_client(lambda request: httpx.Response(200, content=body))
    with pytest.raises(EmbeddingError):
        client.embed_query("CPSIF")


@pytest.mark.parametrize(
    "status,expected", [(401, 502), (403, 502), (429, 503), (500, 502), (503, 503), (504, 504)]
)
def test_dependency_status_mapping(status, expected):
    with pytest.raises(EmbeddingError) as exc:
        make_client(lambda request: httpx.Response(status, text="secret details")).embed_query(
            "CPSIF"
        )
    assert exc.value.status_code == expected
    assert "secret details" not in str(exc.value)


def test_timeout_and_unconfigured_do_not_retry():
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("timeout")

    with pytest.raises(EmbeddingError) as exc:
        make_client(timeout).embed_query("CPSIF")
    assert exc.value.status_code == 504
    assert len(calls) == 1
    with pytest.raises(EmbeddingError) as exc:
        make_client(timeout, api_key="").embed_query("CPSIF")
    assert exc.value.status_code == 503
    assert len(calls) == 1
