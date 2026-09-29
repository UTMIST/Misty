import base64
import json
import logging
import socket
from collections import deque
from contextlib import ExitStack
from copy import deepcopy
from functools import partial
from unittest.mock import Mock

import anthropic
import boto3
import httpx
import pytest
from botocore.config import Config
from botocore.exceptions import ReadTimeoutError
from botocore.stub import Stubber
from fastapi.testclient import TestClient

from platform_auth import InMemoryKeyStore

from contracts.tool_validation import MAX_JSON_BYTES, MAX_TOOLS
from src.providers.bedrock import BedrockClaudeProvider
from src.providers.bedrock_converse import BedrockConverseProvider

MODEL = "claude-sonnet-4-6"
API_KEY = "offline-roundtrip-api-key"
AWS_ACCESS_KEY = "offline-roundtrip-access-key"
AWS_SECRET_KEY = "offline-roundtrip-secret-key"
AWS_SESSION_TOKEN = "offline-roundtrip-session-token"
PROMPT = "PRIVATE-ROUNDTRIP-PROMPT"
SYSTEM = "PRIVATE-ROUNDTRIP-SYSTEM"
ARGUMENT = "PRIVATE-ROUNDTRIP-ARGUMENT"
REASONING = "PRIVATE-ROUNDTRIP-REASONING"
SIGNATURE = "PRIVATE-ROUNDTRIP-SIGNED-THINKING+/="
UPSTREAM_ERROR = "PRIVATE-ROUNDTRIP-UPSTREAM-ERROR"
REDACTED_BYTES = b"\x00\xff\x80PRIVATE-ROUNDTRIP-OPAQUE\x00"
REDACTED_DATA = base64.b64encode(REDACTED_BYTES).decode("ascii")
TOOLS = [
    {
        "name": "lookup",
        "description": "PRIVATE-ROUNDTRIP-TOOL-DESCRIPTION",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {"name": "check", "input_schema": {"type": "object"}},
]


def _tool_use(index=0, **overrides):
    return {
        "type": "tool_use",
        "id": f"call.lookup-{index}:part.v1",
        "name": "lookup" if index % 2 == 0 else "check",
        "input": {"query": ARGUMENT, "options": [False, 0, None, {"city": "Montréal"}]},
    } | overrides


def _tool_result(index=0, content=None, *, is_error=False):
    return {
        "type": "tool_result",
        "tool_use_id": _tool_use(index)["id"],
        "content": content,
        "is_error": is_error,
    }


def _request(**overrides):
    return {
        "messages": [{"role": "user", "content": PROMPT}],
        "tools": deepcopy(TOOLS),
        "system": SYSTEM,
        "model": MODEL,
        "max_tokens": 256,
        "thinking": True,
    } | overrides


def _history(calls, results):
    return [
        {"role": "user", "content": PROMPT},
        {"role": "assistant", "content": calls},
        {"role": "user", "content": results},
    ]


def _wire_block(kind, block):
    block_type = block["type"]
    if kind == "mantle":
        if block_type == "reasoning":
            return {
                "type": "thinking",
                "thinking": block["text"],
                "signature": block["signature"],
            }
        if block_type == "redacted_reasoning":
            return {"type": "redacted_thinking", "data": block["data"]}
        if block_type == "tool_result":
            return block | {"content": json.dumps(block["content"], ensure_ascii=False)}
        return deepcopy(block)
    if block_type == "text":
        return {"text": block["text"]}
    if block_type == "reasoning":
        return {
            "reasoningContent": {
                "reasoningText": {"text": block["text"], "signature": block["signature"]}
            }
        }
    if block_type == "redacted_reasoning":
        return {"reasoningContent": {"redactedContent": base64.b64decode(block["data"])}}
    if block_type == "tool_use":
        return {
            "toolUse": {
                "toolUseId": block["id"],
                "name": block["name"],
                "input": deepcopy(block["input"]),
            }
        }
    if block_type == "tool_result":
        return {
            "toolResult": {
                "toolUseId": block["tool_use_id"],
                "content": [{"json": deepcopy(block["content"])}],
                "status": "error" if block.get("is_error", False) else "success",
            }
        }
    raise AssertionError(f"unhandled test block type: {block_type}")


def _assert_same_value(actual, expected):
    assert type(actual) is type(expected)
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys()
        for key, value in expected.items():
            _assert_same_value(actual[key], value)
    elif isinstance(expected, list):
        assert len(actual) == len(expected)
        for actual_item, expected_item in zip(actual, expected, strict=True):
            _assert_same_value(actual_item, expected_item)
    else:
        assert actual == expected


def _normalize_results(kind, messages):
    normalized = deepcopy(messages)
    for message in normalized:
        if not isinstance(message["content"], list):
            continue
        for block in message["content"]:
            if kind == "mantle" and block.get("type") == "tool_result":
                content = block["content"]
                if isinstance(content, list):
                    assert all(part["type"] == "text" for part in content)
                    content = "".join(part["text"] for part in content)
                block["content"] = json.loads(content)
                block.setdefault("is_error", False)
            elif kind == "converse" and "toolResult" in block:
                result = block["toolResult"]
                assert len(result["content"]) == 1
                part = result["content"][0]
                if "text" in part:
                    result["content"] = [{"json": json.loads(part["text"])}]
                result.setdefault("status", "success")
    return normalized


def _assert_request_sent(backend, body):
    sent = backend.calls[-1]
    expected_messages = []
    for message in body["messages"]:
        content = message["content"]
        if isinstance(content, list):
            content = [_wire_block(backend.kind, block) for block in content]
        elif backend.kind == "converse":
            content = [{"text": content}]
        expected_messages.append({"role": message["role"], "content": content})
    _assert_same_value(
        _normalize_results(backend.kind, sent["messages"]),
        _normalize_results(backend.kind, expected_messages),
    )
    tools = body.get("tools") or []
    if backend.kind == "converse":
        assert sent["modelId"] == backend.model
        assert sent["inferenceConfig"]["maxTokens"] == body["max_tokens"]
        assert sent["system"] == [{"text": body["system"]}]
        if body.get("thinking", True):
            assert sent["additionalModelRequestFields"] == {"thinking": {"type": "adaptive"}}
        else:
            assert "additionalModelRequestFields" not in sent
        if tools:
            specs = [entry["toolSpec"] for entry in sent["toolConfig"]["tools"]]
            expected = [
                {"name": tool["name"], "inputSchema": {"json": tool["input_schema"]}}
                | ({"description": tool["description"]} if "description" in tool else {})
                for tool in tools
            ]
        else:
            assert "toolConfig" not in sent
            return
    else:
        assert sent["model"] == backend.model
        assert sent["max_tokens"] == body["max_tokens"]
        assert sent["system"] == body["system"]
        if body.get("thinking", True):
            assert sent["thinking"] == {"type": "adaptive"}
        else:
            assert "thinking" not in sent
        if tools:
            specs = sent["tools"]
            expected = tools
        else:
            assert "tools" not in sent
            return
    _assert_same_value(
        sorted(specs, key=lambda tool: tool["name"]),
        sorted(expected, key=lambda tool: tool["name"]),
    )


class _OfflineBackend:
    def __init__(self, kind, stack):
        self.kind = kind
        self.calls = []
        self.responses = deque()
        if kind == "converse":
            self.model = f"us.anthropic.{MODEL}"
            self.sdk = boto3.Session(
                aws_access_key_id=AWS_ACCESS_KEY,
                aws_secret_access_key=AWS_SECRET_KEY,
                aws_session_token=AWS_SESSION_TOKEN,
                region_name="us-east-1",
            ).client(
                "bedrock-runtime",
                endpoint_url="https://bedrock-runtime.offline.invalid",
                config=Config(proxies={}, retries={"total_max_attempts": 1}),
            )
            stack.callback(self.sdk.close)
            self.stubber = stack.enter_context(Stubber(self.sdk))
            self.sdk.meta.events.register(
                "before-parameter-build.bedrock-runtime.Converse", self._capture_converse
            )
            provider_type = BedrockConverseProvider
        else:
            self.model = f"anthropic.{MODEL}"
            http_client = httpx.Client(
                transport=httpx.MockTransport(self._transport), trust_env=False
            )
            self.sdk = anthropic.AnthropicBedrockMantle(
                aws_access_key=AWS_ACCESS_KEY,
                aws_secret_key=AWS_SECRET_KEY,
                aws_session_token=AWS_SESSION_TOKEN,
                aws_region="us-east-1",
                base_url="https://bedrock-mantle.offline.invalid",
                max_retries=0,
                http_client=http_client,
            )
            self.sdk.with_options = partial(self.sdk.with_options, http_client=http_client)
            stack.callback(self.sdk.close)
            provider_type = BedrockClaudeProvider
        self.provider = provider_type(
            aws_region="us-east-1", default_model=MODEL, timeout_s=1.0, client=self.sdk
        )
        self.provider.chat = Mock(wraps=self.provider.chat)

    def _capture_converse(self, params, **kwargs):
        self.calls.append(deepcopy(params))

    def _transport(self, request):
        self.calls.append(json.loads(request.content))
        assert self.responses, "unexpected extra SDK request"
        status, body = self.responses.popleft()
        if status is None:
            raise httpx.ReadTimeout(UPSTREAM_ERROR, request=request)
        return httpx.Response(status, json=body)

    def reply(self, blocks, *, stop_reason="end_turn"):
        content = [_wire_block(self.kind, block) for block in blocks]
        if self.kind == "converse":
            self.stubber.add_response(
                "converse",
                {
                    "output": {"message": {"role": "assistant", "content": content}},
                    "stopReason": stop_reason,
                    "usage": {"inputTokens": 7, "outputTokens": 11, "totalTokens": 18},
                    "metrics": {"latencyMs": 1},
                },
            )
        else:
            self.responses.append(
                (
                    200,
                    {
                        "id": "msg_offline_roundtrip",
                        "type": "message",
                        "role": "assistant",
                        "model": self.model,
                        "content": content,
                        "stop_reason": stop_reason,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 7, "output_tokens": 11},
                    },
                )
            )

    def fail(self, failure):
        if self.kind == "converse":
            if failure == "timeout":
                self.stubber.deactivate()

                def timeout(**kwargs):
                    raise ReadTimeoutError(
                        endpoint_url="https://offline.invalid", error=UPSTREAM_ERROR
                    )

                self.sdk.meta.events.register("before-call.bedrock-runtime.Converse", timeout)
            else:
                code, status = (
                    ("ThrottlingException", 429)
                    if failure == "rate_limit"
                    else ("InternalServerException", 500)
                )
                self.stubber.add_client_error(
                    "converse", code, UPSTREAM_ERROR, http_status_code=status
                )
        else:
            status = {"rate_limit": 429, "timeout": None, "unavailable": 500}[failure]
            self.responses.append(
                (
                    status,
                    {
                        "type": "error",
                        "error": {"type": "api_error", "message": UPSTREAM_ERROR},
                    },
                )
            )

    def post(self, body, *, headers=None):
        headers = {"X-API-Key": API_KEY} if headers is None else headers
        return self.client.post(
            "/chat",
            headers=headers | {"Content-Type": "application/json"},
            content=json.dumps(body, ensure_ascii=True, allow_nan=True),
        )

    def assert_finished(self):
        if self.kind == "converse":
            self.stubber.assert_no_pending_responses()
        else:
            assert not self.responses


@pytest.fixture(autouse=True)
def _offline_environment(monkeypatch, _clear_settings_cache):
    for name, value in {
        "LLM_ENV": "local",
        "API_KEY": API_KEY,
        "CONSUMER_KEYS": "",
        "LLM_MODEL": MODEL,
        "THINKING_DEFAULT": "true",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": AWS_ACCESS_KEY,
        "AWS_SECRET_ACCESS_KEY": AWS_SECRET_KEY,
        "AWS_SESSION_TOKEN": AWS_SESSION_TOKEN,
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
    }.items():
        monkeypatch.setenv(name, value)
    for name in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_BEARER_TOKEN_BEDROCK",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AWS_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    def deny_network(*args, **kwargs):
        pytest.fail("real network access is forbidden in tool roundtrip tests")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)


@pytest.fixture(params=["converse", "mantle"])
def backend(request):
    from src.api.app import create_app
    from src.api.deps import get_key_store, get_llm

    with ExitStack() as stack:
        backend = _OfflineBackend(request.param, stack)
        backend.store = InMemoryKeyStore()
        app = create_app()
        app.dependency_overrides[get_key_store] = lambda: backend.store
        app.dependency_overrides[get_llm] = lambda: backend.provider
        backend.client = stack.enter_context(TestClient(app))
        yield backend


def _assert_private_logs(capsys, caplog, *private_values):
    captured = capsys.readouterr()
    logs = captured.out + captured.err + caplog.text
    for value in (
        API_KEY,
        AWS_ACCESS_KEY,
        AWS_SECRET_KEY,
        AWS_SESSION_TOKEN,
        PROMPT,
        SYSTEM,
        ARGUMENT,
        REASONING,
        SIGNATURE,
        REDACTED_DATA,
        "PRIVATE-ROUNDTRIP-OPAQUE",
        TOOLS[0]["description"],
        UPSTREAM_ERROR,
        *private_values,
    ):
        assert value not in logs
    return [json.loads(line) for line in captured.out.splitlines() if line.startswith("{")]


@pytest.mark.parametrize("with_text", [False, True], ids=["tool-only", "mixed"])
def test_parallel_tool_roundtrip_preserves_order_and_json_values(
    backend, with_text, capsys, caplog
):
    caplog.set_level(logging.INFO)
    calls = [_tool_use(index) for index in range(5)]
    blocks = [
        {"type": "reasoning", "text": REASONING, "signature": SIGNATURE},
        {"type": "redacted_reasoning", "data": REDACTED_DATA},
        {"type": "text", "text": "PRIVATE-VISIBLE-BEFORE|"},
        calls[0],
        {"type": "text", "text": "|PRIVATE-VISIBLE-AFTER"},
        *calls[1:],
    ]
    if not with_text:
        blocks = [block for block in blocks if block["type"] != "text"]
    first_body = _request()
    backend.reply(blocks, stop_reason="tool_use")
    first = backend.post(first_body)
    assert first.status_code == 200, first.text
    assert first.json() == {
        "content": "PRIVATE-VISIBLE-BEFORE||PRIVATE-VISIBLE-AFTER" if with_text else "",
        "model": backend.model,
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 7, "output_tokens": 11},
        "content_blocks": blocks,
    }
    _assert_same_value(first.json()["content_blocks"], blocks)
    assert backend.provider.chat.call_count == len(backend.calls) == 1
    _assert_request_sent(backend, first_body)
    backend.assert_finished()

    values = [
        False,
        0,
        None,
        {"items": [False, 0, None, {"text": "PRIVATE-RESULT-Montréal"}], "empty": []},
        {"error": "PRIVATE-RESULT-ERROR", "retry": False},
    ]
    results = [
        _tool_result(index, values[index], is_error=index == 4) for index in reversed(range(5))
    ]
    results[-1].pop("is_error")
    second_body = _request(
        messages=[
            *first_body["messages"],
            {"role": "assistant", "content": first.json()["content_blocks"]},
            {
                "role": "user",
                "content": [*results, {"type": "text", "text": "PRIVATE-RESULT-CONTEXT"}],
            },
        ]
    )
    final_blocks = [{"type": "text", "text": "PRIVATE-FINAL-"}, {"type": "text", "text": "ANSWER"}]
    backend.reply(final_blocks)
    second = backend.post(second_body)
    assert second.status_code == 200, second.text
    assert second.json() == {
        "content": "PRIVATE-FINAL-ANSWER",
        "model": backend.model,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 7, "output_tokens": 11},
        "content_blocks": final_blocks,
    }
    assert backend.provider.chat.call_count == len(backend.calls) == 2
    _assert_request_sent(backend, second_body)
    backend.assert_finished()
    audit = _assert_private_logs(
        capsys,
        caplog,
        "PRIVATE-VISIBLE-BEFORE",
        "PRIVATE-VISIBLE-AFTER",
        "PRIVATE-RESULT-Montréal",
        "PRIVATE-RESULT-ERROR",
        "PRIVATE-RESULT-CONTEXT",
        "PRIVATE-FINAL-",
    )
    assert [entry["status"] for entry in audit] == [200, 200]
    assert all(entry["key_name"] == "env-bootstrap" for entry in audit)
    assert all(entry["input_tokens"] == 7 and entry["output_tokens"] == 11 for entry in audit)


@pytest.mark.parametrize("tools_setting", ["absent", None, []], ids=["absent", "null", "empty"])
def test_legacy_text_response_bytes_are_unchanged(backend, tools_setting):
    body = _request(
        messages=[
            {"role": "user", "content": PROMPT},
            {"role": "assistant", "content": "previous reply"},
            {"role": "user", "content": "next question"},
        ]
    )
    body.pop("thinking")
    if tools_setting == "absent":
        body.pop("tools")
    else:
        body["tools"] = tools_setting
    backend.reply(
        [
            {"type": "reasoning", "text": REASONING, "signature": SIGNATURE},
            {"type": "text", "text": "héllo "},
            {"type": "redacted_reasoning", "data": REDACTED_DATA},
            {"type": "text", "text": 'world\n"done"'},
        ]
    )
    response = backend.post(body)
    assert response.status_code == 200, response.text
    expected = (
        b'{"content":"h\xc3\xa9llo world\\n\\"done\\"","model":"MODEL",'
        b'"stop_reason":"end_turn","usage":{"input_tokens":7,"output_tokens":11}}'
    ).replace(b"MODEL", backend.model.encode("ascii"))
    assert response.content == expected
    assert backend.provider.chat.call_count == len(backend.calls) == 1
    _assert_request_sent(backend, body)
    backend.assert_finished()


@pytest.mark.parametrize(
    "trigger", ["tools", "structured-absent", "structured-null", "structured-empty"]
)
def test_extended_text_response_without_tool_calls(backend, trigger):
    body = _request()
    if trigger != "tools":
        body["messages"][0]["content"] = [{"type": "text", "text": PROMPT}]
        if trigger == "structured-absent":
            body.pop("tools")
        else:
            body["tools"] = None if trigger == "structured-null" else []
    blocks = [{"type": "text", "text": "a visible completion"}]
    backend.reply(blocks)
    response = backend.post(body)
    assert response.status_code == 200, response.text
    assert response.json()["content"] == "a visible completion"
    assert response.json()["content_blocks"] == blocks
    _assert_request_sent(backend, body)
    assert backend.provider.chat.call_count == len(backend.calls) == 1
    backend.assert_finished()


def test_completed_tool_history_without_thinking_reaches_sdk(backend):
    body = _request(messages=_history([_tool_use()], [_tool_result(content=False)]), thinking=False)
    blocks = [{"type": "text", "text": "completed without thinking"}]
    backend.reply(blocks)
    response = backend.post(body)
    assert response.status_code == 200, response.text
    assert response.json()["content"] == "completed without thinking"
    assert response.json()["content_blocks"] == blocks
    _assert_request_sent(backend, body)
    assert backend.provider.chat.call_count == len(backend.calls) == 1
    backend.assert_finished()


def _invalid_requests():
    call = _tool_use()
    result = _tool_result(content={"value": False})
    tool_cases = {
        "tools-not-list": {"name": "lookup"},
        "invalid-tool-name": [TOOLS[0] | {"name": "bad.name"}],
        "duplicate-tool-names": [TOOLS[0], TOOLS[0]],
        "schema-not-object": [TOOLS[0] | {"input_schema": {"type": "array"}}],
        "schema-properties-not-object": [
            TOOLS[0] | {"input_schema": {"type": "object", "properties": []}}
        ],
        "schema-duplicate-required": [
            TOOLS[0] | {"input_schema": {"type": "object", "required": ["query", "query"]}}
        ],
        "too-many-tools": [TOOLS[0] | {"name": f"tool_{index}"} for index in range(MAX_TOOLS + 1)],
        "oversized-schema": [
            TOOLS[0] | {"input_schema": {"type": "object", "description": "x" * MAX_JSON_BYTES}}
        ],
    }
    for case, tools in tool_cases.items():
        yield pytest.param(_request(tools=tools), id=case)
    message_cases = {
        "empty-block-list": [{"role": "user", "content": []}],
        "unknown-block": [{"role": "user", "content": [{"type": "unknown", "text": PROMPT}]}],
        "user-tool-use": [{"role": "user", "content": [call]}],
        "assistant-tool-result": [{"role": "assistant", "content": [result]}],
        "user-reasoning": [
            {
                "role": "user",
                "content": [{"type": "reasoning", "text": REASONING, "signature": SIGNATURE}],
            }
        ],
        "unsigned-reasoning": [
            {"role": "assistant", "content": [{"type": "reasoning", "text": REASONING}]}
        ],
        "invalid-base64": [
            {
                "role": "assistant",
                "content": [{"type": "redacted_reasoning", "data": "not base64!"}],
            }
        ],
        "invalid-tool-id": _history(
            [call | {"id": "bad id"}], [result | {"tool_use_id": "bad id"}]
        ),
        "non-object-tool-input": _history([call | {"input": [ARGUMENT]}], [result]),
        "undeclared-tool-name": _history([call | {"name": "unknown"}], [result]),
        "orphan-result": [{"role": "user", "content": [result]}],
        "wrong-result-id": _history([call], [result | {"tool_use_id": "other.id"}]),
        "duplicate-call-id": _history([call, call], [result]),
        "duplicate-result-id": _history([call], [result, result]),
        "missing-parallel-result": _history([call, _tool_use(1)], [result]),
        "interrupted-tool-turn": _history([call], [{"type": "text", "text": PROMPT}]),
        "non-finite-result": _history([call], [result | {"content": {"nested": [float("nan")]}}]),
        "non-finite-input": _history([call | {"input": {"query": float("inf")}}], [result]),
        "oversized-result": _history([call], [result | {"content": "x" * MAX_JSON_BYTES}]),
    }
    for case, messages in message_cases.items():
        yield pytest.param(_request(messages=messages, thinking=False), id=case)


@pytest.mark.parametrize("body", list(_invalid_requests()))
def test_malformed_input_is_denied_before_concrete_provider_or_sdk(backend, body, capsys, caplog):
    caplog.set_level(logging.INFO)
    response = backend.post(body)
    assert response.status_code == 422, response.text
    backend.provider.chat.assert_not_called()
    assert backend.calls == []
    _assert_private_logs(capsys, caplog)


@pytest.mark.parametrize("status", [401, 403])
def test_tool_requests_require_auth_and_scope_before_sdk(backend, status):
    headers = {}
    if status == 403:
        from src.api.hashing import generate_key

        plaintext, prefix, key_hash = generate_key()
        backend.store.add(prefix=prefix, key_hash=key_hash, name="no-chat-scope", scopes=[])
        headers["X-API-Key"] = plaintext
    response = backend.post(_request(), headers=headers)
    assert response.status_code == status, response.text
    backend.provider.chat.assert_not_called()
    assert backend.calls == []


@pytest.mark.parametrize(
    "failure,status,detail",
    [
        ("rate_limit", 429, "LLM provider rate limited"),
        ("timeout", 504, "LLM provider timeout"),
        ("unavailable", 502, "LLM provider error"),
    ],
)
def test_sdk_failures_are_sanitized_without_tool_data_leakage(
    backend, failure, status, detail, capsys, caplog
):
    caplog.set_level(logging.INFO)
    backend.fail(failure)
    response = backend.post(_request())
    assert response.status_code == status, response.text
    assert response.json() == {"detail": detail}
    assert backend.provider.chat.call_count == len(backend.calls) == 1
    backend.assert_finished()
    audit = _assert_private_logs(capsys, caplog)
    assert audit[-1]["status"] == status
    assert "input_tokens" not in audit[-1]
    assert "output_tokens" not in audit[-1]


@pytest.mark.parametrize(
    "case",
    [
        "undeclared-name",
        "no-tools-advertised",
        "duplicate-id",
        "invalid-id",
        "non-object-input",
        "assistant-result",
        "tool-stop-without-call",
        "call-without-tool-stop",
    ],
)
def test_unexpected_upstream_tool_outputs_are_sanitized_502(backend, case, capsys, caplog):
    caplog.set_level(logging.INFO)
    body = _request(thinking=False)
    blocks = [_tool_use()]
    stop_reason = "tool_use"
    if case == "undeclared-name":
        blocks[0]["name"] = "unadvertised"
    elif case == "no-tools-advertised":
        body.pop("tools")
        body["messages"][0]["content"] = [{"type": "text", "text": PROMPT}]
    elif case == "duplicate-id":
        blocks.append(deepcopy(blocks[0]))
    elif case == "invalid-id":
        blocks[0]["id"] = "not a valid id"
    elif case == "non-object-input":
        blocks[0]["input"] = [UPSTREAM_ERROR]
    elif case == "assistant-result":
        blocks = [_tool_result(content={"error": UPSTREAM_ERROR})]
        stop_reason = "end_turn"
    elif case == "tool-stop-without-call":
        blocks = [{"type": "text", "text": UPSTREAM_ERROR}]
    elif case == "call-without-tool-stop":
        stop_reason = "end_turn"
    backend.reply(blocks, stop_reason=stop_reason)
    response = backend.post(body)
    assert response.status_code == 502, response.text
    assert response.json() == {"detail": "LLM provider error"}
    assert backend.provider.chat.call_count == len(backend.calls) == 1
    backend.assert_finished()
    audit = _assert_private_logs(capsys, caplog)
    assert audit[-1]["status"] == 502
    assert "input_tokens" not in audit[-1]
    assert "output_tokens" not in audit[-1]


def _complete_thinking_tool_exchange(backend, blocks, *, thinking):
    first_body = _request(thinking=thinking)
    backend.reply(blocks, stop_reason="tool_use")
    first = backend.post(first_body)
    assert backend.provider.chat.call_count == len(backend.calls) == 1
    _assert_request_sent(backend, first_body)
    backend.assert_finished()
    assert first.status_code == 200, first.text
    assert first.json()["content"] == ""
    assert first.json()["stop_reason"] == "tool_use"
    _assert_same_value(first.json()["content_blocks"], blocks)
    echoed_blocks = first.json()["content_blocks"]
    results = [
        {
            "type": "tool_result",
            "tool_use_id": block["id"],
            "content": {"found": True},
            "is_error": False,
        }
        for block in echoed_blocks
        if block["type"] == "tool_use"
    ]
    second_body = _request(
        thinking=thinking,
        messages=[
            *first_body["messages"],
            {"role": "assistant", "content": echoed_blocks},
            {"role": "user", "content": results},
        ],
    )
    final_blocks = [{"type": "text", "text": "Finished the previous question."}]
    backend.reply(final_blocks)
    second = backend.post(second_body)
    assert second.status_code == 200, (
        f"Unmodified echo rejected: first={first.status_code}, replay={second.status_code}, "
        f"SDK calls={len(backend.calls)}, body={second.text}"
    )
    assert second.json()["content"] == final_blocks[0]["text"]
    assert second.json()["stop_reason"] == "end_turn"
    _assert_same_value(second.json()["content_blocks"], final_blocks)
    assert backend.provider.chat.call_count == len(backend.calls) == 2
    _assert_request_sent(backend, second_body)
    backend.assert_finished()
    return [
        *second_body["messages"],
        {"role": "assistant", "content": second.json()["content_blocks"]},
    ]


def test_interleaved_reasoning_tool_blocks_are_preserved_and_replayable(backend):
    blocks = [
        {"type": "reasoning", "text": REASONING, "signature": SIGNATURE},
        _tool_use(0),
        {"type": "reasoning", "text": f"{REASONING}-second", "signature": f"{SIGNATURE}-second"},
        _tool_use(1),
    ]
    _complete_thinking_tool_exchange(backend, blocks, thinking=True)


def test_adaptive_tool_only_response_is_replayable_without_invented_reasoning(backend):
    _complete_thinking_tool_exchange(backend, [_tool_use()], thinking=True)


def test_thinking_can_start_after_a_completed_non_thinking_tool_exchange(backend):
    completed = _complete_thinking_tool_exchange(backend, [_tool_use()], thinking=False)
    body = _request(
        thinking=True,
        messages=[
            *completed,
            {"role": "user", "content": "Now answer an unrelated ordinary question."},
        ],
    )
    blocks = [
        {"type": "reasoning", "text": REASONING, "signature": SIGNATURE},
        {"type": "text", "text": "An answer from the new thinking-enabled turn."},
    ]
    backend.reply(blocks)
    response = backend.post(body)
    assert response.status_code == 200, (
        f"New assistant turn rejected: status={response.status_code}, "
        f"SDK calls={len(backend.calls)}, body={response.text}"
    )
    assert response.json()["content"] == blocks[1]["text"]
    assert response.json()["stop_reason"] == "end_turn"
    _assert_same_value(response.json()["content_blocks"], blocks)
    assert backend.provider.chat.call_count == len(backend.calls) == 3
    _assert_request_sent(backend, body)
    backend.assert_finished()
