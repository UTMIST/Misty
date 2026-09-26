import base64
import json
import struct
import subprocess
import sys
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError

from src.providers.base import (
    EmbeddingRequest,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    ProviderUnsupportedModel,
)
from src.providers.openai_embed import OpenAIEmbeddingProvider

_REQ = httpx.Request("POST", "https://openai.invalid/v1/embeddings")
_INVALID_MODEL_METADATA = [
    pytest.param({}, id="missing"),
    pytest.param({"model": None}, id="null"),
    pytest.param({"model": 7}, id="scalar"),
    pytest.param({"model": True}, id="bool"),
    pytest.param({"model": {"private-upstream-payload": "x"}}, id="mapping"),
    pytest.param({"model": ["private-upstream-payload"]}, id="list"),
    pytest.param({"model": ""}, id="empty"),
    pytest.param({"model": "private-upstream-payload"}, id="unknown"),
    pytest.param({"model": "openai-embed-3-small"}, id="neutral-alias"),
]


def _response(count=1, **fields):
    return SimpleNamespace(
        **{
            "object": "list",
            "model": "text-embedding-3-small",
            "data": [SimpleNamespace(index=i, embedding=[float(i)] * 1536) for i in range(count)],
            "usage": SimpleNamespace(prompt_tokens=9, total_tokens=9),
            **deepcopy(fields),
        }
    )


def _provider(client=None, model="openai-embed-3-small", api_key="test-key"):
    return OpenAIEmbeddingProvider(
        api_key=api_key, default_model=model, timeout_s=30.0, client=client
    )


@contextmanager
def _boundary(response, backend="fake", *, model="openai-embed-3-small"):
    if backend == "fake":
        call = Mock(return_value=response)
        yield _provider(Mock(embeddings=Mock(create=call)), model), call
        return
    call = Mock(
        return_value=httpx.Response(
            200,
            content=json.dumps(response, default=vars),
            headers={"content-type": "application/json"},
        )
    )
    with httpx.Client(transport=httpx.MockTransport(call), trust_env=False) as http_client:
        with OpenAI(
            api_key="test-key",
            base_url="https://openai.invalid/v1",
            http_client=http_client,
            max_retries=0,
        ) as client:
            yield _provider(client, model), call


def _base64_vector(vector):
    return base64.b64encode(struct.pack("<1536f", *vector)).decode("ascii")


@pytest.mark.parametrize(
    "model,override,model_id",
    [
        ("openai-embed-3-small", None, "text-embedding-3-small"),
        ("openai-embed-3-large", None, "text-embedding-3-large"),
        ("openai-embed-3-small", "openai-embed-3-large", "text-embedding-3-large"),
    ],
)
@pytest.mark.parametrize(
    "backend,encoding", [("fake", "float"), ("sdk", "float"), ("sdk", "base64")]
)
def test_batch_mapping_and_order(model, override, model_id, backend, encoding):
    """Vectors must be distinguishable or this cannot detect misordering."""
    response = _response(count=3, model=model_id)
    original_vectors = [item.embedding for item in response.data]
    response.data = [response.data[i] for i in (2, 0, 1)]
    if encoding == "base64":
        for item in response.data:
            item.embedding = _base64_vector(item.embedding)
    with _boundary(response, backend, model=model) as (provider, call):
        result = provider.embed(EmbeddingRequest(inputs=["a", "b", "c"], model=override))
    sent = {"model": model_id, "input": ["a", "b", "c"], "dimensions": 1536}
    if backend == "fake":
        call.assert_called_once_with(**sent)
        assert all(actual is original for actual, original in zip(result.vectors, original_vectors))
    else:
        call.assert_called_once()
        assert json.loads(call.call_args.args[0].content) == {**sent, "encoding_format": "base64"}
    assert len(result.vectors) == 3
    assert all(len(vector) == 1536 for vector in result.vectors)
    assert [vector[0] for vector in result.vectors] == [0.0, 1.0, 2.0]
    assert result.model == model_id
    assert result.dimensions == 1536
    assert result.input_tokens == 9


@pytest.mark.parametrize(
    "exc,expected",
    [
        (
            RateLimitError(
                "private-upstream-payload", response=httpx.Response(429, request=_REQ), body=None
            ),
            ProviderRateLimited,
        ),
        (APITimeoutError(request=_REQ), ProviderTimeout),
        (APIConnectionError(request=_REQ), ProviderTimeout),
        (
            APIStatusError(
                "private-upstream-payload", response=httpx.Response(500, request=_REQ), body=None
            ),
            ProviderUnavailable,
        ),
        (RuntimeError("unexpected"), ProviderUnavailable),
    ],
)
def test_failures_normalize(exc, expected):
    client = Mock(embeddings=Mock(create=Mock(side_effect=exc)))
    with pytest.raises(expected) as caught:
        _provider(client).embed(EmbeddingRequest(inputs=["a"]))
    assert "private-upstream-payload" not in str(caught.value)


def test_unknown_per_request_model_is_rejected():
    """A model this provider cannot serve is a client error (422), not a 502."""
    with _boundary(_response()) as (provider, call):
        with pytest.raises(ProviderUnsupportedModel, match="unsupported embedding model"):
            provider.embed(EmbeddingRequest(inputs=["a"], model="nope"))
    call.assert_not_called()


def test_unknown_default_model_fails_at_construction():
    with pytest.raises(ValueError, match="unsupported default embedding model"):
        _provider(model="nope")


def test_missing_api_key_is_a_request_time_503_not_a_boot_crash(monkeypatch):
    """A missing local key must not stop the service booting. It degrades like
    connectors with no Google credential: /health stays green and only the calls
    that need the key fail. verify_production_secrets makes it boot-fatal outside
    `local`, where it genuinely is."""
    constructor = Mock(side_effect=AssertionError("client must not be constructed"))
    monkeypatch.setattr("openai.OpenAI", constructor)
    provider = _provider(api_key="")
    with pytest.raises(ProviderNotConfigured, match="OPENAI_API_KEY is not set"):
        provider.embed(EmbeddingRequest(inputs=["a"]))
    constructor.assert_not_called()


def test_client_is_not_built_until_first_call(monkeypatch):
    constructor = Mock(side_effect=AssertionError("client must not be constructed"))
    monkeypatch.setattr("openai.OpenAI", constructor)
    _provider()
    constructor.assert_not_called()


def test_sdk_is_not_imported_at_module_scope():
    """Importing the provider and constructing it must not import the SDK."""
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            """
import sys

class BlockOpenAI:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "openai" or fullname.startswith("openai."):
            raise AssertionError("SDK imported before an embedding request")

sys.meta_path.insert(0, BlockOpenAI())
from src.providers.openai_embed import OpenAIEmbeddingProvider
OpenAIEmbeddingProvider(api_key="", default_model="openai-embed-3-small", timeout_s=30.0)
assert "openai" not in sys.modules
""",
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_missing_sdk_normalizes_rather_than_unbinding(monkeypatch):
    """D1b: the deferred import must have its OWN try. Inside the main one, an
    ImportError leaves RateLimitError unbound and evaluating the except clause
    raises UnboundLocalError — a 500, not the documented 502."""
    import builtins

    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "openai":
            raise ImportError("simulated missing extra")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    provider = _provider()
    with pytest.raises(ProviderUnavailable, match="openai package is not installed"):
        provider.embed(EmbeddingRequest(inputs=["a"]))


def test_openai_retries_are_disabled(monkeypatch):
    """D4: the SDK retries twice by default, making `timeout` per-attempt."""
    call = Mock(
        return_value=httpx.Response(500, json={"error": {"message": "private-upstream-payload"}})
    )
    with httpx.Client(transport=httpx.MockTransport(call), trust_env=False) as http_client:
        constructor = Mock(
            side_effect=lambda **kwargs: OpenAI(
                http_client=http_client, base_url="https://openai.invalid/v1", **kwargs
            )
        )
        monkeypatch.setattr("openai.OpenAI", constructor)
        provider = _provider()
        with pytest.raises(ProviderUnavailable) as caught:
            provider.embed(EmbeddingRequest(inputs=["a"]))
        assert "private-upstream-payload" not in str(caught.value)
        constructor.assert_called_once()
        assert provider._client.max_retries == 0
        assert provider._client.timeout == 30.0
        call.assert_called_once()


# --- regressions for the Devin audit (D1, D1b, D2) ---


@pytest.mark.parametrize("backend", ["fake", "sdk"])
@pytest.mark.parametrize(
    "make_response",
    [
        pytest.param(lambda: None, id="null-response"),
        pytest.param(lambda: SimpleNamespace(model="text-embedding-3-small"), id="missing-data"),
        pytest.param(lambda: _response(data=None), id="null-data"),
        pytest.param(lambda: _response(data=7), id="scalar-data"),
        pytest.param(lambda: _response(data="private-upstream-payload"), id="text-data"),
        pytest.param(lambda: _response(data={}), id="mapping-data"),
    ],
)
def test_non_list_data_normalizes(make_response, backend):
    """D1: sorted(None) raises a raw TypeError past the router's ladder."""
    expected = "response shape: data"
    if backend == "sdk":
        expected += "|openai embedding failed:"
    with _boundary(make_response(), backend) as (provider, call):
        with pytest.raises(ProviderUnavailable, match=expected) as caught:
            provider.embed(EmbeddingRequest(inputs=["a"]))
    call.assert_called_once()
    assert "private-upstream-payload" not in str(caught.value)


def test_tuple_data_is_not_coerced():
    with _boundary(_response(data=())) as (provider, call):
        with pytest.raises(ProviderUnavailable, match="response shape: data") as caught:
            provider.embed(EmbeddingRequest(inputs=["a"]))
    call.assert_called_once()
    assert "private-upstream-payload" not in str(caught.value)


@pytest.mark.parametrize("backend", ["fake", "sdk"])
@pytest.mark.parametrize(
    "entry,message",
    [
        pytest.param(None, "indices", id="null"),
        pytest.param(7, "indices", id="scalar"),
        pytest.param("private-upstream-payload", "indices", id="text"),
        pytest.param({}, "indices", id="mapping"),
        pytest.param(SimpleNamespace(), "indices", id="missing-attributes"),
        pytest.param(SimpleNamespace(embedding=[0.1] * 1536), "indices", id="missing-index"),
        pytest.param(SimpleNamespace(index=0), "not a vector", id="missing-embedding"),
        pytest.param(SimpleNamespace(index=0, embedding=None), "not a vector", id="null-vector"),
        pytest.param(SimpleNamespace(index=0, embedding=7), "not a vector", id="scalar-vector"),
    ],
)
def test_malformed_entries_normalize(entry, message, backend):
    expected = message if backend == "fake" else f"{message}|openai embedding failed:"
    with _boundary(_response(data=[entry]), backend) as (provider, call):
        with pytest.raises(ProviderUnavailable, match=expected) as caught:
            provider.embed(EmbeddingRequest(inputs=["a"]))
    call.assert_called_once()
    assert "private-upstream-payload" not in str(caught.value)


@pytest.mark.parametrize("backend", ["fake", "sdk"])
@pytest.mark.parametrize(
    "indices,count",
    [
        pytest.param([], 1, id="empty"),
        pytest.param([False], 1, id="false"),
        pytest.param([0, True], 2, id="true"),
        pytest.param([0.0], 1, id="float"),
        pytest.param([None], 1, id="null"),
        pytest.param([0, None], 2, id="mixed-null"),
        pytest.param([0, "private-upstream-payload"], 2, id="mixed-text"),
        pytest.param(["private-upstream-payload"], 1, id="text"),
        pytest.param([0, {}], 2, id="mixed-mapping"),
        pytest.param([[]], 1, id="list"),
        pytest.param([0, 0], 2, id="duplicate"),
        pytest.param([0, 0, 2], 3, id="duplicate-with-correct-count"),
        pytest.param([-1], 1, id="negative"),
        pytest.param([1], 1, id="out-of-range"),
        pytest.param([0, 2], 2, id="missing-index"),
        pytest.param([0], 2, id="count-mismatch"),
    ],
)
def test_invalid_indices_normalize(indices, count, backend):
    """Indices [0, 0, 2] pass a count check and every width check, then the router
    relabels positionally — input 1 would be stored holding input 0's vector."""
    response = _response(count=len(indices))
    for item, index in zip(response.data, deepcopy(indices)):
        item.index = index
    expected = "indices" if backend == "fake" else "indices|openai embedding failed:"
    with _boundary(response, backend) as (provider, call):
        with pytest.raises(ProviderUnavailable, match=expected) as caught:
            provider.embed(EmbeddingRequest(inputs=["a"] * count))
    call.assert_called_once()
    assert "private-upstream-payload" not in str(caught.value)


@pytest.mark.parametrize(
    "vector,message",
    [
        pytest.param(True, "not a vector", id="bool-vector"),
        pytest.param("private-upstream-payload", "not a vector", id="text-vector"),
        pytest.param((0.1,) * 1536, "not a vector", id="tuple-vector"),
        pytest.param(
            {i: "private-upstream-payload" for i in range(1536)},
            "not a vector",
            id="mapping-vector",
        ),
        pytest.param([0.1] * 512, "512 dimensions, expected 1536", id="wrong-width"),
        pytest.param([None] * 1536, "non-numeric", id="null-component"),
        pytest.param(["private-upstream-payload"] * 1536, "non-numeric", id="text-component"),
        pytest.param([True] * 1536, "non-numeric", id="true-component"),
        pytest.param([False] * 1536, "non-numeric", id="false-component"),
        pytest.param([{}] * 1536, "non-numeric", id="mapping-component"),
        pytest.param([[]] * 1536, "non-numeric", id="list-component"),
        pytest.param([float("nan")] * 1536, "not finite", id="nan"),
        pytest.param([float("inf")] * 1536, "not finite", id="infinity"),
        pytest.param([float("-inf")] * 1536, "not finite", id="negative-infinity"),
        pytest.param([10**1000] * 1536, "not finite", id="huge-integer"),
        pytest.param([-(10**1000)] * 1536, "not finite", id="negative-huge-integer"),
    ],
)
def test_invalid_vectors_normalize(vector, message):
    """Reject non-finite and overflowing vector components."""
    response = _response()
    response.data[0].embedding = deepcopy(vector)
    with _boundary(response) as (provider, call):
        with pytest.raises(ProviderUnavailable, match=message) as caught:
            provider.embed(EmbeddingRequest(inputs=["a"]))
    call.assert_called_once()
    assert "private-upstream-payload" not in str(caught.value)
    if message == "not finite":
        assert str(vector[0]) not in str(caught.value)


@pytest.mark.parametrize("backend", ["fake", "sdk"])
@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({}, id="missing-usage"),
        pytest.param({"usage": None}, id="null-usage"),
        pytest.param({"usage": 7}, id="scalar-usage"),
        pytest.param({"usage": {}}, id="mapping-usage"),
        pytest.param({"usage": SimpleNamespace()}, id="missing-count"),
        *[
            pytest.param({"usage": SimpleNamespace(prompt_tokens=value)}, id=f"count-{name}")
            for name, value in [
                ("text", "many"),
                ("null", None),
                ("mapping", {}),
                ("list", []),
                ("nan", float("nan")),
                ("infinity", float("inf")),
                ("negative-infinity", float("-inf")),
                ("huge-integer", 10**1000),
                ("negative", -1),
                ("true", True),
                ("false", False),
            ]
        ],
    ],
)
def test_malformed_or_missing_usage_is_zero(fields, backend):
    """D1: int('many') raises ValueError outside the ladder. Usage reporting must
    never turn a 200 into a 500 — drop it instead."""
    response = _response()
    del response.usage
    vars(response).update(deepcopy(fields))
    with _boundary(response, backend) as (provider, call):
        result = provider.embed(EmbeddingRequest(inputs=["a"]))
    call.assert_called_once()
    assert result.input_tokens == 0


@pytest.mark.parametrize("backend", ["fake", "sdk"])
@pytest.mark.parametrize("metadata", _INVALID_MODEL_METADATA)
def test_invalid_response_model_metadata_is_rejected(metadata, backend):
    response = _response()
    del response.model
    vars(response).update(deepcopy(metadata))
    with _boundary(response, backend) as (provider, call):
        with pytest.raises(ProviderUnavailable) as caught:
            provider.embed(EmbeddingRequest(inputs=["a"]))
    call.assert_called_once()
    assert str(caught.value) == "OpenAI returned unexpected embedding model"


@pytest.mark.parametrize("backend", ["fake", "sdk"])
@pytest.mark.parametrize(
    "model,response_model",
    [
        ("openai-embed-3-small", "text-embedding-3-large"),
        ("openai-embed-3-large", "text-embedding-3-small"),
    ],
)
def test_same_width_wrong_response_model_is_rejected(model, response_model, backend):
    with _boundary(_response(model=response_model), backend, model=model) as (provider, call):
        with pytest.raises(ProviderUnavailable) as caught:
            provider.embed(EmbeddingRequest(inputs=["a"]))
    call.assert_called_once()
    assert str(caught.value) == "OpenAI returned unexpected embedding model"


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_real_sdk_non_finite_base64_vector_is_rejected(value):
    response = _response()
    response.data[0].embedding = _base64_vector([value] * 1536)
    with _boundary(response, "sdk") as (provider, call):
        with pytest.raises(ProviderUnavailable, match="not finite"):
            provider.embed(EmbeddingRequest(inputs=["a"]))
    call.assert_called_once()
