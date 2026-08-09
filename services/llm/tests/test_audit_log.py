import json

import pytest
from fastapi.testclient import TestClient

from platform_auth import InMemoryKeyStore

from src.api.app import create_app
from src.api.deps import get_key_store, get_llm
from src.providers.base import LLMResult, ProviderUnavailable


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


def _client(env_key, provider):
    app = create_app()
    app.dependency_overrides[get_key_store] = lambda: InMemoryKeyStore()
    app.dependency_overrides[get_llm] = lambda: provider
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
