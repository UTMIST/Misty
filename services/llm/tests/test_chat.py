import pytest
from fastapi.testclient import TestClient

from platform_auth import InMemoryKeyStore

from src.api.app import create_app
from src.api.deps import get_key_store, get_llm
from src.api.hashing import generate_key
from src.providers.base import (
    LLMResult,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)


class _FakeProvider:
    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.last_request = None

    def chat(self, request):
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


def _client(env_key, provider, store=None):
    app = create_app()
    app.dependency_overrides[get_key_store] = lambda: store or InMemoryKeyStore()
    app.dependency_overrides[get_llm] = lambda: provider
    return TestClient(app), {"X-API-Key": env_key}


def _consumer_key(store, *, scopes):
    """Mint a consumer key into `store` and return its plaintext value."""
    plaintext, prefix, key_hash = generate_key()
    store.add(prefix=prefix, key_hash=key_hash, name="consumer", scopes=scopes)
    return plaintext


def _ok_result():
    return LLMResult(
        content="hello",
        model="anthropic.claude-sonnet-5",
        stop_reason="end_turn",
        input_tokens=5,
        output_tokens=3,
    )


def test_chat_happy_path(env_key):
    provider = _FakeProvider(result=_ok_result())
    client, headers = _client(env_key, provider)
    resp = client.post(
        "/chat", headers=headers, json={"messages": [{"role": "user", "content": "hi"}]}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "hello"
    assert body["model"] == "anthropic.claude-sonnet-5"
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 5, "output_tokens": 3}
    # request mapped into neutral type; thinking defaulted on
    assert provider.last_request.messages[0].content == "hi"
    assert provider.last_request.thinking is True


def test_chat_thinking_false_roundtrips(env_key):
    provider = _FakeProvider(result=_ok_result())
    client, headers = _client(env_key, provider)
    resp = client.post(
        "/chat",
        headers=headers,
        json={"messages": [{"role": "user", "content": "hi"}], "thinking": False},
    )
    assert resp.status_code == 200
    assert provider.last_request.thinking is False


def test_chat_requires_auth(env_key):
    client, _ = _client(env_key, _FakeProvider(result=_ok_result()))
    resp = client.post("/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 401


def test_chat_key_without_chat_scope_forbidden(env_key):
    # A valid, active key that lacks the `chat` scope must be rejected (403),
    # not allowed to spend provider budget.
    store = InMemoryKeyStore()
    plaintext = _consumer_key(store, scopes=[])
    provider = _FakeProvider(result=_ok_result())
    client, _ = _client(env_key, provider, store=store)
    resp = client.post(
        "/chat",
        headers={"X-API-Key": plaintext},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 403
    # A forbidden request must never reach the provider (no budget spent).
    assert provider.last_request is None


def test_chat_key_with_chat_scope_allowed(env_key):
    store = InMemoryKeyStore()
    plaintext = _consumer_key(store, scopes=["chat"])
    client, _ = _client(env_key, _FakeProvider(result=_ok_result()), store=store)
    resp = client.post(
        "/chat",
        headers={"X-API-Key": plaintext},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "hello"


def test_chat_admin_wildcard_key_allowed(env_key):
    # The env-bootstrap key carries the admin/wildcard scope, which must satisfy
    # the `chat` scope requirement so local dev / admin callers keep working.
    provider = _FakeProvider(result=_ok_result())
    client, headers = _client(env_key, provider)
    resp = client.post(
        "/chat", headers=headers, json={"messages": [{"role": "user", "content": "hi"}]}
    )
    assert resp.status_code == 200


def test_chat_empty_messages_422(env_key):
    client, headers = _client(env_key, _FakeProvider(result=_ok_result()))
    assert client.post("/chat", headers=headers, json={"messages": []}).status_code == 422


def test_chat_invalid_model_422(env_key):
    client, headers = _client(env_key, _FakeProvider(result=_ok_result()))
    resp = client.post(
        "/chat",
        headers=headers,
        json={"messages": [{"role": "user", "content": "hi"}], "model": "gpt-4"},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "exc,status",
    [
        (ProviderRateLimited("x"), 429),
        (ProviderTimeout("x"), 504),
        (ProviderUnavailable("x"), 502),
    ],
)
def test_chat_provider_errors_mapped(env_key, exc, status):
    client, headers = _client(env_key, _FakeProvider(exc=exc))
    resp = client.post(
        "/chat", headers=headers, json={"messages": [{"role": "user", "content": "hi"}]}
    )
    assert resp.status_code == status


def test_openapi_lists_chat(env_key):
    client, _ = _client(env_key, _FakeProvider(result=_ok_result()))
    assert "/chat" in client.get("/openapi.json").json()["paths"]
