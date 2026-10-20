import json

import httpx
import pytest

from contracts.embedder import EmbedderRejected, EmbedderUnavailable
from src.embed.http_client import MAX_BATCH_INPUTS, HttpEmbedder


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok(inputs, *, model="text-embedding-3-small", dimensions=1536, reverse=False):
    embeddings = [
        {"index": i, "vector": [float(i)] + [0.0] * (dimensions - 1)} for i in range(len(inputs))
    ]
    if reverse:
        embeddings.reverse()
    return httpx.Response(
        200,
        json={
            "embeddings": embeddings,
            "model": model,
            "dimensions": dimensions,
            "usage": {"input_tokens": 1},
        },
    )


def _recording_handler(requests, **ok_kwargs):
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return _ok(body["inputs"], **ok_kwargs)

    return handler


def test_posts_inputs_with_key_and_returns_resolved_model():
    seen = []

    def handler(request):
        assert request.url == "http://llm/embed"
        assert request.headers["X-API-Key"] == "k"
        seen.append(json.loads(request.content))
        return _ok(["a", "b"])

    batch = HttpEmbedder("http://llm/", "k", client=_client(handler)).embed(["a", "b"])
    assert seen == [{"inputs": ["a", "b"]}]
    assert batch.model == "text-embedding-3-small"
    assert batch.dimensions == 1536
    assert [v[0] for v in batch.vectors] == [0.0, 1.0]


def test_sends_configured_model_alias():
    requests = []
    embedder = HttpEmbedder(
        "http://llm",
        "k",
        model="openai-embed-3-small",
        client=_client(_recording_handler(requests)),
    )
    embedder.embed(["a"])
    assert requests[0]["model"] == "openai-embed-3-small"


def test_vectors_follow_response_index_not_response_order():
    def handler(request):
        return _ok(json.loads(request.content)["inputs"], reverse=True)

    batch = HttpEmbedder("http://llm", "k", client=_client(handler)).embed(["a", "b", "c"])
    assert [v[0] for v in batch.vectors] == [0.0, 1.0, 2.0]


def test_splits_at_the_batch_input_limit():
    requests = []
    inputs = [f"chunk {i}" for i in range(MAX_BATCH_INPUTS * 2 + 1)]
    batch = HttpEmbedder("http://llm", "k", client=_client(_recording_handler(requests))).embed(
        inputs
    )
    assert [len(r["inputs"]) for r in requests] == [MAX_BATCH_INPUTS, MAX_BATCH_INPUTS, 1]
    assert [text for r in requests for text in r["inputs"]] == inputs
    assert len(batch.vectors) == len(inputs)


def test_splits_at_the_request_character_budget():
    requests = []
    embedder = HttpEmbedder(
        "http://llm", "k", max_request_chars=10, client=_client(_recording_handler(requests))
    )
    embedder.embed(["aaaa", "bbbb", "cccc", "dddddddddd"])
    assert [r["inputs"] for r in requests] == [["aaaa", "bbbb"], ["cccc"], ["dddddddddd"]]
    assert all(sum(map(len, r["inputs"])) <= 10 for r in requests)


@pytest.mark.parametrize(
    "invalid,budget",
    [
        ("x" * 32_001, 400_000),
        ("x" * 11, 10),
        (" \n\t", 400_000),
        ("private text \ud800", 400_000),
        (None, 400_000),
    ],
    ids=["input-limit", "request-budget", "blank", "invalid-utf8", "non-string"],
)
def test_late_invalid_input_is_rejected_before_any_request(invalid, budget):
    def unexpected_request(request):
        pytest.fail("known invalid input must fail before the first HTTP request")

    embedder = HttpEmbedder(
        "http://llm", "k", max_request_chars=budget, client=_client(unexpected_request)
    )
    inputs = ["valid"] * (MAX_BATCH_INPUTS + 1) + [invalid]
    with pytest.raises(EmbedderRejected) as exc:
        embedder.embed(inputs)
    assert "private text" not in str(exc.value)


def test_empty_input_list_is_rejected_without_a_request():
    def unexpected_request(request):
        pytest.fail("an empty input list must not reach the LLM service")

    with pytest.raises(EmbedderRejected):
        HttpEmbedder("http://llm", "k", client=_client(unexpected_request)).embed([])


def test_input_at_both_character_limits_is_accepted():
    requests = []
    embedder = HttpEmbedder(
        "http://llm", "k", max_request_chars=32_000, client=_client(_recording_handler(requests))
    )
    embedder.embed(["x" * 32_000])
    assert [len(r["inputs"][0]) for r in requests] == [32_000]


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_statuses_are_unavailable(status):
    embedder = HttpEmbedder(
        "http://llm", "k", client=_client(lambda r: httpx.Response(status, json={}))
    )
    with pytest.raises(EmbedderUnavailable):
        embedder.embed(["a"])


@pytest.mark.parametrize("status", [401, 403, 422])
def test_client_errors_are_rejected(status):
    embedder = HttpEmbedder(
        "http://llm", "k", client=_client(lambda r: httpx.Response(status, json={}))
    )
    with pytest.raises(EmbedderRejected):
        embedder.embed(["a"])


def test_connection_error_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(EmbedderUnavailable):
        HttpEmbedder("http://llm", "k", client=_client(handler)).embed(["a"])


def test_wrong_dimensions_are_rejected():
    def handler(request):
        return _ok(["a"], dimensions=3072)

    with pytest.raises(EmbedderRejected, match="3072"):
        HttpEmbedder("http://llm", "k", client=_client(handler)).embed(["a"])


def test_missing_vectors_are_rejected():
    def handler(request):
        return _ok(["a"])  # one vector for two inputs

    with pytest.raises(EmbedderRejected):
        HttpEmbedder("http://llm", "k", client=_client(handler)).embed(["a", "b"])


@pytest.mark.parametrize("body", [{"model": "m"}, [], {"embeddings": "x", "model": "m"}])
def test_malformed_body_is_rejected(body):
    embedder = HttpEmbedder(
        "http://llm", "k", client=_client(lambda r: httpx.Response(200, json=body))
    )
    with pytest.raises(EmbedderRejected):
        embedder.embed(["a"])


def test_model_change_between_batches_is_unavailable():
    models = iter(["model-a", "model-b"])

    def handler(request):
        return _ok(json.loads(request.content)["inputs"], model=next(models))

    embedder = HttpEmbedder("http://llm", "k", max_request_chars=1, client=_client(handler))
    with pytest.raises(EmbedderUnavailable, match="model changed"):
        embedder.embed(["a", "b"])
