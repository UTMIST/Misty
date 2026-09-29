import base64
import json
import logging
import socket
from collections import deque
from contextlib import ExitStack
from copy import deepcopy
from typing import get_args

import anthropic
import boto3
import httpx
import pytest
from anthropic.types.stop_reason import StopReason
from botocore.awsrequest import AWSResponse
from botocore.config import Config
from botocore.exceptions import ReadTimeoutError
from fastapi.testclient import TestClient

from platform_auth import InMemoryKeyStore

from contracts.tool_validation import MAX_CONTENT_BLOCKS, MAX_JSON_BYTES, MAX_TOOL_PAYLOAD_BYTES
from src.providers.base import LLMMessage, LLMRequest, LLMTool, ProviderUnavailable
from src.providers.bedrock import BedrockClaudeProvider
from src.providers.bedrock_converse import BedrockConverseProvider

MODEL = "claude-sonnet-4-6"
API_KEY = "offline-wire-api-key"
AWS_ACCESS_KEY = "offline-wire-access-key"
AWS_SECRET_KEY = "offline-wire-secret-key"
AWS_SESSION_TOKEN = "offline-wire-session-token"
PRIVATE = "PRIVATE-WIRE-PAYLOAD"
SIGNATURE = "  opaque+/= signed\n雪\t"
REDACTED = base64.b64encode(b"\x00\xff\x80\x01opaque\x00").decode("ascii")


@pytest.fixture(autouse=True)
def _wire_offline(monkeypatch, _clear_settings_cache):
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
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)

    def deny_network(*args, **kwargs):
        pytest.fail("real network is forbidden in wire regression tests")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)


class _RawBody:
    def __init__(self, body):
        self.body = body

    def stream(self):
        yield self.body


class _WireBackend:
    def __init__(self, kind, stack, monkeypatch):
        from src.api.app import create_app
        from src.api.deps import get_key_store, get_llm

        self.kind = kind
        self.calls = []
        self.replies = deque()
        if kind == "converse":
            self.model = f"us.anthropic.{MODEL}"
            self.sdk = boto3.Session(
                aws_access_key_id=AWS_ACCESS_KEY,
                aws_secret_access_key=AWS_SECRET_KEY,
                aws_session_token=AWS_SESSION_TOKEN,
                region_name="us-east-1",
            ).client(
                "bedrock-runtime",
                endpoint_url="https://converse.wire.invalid",
                config=Config(proxies={}, retries={"total_max_attempts": 1}),
            )
            monkeypatch.setattr(self.sdk._endpoint.http_session, "send", self._converse_transport)
            provider_class = BedrockConverseProvider
        else:
            self.model = f"anthropic.{MODEL}"
            self.sdk = anthropic.AnthropicBedrockMantle(
                aws_access_key=AWS_ACCESS_KEY,
                aws_secret_key=AWS_SECRET_KEY,
                aws_session_token=AWS_SESSION_TOKEN,
                aws_region="us-east-1",
                base_url="https://mantle.wire.invalid",
                max_retries=0,
                http_client=httpx.Client(
                    transport=httpx.MockTransport(self._mantle_transport), trust_env=False
                ),
            )
            monkeypatch.setattr(
                httpx.HTTPTransport,
                "handle_request",
                lambda transport, request: self._mantle_transport(request),
            )
            provider_class = BedrockClaudeProvider
        stack.callback(self.sdk.close)
        self.provider = provider_class(
            aws_region="us-east-1", default_model=MODEL, timeout_s=7.25, client=self.sdk
        )
        app = create_app()
        store = InMemoryKeyStore()
        app.dependency_overrides[get_key_store] = lambda: store
        app.dependency_overrides[get_llm] = lambda: self.provider
        self.client = stack.enter_context(TestClient(app, raise_server_exceptions=False))

    def _capture(self, request, body):
        self.calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "body": json.loads(body),
                "bytes": body,
                "headers": dict(request.headers),
                "timeout": getattr(request, "extensions", {}).get("timeout"),
            }
        )
        assert self.replies, "unexpected extra HTTP request"
        reply = self.replies.popleft()
        if isinstance(reply, Exception):
            raise reply
        return reply

    def _converse_transport(self, request):
        status, body, headers = self._capture(request, request.body)
        return AWSResponse(request.url, status, headers, _RawBody(body))

    def _mantle_transport(self, request):
        status, body, headers = self._capture(request, request.content)
        return httpx.Response(status, content=body, headers=headers)

    def raw_reply(self, body, *, status=200, headers=None):
        self.replies.append((status, body, {"content-type": "application/json"} | (headers or {})))

    def reply(self, body, *, status=200, headers=None):
        self.raw_reply(_json_bytes(body), status=status, headers=headers)

    def post(self, body=None):
        return self.client.post(
            "/chat", headers={"X-API-Key": API_KEY}, json=_request() if body is None else body
        )


@pytest.fixture
def wire(monkeypatch):
    with ExitStack() as stack:
        backends = []

        def factory(kind):
            backend = _WireBackend(kind, stack, monkeypatch)
            backends.append(backend)
            return backend

        yield factory
        for backend in backends:
            assert not backend.replies, "HTTP replies were not consumed"


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=True, separators=(",", ":")).encode()


def _request():
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Find it"}],
        "tools": [
            {
                "name": "lookup",
                "description": "Find a value",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            }
        ],
        "max_tokens": 1024,
        "system": "Be brief",
    }


def _call(index=0):
    return {
        "type": "tool_use",
        "id": f"call.{index}:part-1",
        "name": "lookup",
        "input": {"query": PRIVATE, "values": [False, 0, None, 1.25, "Montréal"]},
    }


def _block(kind, value):
    if kind == "mantle":
        if value["type"] == "reasoning":
            return {"type": "thinking", "thinking": value["text"], "signature": value["signature"]}
        if value["type"] == "redacted_reasoning":
            return {"type": "redacted_thinking", "data": value["data"]}
        if value["type"] == "tool_result":
            content = value["content"]
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            return value | {"content": content}
        return deepcopy(value)
    if value["type"] == "text":
        return {"text": value["text"]}
    if value["type"] == "reasoning":
        return {
            "reasoningContent": {
                "reasoningText": {"text": value["text"], "signature": value["signature"]}
            }
        }
    if value["type"] == "redacted_reasoning":
        return {"reasoningContent": {"redactedContent": value["data"]}}
    if value["type"] == "tool_use":
        return {
            "toolUse": {"toolUseId": value["id"], "name": value["name"], "input": value["input"]}
        }
    if value["type"] == "tool_result":
        content = value["content"]
        return {
            "toolResult": {
                "toolUseId": value["tool_use_id"],
                "content": [
                    {"text": content} if isinstance(content, str) and content else {"json": content}
                ],
                "status": "error" if value["is_error"] else "success",
            }
        }
    raise AssertionError("unexpected test block")


def _response(kind, blocks=None, stop="end_turn"):
    blocks = [_block(kind, {"type": "text", "text": "Done"})] if blocks is None else blocks
    if kind == "converse":
        return {
            "output": {"message": {"role": "assistant", "content": blocks}},
            "stopReason": stop,
            "usage": {"inputTokens": 7, "outputTokens": 11, "totalTokens": 18},
            "metrics": {"latencyMs": 1},
        }
    return {
        "id": "msg_offline_wire",
        "type": "message",
        "role": "assistant",
        "model": f"anthropic.{MODEL}",
        "content": blocks,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 7, "output_tokens": 11},
    }


def _assert_request(backend, body):
    messages = []
    for message in body["messages"]:
        content = message["content"]
        if isinstance(content, list):
            content = [_block(backend.kind, block) for block in content]
        elif backend.kind == "converse":
            content = [{"text": content}]
        messages.append({"role": message["role"], "content": content})
    tools = body.get("tools") or []
    if backend.kind == "converse":
        expected = {
            "messages": messages,
            "inferenceConfig": {"maxTokens": body["max_tokens"]},
            "system": [{"text": body["system"]}],
        }
        if body.get("thinking", True):
            expected["additionalModelRequestFields"] = {"thinking": {"type": "adaptive"}}
        if tools:
            expected["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": tool["name"],
                            "inputSchema": {"json": tool["input_schema"]},
                        }
                        | ({"description": tool["description"]} if "description" in tool else {})
                    }
                    for tool in tools
                ]
            }
        assert backend.calls[-1]["url"].endswith(f"/model/{backend.model}/converse")
    else:
        expected = {
            "messages": messages,
            "model": backend.model,
            "max_tokens": body["max_tokens"],
            "system": body["system"],
        }
        if body.get("thinking", True):
            expected["thinking"] = {"type": "adaptive"}
        if tools:
            expected["tools"] = tools
        assert backend.calls[-1]["url"].endswith("/v1/messages")
        assert backend.calls[-1]["timeout"]["read"] == 7.25
    authorization = next(
        value
        for key, value in backend.calls[-1]["headers"].items()
        if key.lower() == "authorization"
    )
    if isinstance(authorization, bytes):
        authorization = authorization.decode("ascii")
    assert authorization.startswith(f"AWS4-HMAC-SHA256 Credential={AWS_ACCESS_KEY}/")
    assert backend.calls[-1]["method"] == "POST"
    assert backend.calls[-1]["body"] == expected
    assert json.dumps(backend.calls[-1]["body"], sort_keys=True) == json.dumps(
        expected, sort_keys=True
    )


def _assert_502(response):
    assert response.status_code == 502, response.text
    assert response.json() == {"detail": "LLM provider error"}


@pytest.mark.parametrize("kind", ["converse", "mantle"])
@pytest.mark.parametrize("reasoning", [False, True], ids=["adaptive-no-reasoning", "interleaved"])
def test_wire_roundtrip_preserves_signed_blocks_ids_and_json_values(wire, kind, reasoning):
    backend = wire(kind)
    blocks = [{"type": "text", "text": "Visible "}, _call()] if reasoning else [_call()]
    if reasoning:
        blocks.extend(
            [
                {"type": "reasoning", "text": "", "signature": SIGNATURE},
                {"type": "redacted_reasoning", "data": REDACTED},
            ]
        )
    blocks.extend(_call(index) for index in range(1, 6))
    backend.reply(_response(kind, [_block(kind, block) for block in blocks], "tool_use"))
    body = _request()
    first = backend.post(body)
    assert first.status_code == 200, first.text
    assert first.json()["content_blocks"] == blocks
    assert first.json()["content"] == ("Visible " if reasoning else "")
    assert first.json()["usage"] == {"input_tokens": 7, "output_tokens": 11}
    _assert_request(backend, body)
    values = [False, 0, None, "", {"values": [False, 0, None, 1.25, "雪"]}, {"error": PRIVATE}]
    results = [
        {
            "type": "tool_result",
            "tool_use_id": _call(index)["id"],
            "content": values[index],
            "is_error": index == 5,
        }
        for index in reversed(range(6))
    ]
    body["messages"].extend(
        [
            {"role": "assistant", "content": first.json()["content_blocks"]},
            {"role": "user", "content": results},
        ]
    )
    backend.reply(_response(kind))
    second = backend.post(body)
    assert second.status_code == 200, second.text
    assert second.json()["content_blocks"] == [{"type": "text", "text": "Done"}]
    assert len(backend.calls) == 2
    _assert_request(backend, body)


@pytest.mark.parametrize("kind", ["converse", "mantle"])
@pytest.mark.parametrize("tools", ["omitted", None, []], ids=["omitted", "null", "empty"])
def test_legacy_wire_request_and_response_bytes_remain_compatible(wire, kind, tools):
    backend = wire(kind)
    body = _request()
    if tools == "omitted":
        body.pop("tools")
    else:
        body["tools"] = tools
    backend.reply(
        _response(
            kind,
            [
                _block(kind, {"type": "reasoning", "text": PRIVATE, "signature": SIGNATURE}),
                _block(kind, {"type": "text", "text": 'héllo\n"done"'}),
                _block(kind, {"type": "redacted_reasoning", "data": REDACTED}),
            ],
        )
    )
    response = backend.post(body)
    assert response.status_code == 200, response.text
    expected = (
        b'{"content":"h\xc3\xa9llo\\n\\"done\\"","model":"MODEL",'
        b'"stop_reason":"end_turn","usage":{"input_tokens":7,"output_tokens":11}}'
    ).replace(b"MODEL", backend.model.encode())
    assert response.content == expected
    _assert_request(backend, body)


@pytest.mark.parametrize("kind", ["converse", "mantle"])
@pytest.mark.parametrize("value", [True, False, "11", 11.0, 7.9, -0.5, " 7 "])
def test_wire_numeric_usage_is_not_coerced(wire, kind, value):
    backend = wire(kind)
    body = _response(kind)
    body["usage"]["inputTokens" if kind == "converse" else "input_tokens"] = value
    backend.reply(body)
    _assert_502(backend.post())


@pytest.mark.parametrize("kind", ["converse", "mantle"])
@pytest.mark.parametrize("value", ["Z!g==", "Zh==", "Zg==\n"])
def test_wire_redacted_base64_is_not_silently_repaired(wire, kind, value):
    backend = wire(kind)
    block = _block(kind, {"type": "redacted_reasoning", "data": value})
    backend.reply(_response(kind, [block]))
    _assert_502(backend.post())


@pytest.mark.parametrize("kind", ["converse", "mantle"])
@pytest.mark.parametrize("field", ["tool", "reasoning", "null-reasoning-text"])
def test_wire_unknown_or_null_content_fields_are_not_discarded(wire, kind, field):
    backend = wire(kind)
    call = _block(kind, _call())
    reasoning = _block(kind, {"type": "reasoning", "text": "", "signature": SIGNATURE})
    if field == "tool":
        (call["toolUse"] if kind == "converse" else call)["unexpected"] = PRIVATE
    elif field == "reasoning":
        inner = reasoning["reasoningContent"]["reasoningText"] if kind == "converse" else reasoning
        inner["unexpected"] = PRIVATE
    elif kind == "converse":
        reasoning["reasoningContent"]["reasoningText"]["text"] = None
    else:
        reasoning["thinking"] = None
    backend.reply(_response(kind, [reasoning, call], "tool_use"))
    _assert_502(backend.post())


@pytest.mark.parametrize("kind", ["converse", "mantle"])
@pytest.mark.parametrize("field", ["id", "input", "stop", "nested-input"])
def test_wire_duplicate_json_members_are_rejected_before_parsing(wire, kind, field):
    backend = wire(kind)
    body = _json_bytes(_response(kind, [_block(kind, _call())], "tool_use"))
    if field == "id":
        key = b"toolUseId" if kind == "converse" else b"id"
        old = b'"' + key + b'":"call.0:part-1"'
        new = b'"' + key + b'":123,' + old
    elif field == "input":
        old = b'"input":'
        new = b'"input":[],"input":'
    elif field == "stop":
        key = b"stopReason" if kind == "converse" else b"stop_reason"
        old = b'"' + key + b'":"tool_use"'
        new = b'"' + key + b'":"end_turn",' + old
    else:
        old = b'"query":'
        new = b'"query":false,"query":'
    assert body.count(old) == 1
    backend.raw_reply(body.replace(old, new))
    _assert_502(backend.post())


def _malformed_wire_cases(kind):
    yield "invalid-utf8", b"\xff"
    yield "null-envelope", b"null"
    yield "array-envelope", b"[]"
    yield "number-envelope", b"1"
    yield "string-envelope", b'"unexpected"'
    yield "invalid-json", b"not json"
    yield "excessive-json-recursion", b"[" * 1100 + b"0" + b"]" * 1100
    for label, value in (
        ("object", {}),
        ("array", []),
        ("string", PRIVATE),
        ("nan", float("nan")),
        ("infinity", float("inf")),
    ):
        body = _response(kind)
        body["usage"]["inputTokens" if kind == "converse" else "input_tokens"] = value
        yield f"usage-{label}", _json_bytes(body)
    for label, value in (("padding", "YQ"), ("type", 7), ("unicode", "雪")):
        block = _block(kind, {"type": "redacted_reasoning", "data": value})
        yield f"blob-{label}", _json_bytes(_response(kind, [block]))
    for label, value in (("empty", {}), ("scalar", 7), ("array", [])):
        yield f"block-{label}", _json_bytes(_response(kind, [value]))
    body = _response(kind)
    message = body["output"]["message"] if kind == "converse" else body
    message["content"] = True
    yield "content-boolean", _json_bytes(body)
    if kind == "converse":
        mixed = {"text": PRIVATE, "toolUse": _block(kind, _call())["toolUse"]}
        yield "mixed-union", _json_bytes(_response(kind, [mixed], "tool_use"))
        body = _response(kind)
        body["metrics"]["latencyMs"] = PRIVATE
        yield "ignored-metric-type", _json_bytes(body)


@pytest.mark.parametrize(
    "kind,body",
    [
        pytest.param(kind, body, id=f"{kind}-{label}")
        for kind in ("converse", "mantle")
        for label, body in _malformed_wire_cases(kind)
    ],
)
def test_wire_parser_failures_are_sanitized_502_not_500(wire, kind, body, capsys, caplog):
    backend = wire(kind)
    backend.raw_reply(body)
    response = backend.post()
    _assert_502(response)
    observed = response.text + capsys.readouterr().out + caplog.text
    assert PRIVATE not in observed


@pytest.mark.parametrize("kind", ["converse", "mantle"])
@pytest.mark.parametrize("value", [False, 7, [], {}])
@pytest.mark.parametrize("field", ["text", "id", "signature"])
def test_wire_string_fields_are_not_coerced(wire, kind, value, field):
    backend = wire(kind)
    call = _call()
    block = {"type": "text", "text": "Visible"}
    if field == "id":
        call["id"] = value
    elif field == "signature":
        block = {"type": "reasoning", "text": "", "signature": value}
    else:
        block["text"] = value
    backend.reply(_response(kind, [_block(kind, block), _block(kind, call)], "tool_use"))
    _assert_502(backend.post())


@pytest.mark.parametrize("kind", ["converse", "mantle"])
@pytest.mark.parametrize("limit", ["blocks", "input", "aggregate", "depth", "nonfinite"])
def test_wire_response_bounds_and_json_values_fail_closed(wire, kind, limit):
    backend = wire(kind)
    blocks = [_block(kind, _call())]
    if limit == "blocks":
        blocks += [_block(kind, {"type": "text", "text": "a"})] * MAX_CONTENT_BLOCKS
    elif limit == "aggregate":
        blocks += [_block(kind, {"type": "text", "text": "a" * MAX_TOOL_PAYLOAD_BYTES})]
    else:
        inner = blocks[0]["toolUse"] if kind == "converse" else blocks[0]
        if limit == "input":
            inner["input"] = {"value": "a" * MAX_JSON_BYTES}
        elif limit == "nonfinite":
            inner["input"] = {"value": float("inf")}
        else:
            inner["input"] = {"value": 0}
            for _ in range(21):
                inner["input"] = {"value": inner["input"]}
    backend.reply(_response(kind, blocks, "tool_use"))
    _assert_502(backend.post())


@pytest.mark.parametrize(
    "caller",
    [{"type": "code_execution_20260120"}, {"type": "direct", "extra": PRIVATE}, [], False],
)
def test_mantle_wire_unsupported_caller_cannot_become_client_tool(wire, caller):
    backend = wire("mantle")
    block = _block("mantle", _call()) | {"caller": caller}
    backend.reply(_response("mantle", [block], "tool_use"))
    _assert_502(backend.post())


@pytest.mark.parametrize("caller", [None, {"type": "direct"}])
def test_mantle_wire_direct_caller_is_supported(wire, caller):
    backend = wire("mantle")
    backend.reply(_response("mantle", [_call() | {"caller": caller}], "tool_use"))
    response = backend.post()
    assert response.status_code == 200, response.text
    assert response.json()["content_blocks"] == [_call()]


def test_converse_wire_server_tool_type_is_not_downgraded(wire):
    backend = wire("converse")
    block = _block("converse", _call())
    block["toolUse"]["type"] = "server_tool_use"
    backend.reply(_response("converse", [block], "tool_use"))
    _assert_502(backend.post())


def test_converse_wire_unknown_union_does_not_log_payload_field_names(wire, caplog):
    backend = wire("converse")
    caplog.set_level(logging.INFO)
    backend.reply(_response("converse", [{PRIVATE: {"value": PRIVATE}}]))
    _assert_502(backend.post())
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("kind", ["converse", "mantle"])
def test_wire_documented_context_window_stop_is_a_valid_completion(wire, kind):
    backend = wire(kind)
    backend.reply(_response(kind, stop="model_context_window_exceeded"))
    response = backend.post()
    assert response.status_code == 200, response.text
    assert response.json()["stop_reason"] == "model_context_window_exceeded"
    assert response.json()["content_blocks"] == [{"type": "text", "text": "Done"}]


@pytest.mark.parametrize("kind", ["converse", "mantle"])
def test_wire_pinned_sdk_stop_reasons_are_supported(wire, kind):
    backend = wire(kind)
    stops = (
        backend.sdk.meta.service_model.shape_for("StopReason").enum
        if kind == "converse"
        else get_args(StopReason)
    )
    for stop in stops:
        blocks = [_block(kind, _call())] if stop == "tool_use" else None
        backend.reply(_response(kind, blocks, stop))
        response = backend.post()
        assert response.status_code == 200, (stop, response.text)
        assert response.json()["stop_reason"] == stop


@pytest.mark.parametrize("kind", ["converse", "mantle"])
@pytest.mark.parametrize("status,expected", [(400, 502), (403, 502), (429, 429), (503, 502)])
def test_wire_http_error_statuses_are_normalized(wire, kind, status, expected, capsys, caplog):
    backend = wire(kind)
    code = "ThrottlingException" if status == 429 else "AccessDeniedException"
    body = (
        {"__type": code, "message": PRIVATE}
        if kind == "converse"
        else {"type": "error", "error": {"type": "api_error", "message": PRIVATE}}
    )
    backend.reply(body, status=status)
    response = backend.post()
    assert response.status_code == expected, response.text
    assert PRIVATE not in response.text + capsys.readouterr().out + caplog.text


@pytest.mark.parametrize("kind", ["converse", "mantle"])
def test_wire_transport_timeout_maps_to_504(wire, kind):
    backend = wire(kind)
    backend.replies.append(
        ReadTimeoutError(endpoint_url="https://converse.wire.invalid", error=PRIVATE)
        if kind == "converse"
        else httpx.ReadTimeout(PRIVATE)
    )
    response = backend.post()
    assert response.status_code == 504, response.text
    assert response.json() == {"detail": "LLM provider timeout"}


@pytest.mark.parametrize("body", [b"[]", b"null", b"\xff", b'{"__type":7}'])
def test_converse_wire_malformed_error_body_is_sanitized(wire, body):
    backend = wire("converse")
    backend.raw_reply(body, status=400)
    _assert_502(backend.post())


def test_converse_wire_parser_exception_is_normalized_at_provider_boundary(wire):
    backend = wire("converse")
    backend.raw_reply(b"\xff")
    request = LLMRequest(
        messages=[LLMMessage("user", "Find it")],
        tools=[LLMTool("lookup", {"type": "object"})],
    )
    with pytest.raises(ProviderUnavailable):
        backend.provider.chat(request)


@pytest.mark.parametrize("error_type", [RuntimeError, TypeError, AttributeError])
def test_converse_wire_transport_programmer_errors_are_not_swallowed(wire, error_type):
    backend = wire("converse")
    error = error_type("offline transport programmer defect")
    backend.replies.append(error)
    request = LLMRequest(
        messages=[LLMMessage("user", "Find it")],
        tools=[LLMTool("lookup", {"type": "object"})],
    )
    with pytest.raises(error_type) as caught:
        backend.provider.chat(request)
    assert caught.value is error


@pytest.mark.parametrize("kind", ["converse", "mantle"])
@pytest.mark.parametrize("thinking", [False, True])
def test_empty_tools_variants_emit_identical_legacy_request_bytes(wire, kind, thinking):
    backend = wire(kind)
    for tools in ("omitted", None, []):
        body = _request() | {"thinking": thinking}
        if tools == "omitted":
            body.pop("tools")
        else:
            body["tools"] = tools
        backend.reply(_response(kind))
        assert backend.post(body).status_code == 200
        _assert_request(backend, body)
    assert backend.calls[0]["bytes"] == backend.calls[1]["bytes"] == backend.calls[2]["bytes"]


def test_mantle_wire_raw_response_byte_limit(wire):
    backend = wire("mantle")
    raw = _json_bytes(_response("mantle"))
    raw += b" " * (MAX_TOOL_PAYLOAD_BYTES - len(raw))
    backend.raw_reply(raw)
    assert backend.post().status_code == 200
    backend.raw_reply(raw + b" ")
    _assert_502(backend.post())


@pytest.mark.parametrize("kind", ["converse", "mantle"])
@pytest.mark.parametrize(
    "stop,calls",
    [
        ("unknown_stop", False),
        (None, False),
        (True, False),
        ("tool_use", False),
        ("end_turn", True),
    ],
)
def test_wire_unknown_and_contradictory_stop_reasons_are_rejected(wire, kind, stop, calls):
    backend = wire(kind)
    blocks = [_block(kind, _call())] if calls else None
    backend.reply(_response(kind, blocks, stop))
    _assert_502(backend.post())
