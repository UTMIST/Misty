from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import OpenAI

from platform_auth import InMemoryKeyStore

from contracts.embed import MAX_BATCH_INPUTS, MAX_INPUT_CHARS
from src.api.app import create_app
from src.api.deps import get_embedder, get_key_store, get_llm
from src.api.hashing import generate_key
from src.providers.base import (
    EmbeddingRequest,
    EmbeddingResult,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)


class _FakeEmbedder:
    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.last_request = None

    def embed(self, request):
        self.last_request = request
        if self._exc is not None:
            raise self._exc
        return self._result


@pytest.fixture
def env_key(monkeypatch):
    monkeypatch.setenv("API_KEY", "env-bootstrap-key-value")
    from src.config import get_settings

    get_settings.cache_clear()
    yield "env-bootstrap-key-value"


def _client(env_key, embedder, store=None):
    app = create_app()
    app.dependency_overrides[get_key_store] = lambda: store or InMemoryKeyStore()
    app.dependency_overrides[get_embedder] = lambda: embedder
    return TestClient(app), {"X-API-Key": env_key}


def _consumer_key(store, *, scopes):
    plaintext, prefix, key_hash = generate_key()
    store.add(prefix=prefix, key_hash=key_hash, name="consumer", scopes=scopes)
    return plaintext


def _ok_result(count=1, model="text-embedding-3-small"):
    return EmbeddingResult(
        vectors=[[i + 0.25] * 1536 for i in range(count)],
        model=model,
        dimensions=1536,
        input_tokens=11,
    )


@pytest.mark.parametrize(
    "model,resolved",
    [
        (None, "text-embedding-3-small"),
        ("openai-embed-3-small", "text-embedding-3-small"),
        ("openai-embed-3-large", "text-embedding-3-large"),
    ],
)
def test_embed_returns_one_vector_per_input(env_key, model, resolved):
    result = _ok_result(count=2, model=resolved)
    embedder = _FakeEmbedder(result=result)
    client, headers = _client(env_key, embedder)
    inputs = [" first input ", "second input"]
    payload = {"inputs": inputs}
    if model is not None:
        payload["model"] = model

    resp = client.post("/embed", json=payload, headers=headers)

    assert resp.status_code == 200
    assert embedder.last_request == EmbeddingRequest(inputs=inputs, model=model)
    body = resp.json()
    assert body["embeddings"] == [
        {"index": i, "vector": vector} for i, vector in enumerate(result.vectors)
    ]
    assert all(len(item["vector"]) == 1536 for item in body["embeddings"])
    assert body["model"] == resolved
    assert body["dimensions"] == 1536
    assert body["usage"] == {"input_tokens": 11}


@pytest.mark.parametrize("count", [1, MAX_BATCH_INPUTS])
def test_batch_size_boundaries_are_allowed(env_key, count):
    embedder = _FakeEmbedder(result=_ok_result(count=count))
    client, headers = _client(env_key, embedder)
    inputs = [f"input {i}" for i in range(count)]

    resp = client.post("/embed", json={"inputs": inputs}, headers=headers)

    assert resp.status_code == 200
    assert embedder.last_request.inputs == inputs
    assert [item["index"] for item in resp.json()["embeddings"]] == list(range(count))


def test_embed_scope_is_required_and_chat_does_not_satisfy_it(env_key):
    """A completions key must not be able to spend embedding budget."""
    store = InMemoryKeyStore()
    chat_only = _consumer_key(store, scopes=["chat"])
    embedder = _FakeEmbedder(result=_ok_result())
    client, _ = _client(env_key, embedder, store=store)

    resp = client.post("/embed", json={"inputs": ["a"]}, headers={"X-API-Key": chat_only})

    assert resp.status_code == 403
    assert embedder.last_request is None  # rejected before any provider call


def test_embed_scope_grants_access(env_key):
    store = InMemoryKeyStore()
    key = _consumer_key(store, scopes=["embed"])
    client, _ = _client(env_key, _FakeEmbedder(result=_ok_result()), store=store)

    resp = client.post("/embed", json={"inputs": ["a"]}, headers={"X-API-Key": key})

    assert resp.status_code == 200


def test_embed_key_does_not_grant_chat_access(env_key):
    store = InMemoryKeyStore()
    key = _consumer_key(store, scopes=["embed"])
    client, _ = _client(env_key, _FakeEmbedder(result=_ok_result()), store=store)
    provider = Mock()
    client.app.dependency_overrides[get_llm] = lambda: provider

    resp = client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "hello"}]},
        headers={"X-API-Key": key},
    )

    assert resp.status_code == 403
    provider.chat.assert_not_called()


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "invalid-key"}])
def test_missing_or_invalid_key_is_401_before_the_provider(env_key, headers):
    embedder = _FakeEmbedder(result=_ok_result())
    client, _ = _client(env_key, embedder)
    assert client.post("/embed", json={"inputs": ["a"]}, headers=headers).status_code == 401
    assert embedder.last_request is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"inputs": "a"},
        {"inputs": [None]},
        {"inputs": []},
        {"inputs": [""]},
        {"inputs": ["   "]},
        {"inputs": ["a"], "model": "not-a-model"},
        {"inputs": ["a" * (MAX_INPUT_CHARS + 1)]},
        {"inputs": ["a"] * (MAX_BATCH_INPUTS + 1)},
    ],
)
def test_invalid_requests_are_422_before_the_provider(env_key, payload):
    """Validation happens before any paid call — nothing reaches the provider."""
    embedder = _FakeEmbedder(result=_ok_result())
    client, headers = _client(env_key, embedder)

    resp = client.post("/embed", json=payload, headers=headers)

    assert resp.status_code == 422
    assert embedder.last_request is None


@pytest.mark.parametrize(
    "error,status,detail",
    [
        (ProviderRateLimited, 429, "embedding provider rate limited"),
        (ProviderTimeout, 504, "embedding provider timeout"),
        (ProviderUnavailable, 502, "embedding provider error"),
    ],
)
def test_provider_failures_map_to_status_codes(env_key, error, status, detail):
    client, headers = _client(
        env_key, _FakeEmbedder(exc=error("synthetic-private-upstream-message"))
    )
    resp = client.post("/embed", json={"inputs": ["a"]}, headers=headers)
    assert resp.status_code == status
    assert resp.json() == {"detail": detail}


def test_unsupported_model_error_is_422_not_502(env_key):
    """Provider model validation is a client error, not an upstream fault."""
    from src.providers.base import ProviderUnsupportedModel

    embedder = _FakeEmbedder(exc=ProviderUnsupportedModel("unsupported embedding model: 'x'"))
    client, headers = _client(env_key, embedder)

    resp = client.post(
        "/embed", json={"inputs": ["a"], "model": "openai-embed-3-small"}, headers=headers
    )

    assert resp.status_code == 422
    assert "unsupported embedding model" in resp.json()["detail"]


def test_missing_credential_is_503_not_502(env_key):
    """Nothing upstream failed; this deployment has no credential."""
    from src.providers.base import ProviderNotConfigured

    client, headers = _client(env_key, _FakeEmbedder(exc=ProviderNotConfigured("no key")))
    assert client.post("/embed", json={"inputs": ["a"]}, headers=headers).status_code == 503


def test_embed_appears_in_openapi(env_key):
    client, _ = _client(env_key, _FakeEmbedder(result=_ok_result()))
    schema = client.get("/openapi.json").json()
    assert "/embed" in schema["paths"]
    assert "post" in schema["paths"]["/embed"]


def test_unknown_embed_model_fails_at_boot(monkeypatch):
    """A typo'd EMBED_MODEL must kill the deploy, not leave /health green."""
    monkeypatch.setenv("EMBED_MODEL", "openai-embed-typo")
    from src.api import deps
    from src.config import get_settings

    get_settings.cache_clear()
    deps._embedder.cache_clear()
    with pytest.raises(ValueError, match="unsupported default embedding model"):
        create_app()


def test_oversized_batch_is_422_before_any_spend(env_key, monkeypatch):
    """Per-input caps multiply: 96 x 32,000 is ~3M chars in one request. The
    batch ceiling bounds their product, and must reject before the provider."""
    monkeypatch.setenv("EMBED_MAX_REQUEST_CHARS", "1000")
    from src.config import get_settings

    get_settings.cache_clear()
    embedder = _FakeEmbedder(result=_ok_result())
    client, headers = _client(env_key, embedder)

    resp = client.post("/embed", json={"inputs": ["a" * 600, "b" * 600]}, headers=headers)

    assert resp.status_code == 422
    assert "over the 1000 limit" in resp.json()["detail"]
    assert embedder.last_request is None  # never reached the provider


def test_batch_at_the_ceiling_is_allowed(env_key, monkeypatch):
    monkeypatch.setenv("EMBED_MAX_REQUEST_CHARS", "1000")
    from src.config import get_settings

    get_settings.cache_clear()
    client, headers = _client(env_key, _FakeEmbedder(result=_ok_result()))

    resp = client.post("/embed", json={"inputs": ["a" * 1000]}, headers=headers)

    assert resp.status_code == 200


@pytest.mark.parametrize("value", [True, False])
@pytest.mark.parametrize(
    "model,model_id",
    [
        ("openai-embed-3-small", "text-embedding-3-small"),
        ("openai-embed-3-large", "text-embedding-3-large"),
    ],
)
def test_real_sdk_boolean_vectors_are_502(env_key, value, model, model_id):
    from src.providers.openai_embed import OpenAIEmbeddingProvider

    vector = [0.125] * 1536
    vector[768] = value
    upstream = Mock(
        return_value=httpx.Response(
            200,
            json={
                "object": "list",
                "model": model_id,
                "data": [{"object": "embedding", "index": 0, "embedding": vector}],
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
            },
        )
    )
    with httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False) as http_client:
        with OpenAI(
            api_key="test-key",
            base_url="https://openai.invalid/v1",
            http_client=http_client,
            max_retries=0,
        ) as sdk:
            provider = OpenAIEmbeddingProvider(
                api_key="test-key", default_model=model, timeout_s=30.0, client=sdk
            )
            client, headers = _client(env_key, provider)
            with client:
                resp = client.post(
                    "/embed", json={"inputs": ["synthetic input"], "model": model}, headers=headers
                )

    upstream.assert_called_once()
    assert resp.status_code == 502
    assert resp.json() == {"detail": "embedding provider error"}
