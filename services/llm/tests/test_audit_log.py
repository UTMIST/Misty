import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from platform_auth import InMemoryKeyStore

from src.api.app import create_app
from src.api.deps import get_embedder, get_key_store, get_llm
from src.providers.base import EmbeddingResult, LLMResult, ProviderUnavailable


class _FakeProvider:
    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc

    def chat(self, request):
        if self._exc is not None:
            raise self._exc
        return self._result


@pytest.fixture
def env_key(monkeypatch):
    monkeypatch.setenv("API_KEY", "env-bootstrap-key-value")
    from src.config import get_settings

    get_settings.cache_clear()
    yield "env-bootstrap-key-value"


def _client(env_key, provider, dependency=get_llm):
    app = create_app()
    app.dependency_overrides[get_key_store] = lambda: InMemoryKeyStore()
    app.dependency_overrides[dependency] = lambda: provider
    return TestClient(app), {"X-API-Key": env_key}


def _ok_result():
    return LLMResult(
        content="the completion body",
        model="us.anthropic.claude-sonnet-4-6",
        stop_reason="end_turn",
        input_tokens=11,
        output_tokens=7,
    )


def _last_line(capsys) -> dict:
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip().startswith("{")]
    assert lines, "no audit log line emitted"
    return json.loads(lines[-1])


def test_success_line_has_model_and_tokens(env_key, capsys):
    client, headers = _client(env_key, _FakeProvider(result=_ok_result()))
    resp = client.post(
        "/chat",
        headers=headers,
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    entry = _last_line(capsys)
    assert entry["path"] == "/chat"
    assert entry["status"] == 200
    assert entry["model"] == "claude-sonnet-4-6"  # neutral default, not the bedrock id
    assert entry["input_tokens"] == 11
    assert entry["output_tokens"] == 7
    assert entry["key_name"] == "env-bootstrap"
    assert "request_id" in entry


def test_line_omits_prompt_and_key(env_key, capsys):
    client, headers = _client(env_key, _FakeProvider(result=_ok_result()))
    secret_prompt = "SUPER-SECRET-PROMPT-TEXT"
    client.post(
        "/chat",
        headers=headers,
        json={"messages": [{"role": "user", "content": secret_prompt}], "system": "SYSTEM-SECRET"},
    )
    entry = _last_line(capsys)
    line = json.dumps(entry)
    assert secret_prompt not in line
    assert "SYSTEM-SECRET" not in line
    assert "the completion body" not in line
    assert "env-bootstrap-key-value" not in line  # the raw API key never appears


def test_error_line_has_model_no_tokens(env_key, capsys):
    client, headers = _client(env_key, _FakeProvider(exc=ProviderUnavailable("boom")))
    resp = client.post(
        "/chat",
        headers=headers,
        json={"messages": [{"role": "user", "content": "hi"}], "model": "claude-opus-4-6"},
    )
    assert resp.status_code == 502
    entry = _last_line(capsys)
    assert entry["status"] == 502
    assert entry["model"] == "claude-opus-4-6"  # attempted model recorded
    assert "input_tokens" not in entry
    assert "output_tokens" not in entry


@pytest.mark.parametrize("fails", [False, True])
def test_embed_audit_contains_metadata_without_private_content(env_key, capsys, monkeypatch, fails):
    api_key = "synthetic-private-openai-key"
    monkeypatch.setenv("OPENAI_API_KEY", api_key)
    inputs = ["SYNTHETIC PRIVATE INPUT ONE", "SYNTHETIC PRIVATE INPUT TWO"]
    upstream_message = "synthetic-private-upstream-message"
    embedder = Mock()
    embedder.embed.return_value = EmbeddingResult(
        vectors=[[0.123456789] * 1536, [0.987654321] * 1536],
        model="text-embedding-3-large",
        dimensions=1536,
        input_tokens=11,
    )
    if fails:
        embedder.embed.side_effect = ProviderUnavailable(upstream_message)
    client, headers = _client(env_key, embedder, dependency=get_embedder)

    resp = client.post(
        "/embed", headers=headers, json={"inputs": inputs, "model": "openai-embed-3-large"}
    )

    assert resp.status_code == (502 if fails else 200)
    assert upstream_message not in resp.text
    entry = _last_line(capsys)
    assert entry["path"] == "/embed"
    assert entry["status"] == resp.status_code
    assert entry["key_name"] == "env-bootstrap"
    assert entry["model"] == "openai-embed-3-large"
    assert entry["input_count"] == 2
    assert entry["input_chars"] == sum(map(len, inputs))
    if fails:
        assert "dimensions" not in entry
        assert "input_tokens" not in entry
    else:
        assert entry["dimensions"] == 1536
        assert entry["input_tokens"] == 11
    line = json.dumps(entry)
    for private in [*inputs, env_key, api_key, "0.123456789", "0.987654321", upstream_message]:
        assert private not in line
