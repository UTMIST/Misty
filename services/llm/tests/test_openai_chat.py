import json
import logging
from contextlib import contextmanager
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import OpenAI

from platform_auth import InMemoryKeyStore

from src.api.app import create_app
from src.api.deps import get_key_store, get_llm
from src.api.hashing import generate_key
from src.providers.base import (
    LLMMessage,
    LLMReasoningBlock,
    LLMRedactedReasoningBlock,
    LLMRequest,
    ProviderInvalidRequest,
    ProviderNotConfigured,
    ProviderUnsupportedModel,
)
from src.providers.openai_chat import OpenAIChatProvider

_MODEL = "gpt-6.1-sol"
_TOOL = {
    "name": "lookup",
    "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
}
_STATE = {
    "id": "rs_1",
    "type": "reasoning",
    "summary": [],
    "encrypted_content": "PRIVATE-ENCRYPTED-STATE",
}
_CALL = {
    "type": "function_call",
    "id": "fc_1",
    "call_id": "call_1",
    "name": "lookup",
    "arguments": '{"q":"PRIVATE-QUERY"}',
    "status": "completed",
}


def _message(text="hello", phase=None):
    result = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    if phase:
        result["phase"] = phase
    return result


def _response(output=None, **fields):
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 123,
        "model": _MODEL,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "output": [_STATE, _message()] if output is None else output,
        "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        **fields,
    }


@contextmanager
def _boundary(payloads=None, *, status=200, transport_error=None, raw=None):
    payloads = payloads or [_response()]
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        assert request.url.path == "/v1/responses"
        if transport_error:
            raise transport_error("PRIVATE-UPSTREAM", request=request)
        payload = payloads[min(len(calls) - 1, len(payloads) - 1)]
        return httpx.Response(
            status, content=raw or json.dumps(payload), headers={"content-type": "application/json"}
        )

    with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False) as http_client:
        with OpenAI(
            api_key="synthetic-openai-key",
            base_url="https://openai.invalid/v1",
            http_client=http_client,
            max_retries=0,
        ) as sdk:
            yield (
                OpenAIChatProvider(
                    api_key="synthetic-openai-key", default_model=_MODEL, timeout_s=3, client=sdk
                ),
                calls,
            )


def _route(provider, monkeypatch, store=None):
    monkeypatch.setenv("API_KEY", "synthetic-bootstrap")
    app = create_app()
    app.dependency_overrides[get_llm] = lambda: provider
    app.dependency_overrides[get_key_store] = lambda: store or InMemoryKeyStore()
    return TestClient(app, headers={"X-API-Key": "synthetic-bootstrap"})


@pytest.mark.parametrize("thinking,effort", [(True, "medium"), (False, "low")])
def test_text_request_and_legacy_response_through_real_sdk(monkeypatch, thinking, effort):
    with _boundary() as (provider, calls):
        client = _route(provider, monkeypatch)
        response = client.post(
            "/chat",
            json={
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                    {"role": "user", "content": "continue"},
                ],
                "model": _MODEL,
                "system": "Be concise",
                "max_tokens": 1000,
                "thinking": thinking,
            },
        )
    assert response.status_code == 200
    assert response.json() == {
        "content": "hello",
        "model": _MODEL,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }
    assert calls == [
        {
            "model": _MODEL,
            "input": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "user", "content": "continue"},
            ],
            "instructions": "Be concise",
            "max_output_tokens": 1000,
            "reasoning": {"effort": effort},
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
    ]


def test_parallel_tool_roundtrip_preserves_state_results_and_phase(monkeypatch, capsys):
    first = _response(
        [
            _STATE,
            _message("Checking", "commentary"),
            _CALL,
            {**_CALL, "id": "fc_2", "call_id": "call_2"},
        ]
    )
    with _boundary([first, _response([_message("Done", "final_answer")])]) as (provider, calls):
        client = _route(provider, monkeypatch)
        body = {"messages": [{"role": "user", "content": "PRIVATE-PROMPT"}], "tools": [_TOOL]}
        response = client.post("/chat", json=body)
        assert response.status_code == 200
        turn = response.json()
        assert turn["stop_reason"] == "tool_use"
        assert turn["content"] == "Checking"
        assert turn["content_blocks"][0] == {
            "type": "reasoning_state",
            "provider": "openai",
            "data": _STATE,
        }
        body["messages"] += [
            {"role": "assistant", "content": turn["content_blocks"]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_1",
                        "content": {"answer": "PRIVATE-RESULT"},
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_2",
                        "content": "PRIVATE-ERROR",
                        "is_error": True,
                    },
                    {"type": "text", "text": "Continue"},
                ],
            },
        ]
        final = client.post("/chat", json=body)
    assert final.status_code == 200
    assert final.json()["content_blocks"] == [
        {"type": "text", "text": "Done", "phase": "final_answer"}
    ]
    assert calls[0]["tools"] == [
        {"type": "function", "name": "lookup", "parameters": _TOOL["input_schema"], "strict": False}
    ]
    replay = calls[1]["input"]
    assert replay[1] == _STATE
    assert replay[2] == {"role": "assistant", "content": "Checking", "phase": "commentary"}
    assert replay[3]["call_id"] == "call_1"
    assert replay[4]["call_id"] == "call_2"
    assert replay[5] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": '{"answer":"PRIVATE-RESULT"}',
    }
    assert replay[6] == {
        "type": "function_call_output",
        "call_id": "call_2",
        "output": '{"content":"PRIVATE-ERROR","is_error":true}',
    }
    assert replay[7] == {"role": "user", "content": "Continue"}
    logs = capsys.readouterr()
    for secret in (
        "PRIVATE-PROMPT",
        "PRIVATE-ENCRYPTED-STATE",
        "PRIVATE-QUERY",
        "PRIVATE-RESULT",
        "PRIVATE-ERROR",
        "synthetic-openai-key",
    ):
        assert secret not in logs.out + logs.err


def test_truncated_parallel_calls_are_withheld_and_usage_retained(monkeypatch):
    payload = _response(
        [_STATE, _message("Partial"), _CALL, {**_CALL, "call_id": "call_2", "arguments": '{"q":'}],
        status="incomplete",
        incomplete_details={"reason": "max_output_tokens"},
    )
    with _boundary([payload]) as (provider, calls):
        response = _route(provider, monkeypatch).post(
            "/chat", json={"messages": [{"role": "user", "content": "hi"}], "tools": [_TOOL]}
        )
    assert response.status_code == 200
    data = response.json()
    assert data["stop_reason"] == "max_tokens"
    assert data["content"] == "Partial"
    assert data["usage"] == {"input_tokens": 11, "output_tokens": 7}
    assert [b["type"] for b in data["content_blocks"]] == ["reasoning_state", "text"]
    assert len(calls) == 1


@pytest.mark.parametrize(
    "output,fields,stop,content",
    [
        ([], {}, "end_turn", ""),
        (
            [],
            {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}},
            "content_filtered",
            "",
        ),
        (
            [{**_message(), "content": [{"type": "refusal", "refusal": "Cannot help"}]}],
            {},
            "refusal",
            "Cannot help",
        ),
    ],
)
def test_empty_filtered_and_refusal_responses(monkeypatch, output, fields, stop, content):
    with _boundary([_response(output, **fields)]) as (provider, _):
        response = _route(provider, monkeypatch).post(
            "/chat",
            json={"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]},
        )
    assert response.status_code == 200
    assert response.json()["stop_reason"] == stop
    assert response.json()["content"] == content


@pytest.mark.parametrize(
    "patch",
    [
        {"output": [{"type": "unknown", "private": "PRIVATE-UPSTREAM"}]},
        {"output": [{**_STATE, "encrypted_content": None}]},
        {"output": [{**_CALL, "arguments": "[]"}]},
        {"output": [{**_CALL, "arguments": '{"q":NaN}'}]},
        {"output": [{**_CALL, "arguments": '{"q":1,"q":2}'}]},
        {"output": [{**_CALL, "name": "undeclared"}]},
        {"output": [_CALL, _CALL]},
        {"output": [{**_CALL, "call_id": True}]},
        {"output": [{**_CALL, "unknown": True}]},
        {"output": [{**_CALL, "status": "incomplete"}]},
        {"usage": {"input_tokens": True, "output_tokens": 7}},
        {"usage": {"input_tokens": 11, "output_tokens": "7"}},
        {"status": "failed", "error": {"message": "PRIVATE-UPSTREAM"}},
        {"model": "unrequested-model"},
        {"unknown": "PRIVATE-UPSTREAM"},
    ],
)
def test_malformed_raw_outputs_fail_closed(monkeypatch, patch, capsys):
    with _boundary([_response(**patch)]) as (provider, _):
        response = _route(provider, monkeypatch).post(
            "/chat", json={"messages": [{"role": "user", "content": "hi"}], "tools": [_TOOL]}
        )
    assert response.status_code == 502
    assert response.json() == {"detail": "LLM provider error"}
    logs = capsys.readouterr()
    assert "PRIVATE-UPSTREAM" not in logs.out + logs.err


def test_duplicate_raw_envelope_fields_rejected(monkeypatch):
    raw = json.dumps(_response()).replace(
        '"output_tokens": 7', '"output_tokens": 7, "output_tokens": 8'
    )
    with _boundary(raw=raw) as (provider, _):
        response = _route(provider, monkeypatch).post(
            "/chat", json={"messages": [{"role": "user", "content": "hi"}]}
        )
    assert response.status_code == 502


@pytest.mark.parametrize(
    "patch,exception,location",
    [
        ({"PRIVATE-FIELD": "PRIVATE-UPSTREAM"}, "ValueError", "object_fields:"),
        (
            {"status": "incomplete", "incomplete_details": {"reason": "PRIVATE-REASON"}},
            "KeyError",
            "_result:",
        ),
    ],
)
def test_schema_failures_log_safe_diagnostics(monkeypatch, caplog, patch, exception, location):
    with _boundary([_response(**patch)]) as (provider, _):
        with caplog.at_level(logging.WARNING, logger="src.providers.openai_chat"):
            response = _route(provider, monkeypatch).post(
                "/chat", json={"messages": [{"role": "user", "content": "PRIVATE-PROMPT"}]}
            )
    assert response.status_code == 502
    assert response.json() == {"detail": "LLM provider error"}
    records = [r for r in caplog.records if r.name == "src.providers.openai_chat"]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert "stage=response_validation" in record.message
    assert f"exception={exception}" in record.message
    assert "upstream_status=200" in record.message
    assert location in record.message
    assert record.exc_info is None
    assert record.stack_info is None
    for private in (
        "PRIVATE-FIELD",
        "PRIVATE-UPSTREAM",
        "PRIVATE-REASON",
        "PRIVATE-PROMPT",
        "PRIVATE-ENCRYPTED-STATE",
        "synthetic-openai-key",
    ):
        assert private not in caplog.text + response.text


def test_invalid_json_logs_decode_location_without_response_body(monkeypatch, caplog):
    with _boundary(raw='{"PRIVATE-UPSTREAM":') as (provider, _):
        response = _route(provider, monkeypatch).post(
            "/chat", json={"messages": [{"role": "user", "content": "hi"}]}
        )
    assert response.status_code == 502
    assert response.json() == {"detail": "LLM provider error"}
    assert "stage=response_decode" in caplog.text
    assert "exception=JSONDecodeError" in caplog.text
    assert "decode_json_object:" in caplog.text
    assert "PRIVATE-UPSTREAM" not in caplog.text + response.text


def test_unexpected_exception_logs_no_exception_message(monkeypatch, caplog):
    with _boundary() as (provider, calls):
        monkeypatch.setattr(
            provider._client.responses.with_raw_response,
            "create",
            Mock(side_effect=RuntimeError("PRIVATE-PROMPT synthetic-openai-key")),
        )
        response = _route(provider, monkeypatch).post(
            "/chat", json={"messages": [{"role": "user", "content": "hi"}]}
        )
    assert calls == []
    assert response.status_code == 502
    assert response.json() == {"detail": "LLM provider error"}
    assert "stage=request" in caplog.text
    assert "exception=RuntimeError" in caplog.text
    assert "upstream_status=None" in caplog.text
    assert "PRIVATE-PROMPT" not in caplog.text + response.text
    assert "synthetic-openai-key" not in caplog.text + response.text


@pytest.mark.parametrize(
    "upstream,expected", [(429, 429), (400, 502), (401, 502), (403, 502), (500, 502)]
)
def test_upstream_errors_are_normalized_without_retries(monkeypatch, upstream, expected, caplog):
    with _boundary(
        [{"error": {"message": "PRIVATE-UPSTREAM", "type": "api_error"}}], status=upstream
    ) as (provider, calls):
        response = _route(provider, monkeypatch).post(
            "/chat", json={"messages": [{"role": "user", "content": "hi"}]}
        )
    assert response.status_code == expected
    assert "PRIVATE-UPSTREAM" not in response.text + caplog.text
    if expected == 502:
        assert response.json() == {"detail": "LLM provider error"}
        assert "stage=request" in caplog.text
        assert f"upstream_status={upstream}" in caplog.text
        assert "exception=" in caplog.text
        assert "locations=chat:" in caplog.text
    else:
        assert not [r for r in caplog.records if r.name == "src.providers.openai_chat"]
    assert len(calls) == 1


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError])
def test_connection_failures_are_504(monkeypatch, error):
    with _boundary(transport_error=error) as (provider, calls):
        response = _route(provider, monkeypatch).post(
            "/chat", json={"messages": [{"role": "user", "content": "hi"}]}
        )
    assert response.status_code == 504
    assert len(calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"model": "claude-sonnet-4-6"},
        {"max_tokens": 15},
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "reasoning", "text": "", "signature": "private"}],
                }
            ]
        },
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "missing", "content": "x"}],
                }
            ]
        },
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "reasoning_state", "data": {**_STATE, "unknown": "private"}}
                    ],
                }
            ]
        },
    ],
)
def test_invalid_input_rejected_before_inference(monkeypatch, body):
    with _boundary() as (provider, calls):
        response = _route(provider, monkeypatch).post(
            "/chat", json={"messages": [{"role": "user", "content": "hi"}], **body}
        )
    assert response.status_code == 422
    assert calls == []


def test_scope_rejected_before_inference(monkeypatch):
    store = InMemoryKeyStore()
    token, prefix, key_hash = generate_key()
    store.add(prefix=prefix, key_hash=key_hash, name="no-chat", scopes=["embed"])
    with _boundary() as (provider, calls):
        response = _route(provider, monkeypatch, store).post(
            "/chat",
            headers={"X-API-Key": token},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 403
    assert calls == []


def test_local_missing_key_is_503_and_health_stays_available(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("API_KEY", "synthetic-bootstrap")
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        response = client.post(
            "/chat",
            headers={"X-API-Key": "synthetic-bootstrap"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 503


def test_client_is_lazy_and_uses_configured_timeout_without_retries(monkeypatch):
    constructor = Mock()
    monkeypatch.setattr("openai.OpenAI", constructor)
    provider = OpenAIChatProvider(api_key="synthetic-key", default_model=_MODEL, timeout_s=17.5)
    constructor.assert_not_called()
    assert provider._get_client() is provider._get_client()
    constructor.assert_called_once_with(api_key="synthetic-key", timeout=17.5, max_retries=0)


@pytest.mark.parametrize(
    "block", [LLMReasoningBlock("", "signature"), LLMRedactedReasoningBlock("YQ==")]
)
def test_bedrock_state_rejected_at_reusable_provider_boundary(block):
    client = Mock()
    provider = OpenAIChatProvider(api_key="", default_model=_MODEL, timeout_s=1, client=client)
    with pytest.raises(ProviderInvalidRequest):
        provider.chat(LLMRequest(messages=[LLMMessage("assistant", [block])]))
    client.responses.with_raw_response.create.assert_not_called()


def test_invalid_default_and_override_models_fail_without_client_creation():
    with pytest.raises(ValueError):
        OpenAIChatProvider(api_key="", default_model="gpt-5.6-sol", timeout_s=1)
    provider = OpenAIChatProvider(api_key="", default_model=_MODEL, timeout_s=1)
    with pytest.raises(ProviderUnsupportedModel):
        provider.chat(LLMRequest(model="claude-sonnet-4-6"))
    with pytest.raises(ProviderNotConfigured):
        provider.chat(LLMRequest(messages=[LLMMessage("user", "hi")]))
