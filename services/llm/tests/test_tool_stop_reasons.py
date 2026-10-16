import json
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import httpx
import pytest
from anthropic import AnthropicBedrockMantle
from botocore.awsrequest import AWSResponse
from botocore.config import Config
from fastapi.testclient import TestClient

from platform_auth import InMemoryKeyStore

from contracts.tool_validation import MAX_CONTENT_BLOCKS
from src.api.app import create_app
from src.api.deps import get_key_store, get_llm
from src.providers.base import (
    LLMMessage,
    LLMReasoningBlock,
    LLMRedactedReasoningBlock,
    LLMRequest,
    LLMResult,
    LLMTextBlock,
    LLMTool,
    LLMToolUseBlock,
    ProviderUnavailable,
)
from src.providers.bedrock import BedrockClaudeProvider
from src.providers.bedrock_converse import BedrockConverseProvider


_TEXT = {"type": "text", "text": "Partial answer"}
_CALL = {
    "type": "tool_use",
    "id": "next_call",
    "name": "lookup",
    "input": {"query": "PRIVATE-ARGUMENT"},
}
_PARTIAL = {**_CALL, "id": "partial_call", "input": '{"query":"PRIVATE-PARTIAL'}
_TOOL = {
    "name": "lookup",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}


def _request():
    return {
        "thinking": False,
        "max_tokens": 7,
        "tools": [_TOOL],
        "messages": [
            {"role": "user", "content": "PRIVATE-PROMPT"},
            {"role": "assistant", "content": [{**_CALL, "id": "previous_call"}]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "previous_call",
                        "content": "PRIVATE-RESULT",
                    }
                ],
            },
        ],
    }


def _wire_response(backend, blocks, stop_reason):
    if backend == "converse":
        content = [
            {"text": block["text"]}
            if block["type"] == "text"
            else {
                "toolUse": {
                    "toolUseId": block["id"],
                    "name": block["name"],
                    "input": block["input"],
                }
            }
            for block in blocks
        ]
        return {
            "output": {"message": {"role": "assistant", "content": content}},
            "stopReason": stop_reason,
            "usage": {"inputTokens": 11, "outputTokens": 7, "totalTokens": 18},
            "metrics": {"latencyMs": 1},
        }
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "anthropic.claude-sonnet-4-6",
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


@pytest.fixture(params=["converse", "mantle"])
def wire_provider(request, monkeypatch):
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    wire = SimpleNamespace(backend=request.param, response=None, requests=[])
    if request.param == "converse":
        sdk = boto3.client(
            "bedrock-runtime",
            region_name="us-east-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
            config=Config(retries={"total_max_attempts": 1}),
        )

        def intercept(request, **kwargs):
            wire.requests.append(json.loads(request.body))
            raw = SimpleNamespace(stream=lambda: iter([json.dumps(wire.response).encode()]))
            return AWSResponse(request.url, 200, {"content-type": "application/json"}, raw)

        monkeypatch.setattr(sdk._endpoint.http_session, "send", intercept)
        provider_type = BedrockConverseProvider
    else:

        def intercept(request):
            wire.requests.append(json.loads(request.content))
            return httpx.Response(200, json=wire.response)

        sdk = AnthropicBedrockMantle(
            aws_region="us-east-1",
            api_key="test-mantle-key",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(intercept), trust_env=False),
        )
        provider_type = BedrockClaudeProvider
    provider = provider_type(
        aws_region="us-east-1", default_model="claude-sonnet-4-6", timeout_s=1, client=sdk
    )
    yield provider, wire
    sdk.close()


@pytest.fixture
def api_client(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-stop-reasons-key")
    app = create_app()
    app.dependency_overrides[get_key_store] = lambda: InMemoryKeyStore()
    with TestClient(app) as client:
        yield app, client, {"X-API-Key": "test-stop-reasons-key"}


@pytest.mark.parametrize(
    "stop_reason,blocks,expected",
    [
        pytest.param("end_turn", [], [], id="empty-completion"),
        pytest.param("end_turn", [_TEXT], [_TEXT], id="ordinary-completion"),
        pytest.param("tool_use", [_CALL], [_CALL], id="complete-tool-call"),
        pytest.param("max_tokens", [], [], id="empty-truncation"),
        pytest.param("max_tokens", [_TEXT], [_TEXT], id="text-truncation"),
        pytest.param("max_tokens", [_CALL], [], id="object-tool-truncation"),
        pytest.param("max_tokens", [{**_CALL, "input": {}}], [], id="incomplete-arguments"),
        pytest.param("max_tokens", [_PARTIAL], [], id="partial-json-tool-truncation"),
        pytest.param("max_tokens", [_CALL, _TEXT, _PARTIAL], [_TEXT], id="parallel-truncation"),
    ],
)
def test_stop_reason_and_usage_survive_real_sdk(
    wire_provider, api_client, capsys, stop_reason, blocks, expected
):
    provider, wire = wire_provider
    app, client, headers = api_client
    wire.response = _wire_response(wire.backend, blocks, stop_reason)
    app.dependency_overrides[get_llm] = lambda: provider

    response = client.post("/chat", headers=headers, json=_request())

    assert response.status_code == 200
    body = response.json()
    assert body["stop_reason"] == stop_reason
    assert body["model"] == (
        "us.anthropic.claude-sonnet-4-6"
        if wire.backend == "converse"
        else "anthropic.claude-sonnet-4-6"
    )
    assert body["usage"] == {"input_tokens": 11, "output_tokens": 7}
    assert body["content_blocks"] == expected
    assert body["content"] == "".join(b["text"] for b in expected if b["type"] == "text")
    assert len(wire.requests) == 1
    assert len(wire.requests[0]["messages"]) == 3
    logs = capsys.readouterr().out
    entries = [json.loads(line) for line in logs.splitlines() if line.startswith("{")]
    assert entries[-1]["status"] == 200
    assert entries[-1]["input_tokens"] == 11
    assert entries[-1]["output_tokens"] == 7
    assert "PRIVATE-" not in logs
    if stop_reason == "max_tokens":
        assert "PRIVATE-" not in response.text


@pytest.mark.parametrize(
    "failure",
    ["missing-content", "null-content", "usage", "tool-input", "empty-tool-use", "too-many-blocks"],
)
def test_malformed_responses_still_fail_closed(wire_provider, api_client, failure):
    provider, wire = wire_provider
    app, client, headers = api_client
    wire.response = _wire_response(
        wire.backend,
        [_CALL] * (MAX_CONTENT_BLOCKS + 1) if failure == "too-many-blocks" else [_PARTIAL],
        "tool_use" if failure in {"tool-input", "empty-tool-use"} else "max_tokens",
    )
    message = wire.response["output"]["message"] if wire.backend == "converse" else wire.response
    if failure == "missing-content":
        message.pop("content")
    elif failure == "null-content":
        message["content"] = None
    elif failure == "usage":
        wire.response["usage"]["inputTokens" if wire.backend == "converse" else "input_tokens"] = (
            True
        )
    elif failure == "empty-tool-use":
        message["content"] = []
    app.dependency_overrides[get_llm] = lambda: provider

    response = client.post("/chat", headers=headers, json=_request())

    assert response.status_code == 502
    assert response.json() == {"detail": "LLM provider error"}
    assert len(wire.requests) == 1


def test_empty_request_content_stays_invalid(wire_provider, api_client):
    provider, wire = wire_provider
    app, client, headers = api_client
    app.dependency_overrides[get_llm] = lambda: provider
    body = {**_request(), "messages": [{"role": "user", "content": []}]}

    assert client.post("/chat", headers=headers, json=body).status_code == 422
    with pytest.raises(ProviderUnavailable):
        provider.chat(LLMRequest(messages=[LLMMessage("user", [])], tools=[LLMTool(**_TOOL)]))
    assert wire.requests == []


@pytest.mark.parametrize(
    "stop_reason,blocks,status",
    [
        pytest.param("end_turn", [], 200, id="empty-completion"),
        pytest.param("max_tokens", [], 200, id="empty-truncation"),
        pytest.param("max_tokens", None, 502, id="null-is-not-empty"),
        pytest.param(
            "max_tokens",
            [LLMToolUseBlock("call", "lookup", {})] * (MAX_CONTENT_BLOCKS + 1),
            502,
            id="bound-before-filtering",
        ),
    ],
)
def test_router_validates_response_lists(api_client, stop_reason, blocks, status):
    app, client, headers = api_client
    provider = Mock()
    provider.chat.return_value = LLMResult("", "test-model", stop_reason, 11, 7, blocks)
    app.dependency_overrides[get_llm] = lambda: provider

    response = client.post("/chat", headers=headers, json=_request())

    assert response.status_code == status
    if status == 200:
        assert response.json()["content_blocks"] == []
        assert response.json()["usage"] == {"input_tokens": 11, "output_tokens": 7}
    else:
        assert response.json() == {"detail": "LLM provider error"}


def test_router_never_exposes_truncated_tool_calls(api_client):
    app, client, headers = api_client
    provider = Mock()
    blocks = [
        LLMReasoningBlock("", "signature"),
        LLMToolUseBlock("next_call", "lookup", {"query": "PRIVATE-ARGUMENT"}),
        LLMTextBlock("Partial answer"),
        LLMRedactedReasoningBlock("cHJpdmF0ZQ=="),
        LLMToolUseBlock("partial_call", "lookup", '{"query":"PRIVATE-PARTIAL'),
    ]
    provider.chat.return_value = LLMResult(
        "Partial answer", "test-model", "max_tokens", 11, 7, blocks
    )
    app.dependency_overrides[get_llm] = lambda: provider

    response = client.post("/chat", headers=headers, json=_request())

    assert response.status_code == 200
    assert response.json()["stop_reason"] == "max_tokens"
    assert response.json()["content_blocks"] == [
        {"type": "reasoning", "text": "", "signature": "signature"},
        _TEXT,
        {"type": "redacted_reasoning", "data": "cHJpdmF0ZQ=="},
    ]
    assert "PRIVATE-" not in response.text
    assert len(blocks) == 5
