import base64
import copy
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from threading import Event

import anthropic
import boto3
import botocore.exceptions
import httpx
import pytest
from botocore import UNSIGNED
from botocore.awsrequest import AWSResponse
from botocore.config import Config
from botocore.stub import Stubber

from contracts.tool_validation import (
    MAX_CONTENT_BLOCKS,
    MAX_JSON_BYTES,
    MAX_JSON_DEPTH,
    MAX_TOOL_PAYLOAD_BYTES,
    MAX_TOOLS,
)
from src.providers.base import (
    LLMMessage,
    LLMReasoningBlock,
    LLMRedactedReasoningBlock,
    LLMRequest,
    LLMTextBlock,
    LLMTool,
    LLMToolResultBlock,
    LLMToolUseBlock,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from src.providers.bedrock import BedrockClaudeProvider
from src.providers.bedrock_converse import BedrockConverseProvider


_TOOLS = [
    LLMTool(
        name="lookup",
        input_schema={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
        description="Find the forecast",
    ),
    LLMTool(name="notify", input_schema={"type": "object"}),
]
_DATA = base64.b64encode(b"\x00\xff\x01redacted").decode("ascii")
_PRIVATE = "private-provider-payload"


@pytest.fixture(autouse=True)
def _offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network disabled in provider tests")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


class _ConverseClient:
    def __init__(self, responses, captured):
        self.responses = list(responses)
        self.captured = captured

    def converse(self, **kwargs):
        self.captured.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _provider(backend, client):
    provider_class = BedrockConverseProvider if backend == "converse" else BedrockClaudeProvider
    return provider_class(
        aws_region="us-east-1", default_model="claude-sonnet-4-6", timeout_s=30.0, client=client
    )


@contextmanager
def _adapter(backend, *responses):
    captured = []
    if backend == "converse":
        yield _provider(backend, _ConverseClient(responses, captured)), captured
        return

    remaining = list(responses)

    def handle(request):
        captured.append(json.loads(request.content))
        response = remaining.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(
            200, content=json.dumps(response).encode(), headers={"content-type": "application/json"}
        )

    with anthropic.AnthropicBedrockMantle(
        aws_region="us-east-1",
        base_url="https://mantle.invalid",
        skip_auth=True,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    ) as client:
        yield _provider(backend, client), captured


def _request(**kwargs):
    return LLMRequest(
        messages=kwargs.pop("messages", [LLMMessage(role="user", content="Find both forecasts")]),
        tools=kwargs.pop("tools", copy.deepcopy(_TOOLS)),
        **kwargs,
    )


def _text(backend, text="Done"):
    return {"text": text} if backend == "converse" else {"type": "text", "text": text}


def _call(backend, *, id="call-1.a:b", name="lookup", input=None):
    value = {"name": name, "input": {"city": "Toronto"} if input is None else input}
    if backend == "converse":
        return {"toolUse": {"toolUseId": id, **value}}
    return {"type": "tool_use", "id": id, **value}


def _response(backend, blocks=None, stop="end_turn"):
    if blocks is None:
        blocks = [_text(backend)]
    if backend == "converse":
        return {
            "output": {"message": {"role": "assistant", "content": blocks}},
            "stopReason": stop,
            "usage": {"inputTokens": 11, "outputTokens": 7, "totalTokens": 18},
            "metrics": {"latencyMs": 1},
        }
    return {
        "id": "msg_offline",
        "type": "message",
        "role": "assistant",
        "model": "anthropic.claude-sonnet-4-6",
        "content": blocks,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


def _neutral_turn():
    return [
        LLMReasoningBlock(text="Choose both cities", signature="opaque-signature"),
        LLMRedactedReasoningBlock(data=_DATA),
        LLMTextBlock(text="First "),
        LLMToolUseBlock(id="call-1.a:b", name="lookup", input={"city": "Toronto"}),
        LLMTextBlock(text="then "),
        LLMToolUseBlock(id="call_2", name="lookup", input={"city": "Montréal"}),
    ]


def _vendor_turn(backend):
    if backend == "converse":
        prefix = [
            {
                "reasoningContent": {
                    "reasoningText": {"text": "Choose both cities", "signature": "opaque-signature"}
                }
            },
            {"reasoningContent": {"redactedContent": base64.b64decode(_DATA)}},
        ]
    else:
        prefix = [
            {"type": "thinking", "thinking": "Choose both cities", "signature": "opaque-signature"},
            {"type": "redacted_thinking", "data": _DATA},
        ]
    return prefix + [
        _text(backend, "First "),
        _call(backend),
        _text(backend, "then "),
        _call(backend, id="call_2", input={"city": "Montréal"}),
    ]


def test_converse_round_trip_uses_real_botocore_shapes():
    client = boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        endpoint_url="https://bedrock.invalid",
        config=Config(signature_version=UNSIGNED),
    )
    expected = {
        "modelId": "us.anthropic.claude-sonnet-4-6",
        "messages": [{"role": "user", "content": [{"text": "Find both forecasts"}]}],
        "inferenceConfig": {"maxTokens": 16000},
        "additionalModelRequestFields": {"thinking": {"type": "adaptive"}},
        "toolConfig": {
            "tools": [
                {
                    "toolSpec": {
                        "name": "lookup",
                        "description": "Find the forecast",
                        "inputSchema": {"json": _TOOLS[0].input_schema},
                    }
                },
                {"toolSpec": {"name": "notify", "inputSchema": {"json": {"type": "object"}}}},
            ]
        },
        "system": [{"text": "Be brief"}],
    }
    continuation = copy.deepcopy(expected)
    continuation["messages"] += [
        {"role": "assistant", "content": _vendor_turn("converse")},
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "call-1.a:b",
                        "content": [{"json": {"temperature": 8, "rain": None}}],
                        "status": "success",
                    }
                },
                {
                    "toolResult": {
                        "toolUseId": "call_2",
                        "content": [{"text": "No forecast"}],
                        "status": "error",
                    }
                },
                {"text": "Now compare"},
            ],
        },
    ]
    with closing(client), Stubber(client) as stubber:
        stubber.add_response(
            "converse", _response("converse", _vendor_turn("converse"), "tool_use"), expected
        )
        stubber.add_response(
            "converse",
            _response("converse", [_text("converse", "All "), _text("converse", "done")]),
            continuation,
        )
        provider = _provider("converse", client)
        request = _request(system="Be brief")
        first = provider.chat(request)
        assert first.content_blocks == _neutral_turn()
        assert first.content == "First then "
        assert first.stop_reason == "tool_use"
        assert (first.input_tokens, first.output_tokens) == (11, 7)
        assert first.model == expected["modelId"]
        request.messages += [
            LLMMessage(role="assistant", content=first.content_blocks),
            LLMMessage(
                role="user",
                content=[
                    LLMToolResultBlock("call-1.a:b", {"temperature": 8, "rain": None}),
                    LLMToolResultBlock("call_2", "No forecast", is_error=True),
                    LLMTextBlock("Now compare"),
                ],
            ),
        ]
        final = provider.chat(request)
        assert final.content == "All done"
        assert final.content_blocks == [LLMTextBlock("All "), LLMTextBlock("done")]
        assert final.stop_reason == "end_turn"
        stubber.assert_no_pending_responses()


def test_mantle_round_trip_preserves_signed_blocks_through_real_sdk():
    with _adapter(
        "mantle",
        _response("mantle", _vendor_turn("mantle"), "tool_use"),
        _response("mantle", [_text("mantle", "All "), _text("mantle", "done")]),
    ) as (provider, captured):
        request = _request(system="Be brief")
        first = provider.chat(request)
        assert first.content_blocks == _neutral_turn()
        assert first.content == "First then "
        assert first.stop_reason == "tool_use"
        assert (first.input_tokens, first.output_tokens) == (11, 7)
        assert first.model == "anthropic.claude-sonnet-4-6"
        request.messages += [
            LLMMessage(role="assistant", content=first.content_blocks),
            LLMMessage(
                role="user",
                content=[
                    LLMToolResultBlock("call-1.a:b", {"temperature": 8, "rain": None}),
                    LLMToolResultBlock("call_2", "No forecast", is_error=True),
                    LLMTextBlock("Now compare"),
                ],
            ),
        ]
        final = provider.chat(request)
        assert final.content == "All done"
        assert final.content_blocks == [LLMTextBlock("All "), LLMTextBlock("done")]
        assert captured[0] == {
            "model": "anthropic.claude-sonnet-4-6",
            "max_tokens": 16000,
            "messages": [{"role": "user", "content": "Find both forecasts"}],
            "system": "Be brief",
            "thinking": {"type": "adaptive"},
            "tools": [
                {
                    "name": "lookup",
                    "description": "Find the forecast",
                    "input_schema": _TOOLS[0].input_schema,
                },
                {"name": "notify", "input_schema": {"type": "object"}},
            ],
        }
        assert captured[1]["messages"][1] == {
            "role": "assistant",
            "content": _vendor_turn("mantle"),
        }
        assert captured[1]["messages"][2] == {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1.a:b",
                    "content": '{"temperature":8,"rain":null}',
                    "is_error": False,
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "call_2",
                    "content": "No forecast",
                    "is_error": True,
                },
                {"type": "text", "text": "Now compare"},
            ],
        }
        assert captured[1]["thinking"] == {"type": "adaptive"}
        assert "tool_choice" not in captured[1]


@pytest.mark.parametrize("backend", ["converse", "mantle"])
def test_tool_only_answer_and_thinking_off(backend):
    with _adapter(backend, _response(backend, [_call(backend)], "tool_use")) as (provider, sent):
        result = provider.chat(_request(thinking=False))
    assert result.content == ""
    assert result.content_blocks == [LLMToolUseBlock("call-1.a:b", "lookup", {"city": "Toronto"})]
    assert "thinking" not in sent[0]
    assert "additionalModelRequestFields" not in sent[0]


@pytest.mark.parametrize("backend", ["converse", "mantle"])
def test_list_content_enables_blocks_without_tool_definitions(backend):
    with _adapter(backend, _response(backend)) as (provider, sent):
        result = provider.chat(
            _request(tools=[], messages=[LLMMessage("user", [LLMTextBlock("Hello")])])
        )
    assert result.content_blocks == [LLMTextBlock("Done")]
    assert "tools" not in sent[0]
    assert "toolConfig" not in sent[0]


@pytest.mark.parametrize("backend", ["converse", "mantle"])
def test_legacy_response_has_no_content_blocks_and_still_ignores_unsigned_reasoning(
    backend, monkeypatch
):
    reasoning = (
        {"reasoningContent": {"reasoningText": {"text": "Legacy"}}}
        if backend == "converse"
        else {"type": "thinking", "thinking": "Legacy"}
    )
    with _adapter(backend, _response(backend, [reasoning, _text(backend)])) as (provider, sent):
        if backend == "mantle":
            monkeypatch.setattr(provider._client, "with_options", lambda **kwargs: provider._client)
        result = provider.chat(_request(tools=[]))
    assert result.content == "Done"
    assert result.content_blocks is None
    assert "tools" not in sent[0]
    assert "toolConfig" not in sent[0]


@pytest.mark.parametrize("backend", ["converse", "mantle"])
def test_signature_only_reasoning_normalizes_missing_text_without_losing_signature(backend):
    reasoning = (
        {"reasoningContent": {"reasoningText": {"signature": "opaque"}}}
        if backend == "converse"
        else {"type": "thinking", "signature": "opaque"}
    )
    with _adapter(backend, _response(backend, [reasoning, _call(backend)], "tool_use")) as (
        provider,
        _,
    ):
        result = provider.chat(_request())
    assert result.content_blocks[0] == LLMReasoningBlock(text="", signature="opaque")
    assert result.content == ""


@pytest.mark.parametrize("backend", ["converse", "mantle"])
@pytest.mark.parametrize("value", ["plain", "", None, False, 0, 1.25, [], {}, {"a": [True, "é"]}])
def test_tool_results_preserve_all_json_values(backend, value):
    request = _request(
        messages=[
            LLMMessage("user", "Hello"),
            LLMMessage("assistant", [LLMToolUseBlock("exact:id-1.2", "lookup", {})]),
            LLMMessage("user", [LLMToolResultBlock("exact:id-1.2", value)]),
        ]
    )
    with _adapter(backend, _response(backend)) as (provider, sent):
        provider.chat(request)
    block = sent[0]["messages"][-1]["content"][0]
    if backend == "converse":
        sdk = boto3.client(
            "bedrock-runtime",
            region_name="us-east-1",
            endpoint_url="https://bedrock.invalid",
            config=Config(signature_version=UNSIGNED),
        )
        with closing(sdk), Stubber(sdk) as stubber:
            stubber.add_response("converse", _response(backend), sent[0])
            assert _provider(backend, sdk).chat(request).content == "Done"
            stubber.assert_no_pending_responses()
        content = {"text": value} if isinstance(value, str) and value else {"json": value}
        assert block == {
            "toolResult": {
                "toolUseId": "exact:id-1.2",
                "content": [content],
                "status": "success",
            }
        }
    else:
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "exact:id-1.2"
        assert block["is_error"] is False
        if isinstance(value, str):
            assert block["content"] == value
        else:
            assert json.loads(block["content"]) == value


def _nested_json():
    value = {}
    for _ in range(MAX_JSON_DEPTH + 1):
        value = {"nested": value}
    return value


def _bad_blocks(backend):
    if backend == "converse":
        yield {}
        yield {"text": 7}
        yield {"text": _PRIVATE, "toolUse": _call(backend)["toolUse"]}
        yield {"text": _PRIVATE, "unexpected": True}
        yield {"image": {"source": _PRIVATE}}
        yield {"reasoningContent": {"reasoningText": {"text": _PRIVATE}}}
        yield {"reasoningContent": {"reasoningText": {"text": 7, "signature": "s"}}}
        yield {"reasoningContent": {"reasoningText": {"text": _PRIVATE, "signature": 7}}}
        yield {"reasoningContent": {"reasoningText": {"text": _PRIVATE, "signature": ""}}}
        yield {"reasoningContent": {"redactedContent": _DATA}}
        yield {"reasoningContent": {"redactedContent": b""}}
        yield {
            "reasoningContent": {
                "redactedContent": b"a",
                "reasoningText": {"text": _PRIVATE, "signature": "s"},
            }
        }
        yield {"toolResult": {"toolUseId": "output_result", "content": [{"text": _PRIVATE}]}}
    else:
        yield {}
        yield {"type": "text", "text": 7}
        yield {"type": "text", "text": _PRIVATE, "id": "call", "name": "lookup", "input": {}}
        yield {"type": "text", "text": _PRIVATE, "unexpected": True}
        yield {"type": "image", "source": _PRIVATE}
        yield {"type": "thinking", "thinking": _PRIVATE}
        yield {"type": "thinking", "thinking": 7, "signature": "s"}
        yield {"type": "thinking", "thinking": _PRIVATE, "signature": 7}
        yield {"type": "thinking", "thinking": _PRIVATE, "signature": ""}
        yield {"type": "redacted_thinking", "data": "not base64"}
        yield {"type": "redacted_thinking", "data": ""}
        yield {"type": "redacted_thinking", "data": "Zh=="}
        yield {"type": "redacted_thinking", "data": _DATA, "thinking": _PRIVATE}
        yield {"type": "tool_result", "tool_use_id": "output_result", "content": _PRIVATE}
        yield {"type": "text", "text": _PRIVATE, "citations": [{"type": "unknown"}]}
        yield {"type": "tool_use", "id": "caller", "name": "lookup", "input": [], "caller": None}
        yield {"type": [], "text": _PRIVATE}
    for id in [None, 123, True, "", "contains whitespace", "x" * 65]:
        yield _call(backend, id=id)
    for name in [None, 123, "", "contains.dot", "undeclared", "x" * 65]:
        yield _call(backend, name=name)
    for value in [[], "input", True, 1, {"x": float("nan")}, {"x": float("inf")}, _nested_json()]:
        yield _call(backend, input=value)
    yield _call(backend, input={"x": "x" * MAX_JSON_BYTES})
    yield _text(backend, "x" * MAX_TOOL_PAYLOAD_BYTES)
    yield _text(backend, "\ud800")
    yield None
    yield []
    yield _PRIVATE


@pytest.mark.parametrize(
    "backend,block",
    [
        pytest.param(backend, block, id=f"{backend}-{index}")
        for backend in ("converse", "mantle")
        for index, block in enumerate(_bad_blocks(backend))
    ],
)
def test_malformed_unknown_mixed_or_oversized_blocks_fail_closed(backend, block, caplog):
    response = _response(backend, [block, _call(backend)], "tool_use")
    with _adapter(backend, response) as (provider, _):
        with pytest.raises(ProviderUnavailable) as error:
            provider.chat(_request())
    assert _PRIVATE not in str(error.value)
    assert _PRIVATE not in caplog.text


@pytest.mark.parametrize("backend", ["converse", "mantle"])
@pytest.mark.parametrize("stop", ["end_turn", "max_tokens", "stop_sequence", "refusal", ""])
def test_calls_under_contradictory_stop_reasons_are_rejected(backend, stop):
    with _adapter(backend, _response(backend, [_call(backend)], stop)) as (provider, _):
        with pytest.raises(ProviderUnavailable):
            provider.chat(_request())


@pytest.mark.parametrize("backend", ["converse", "mantle"])
@pytest.mark.parametrize("case", ["no-calls", "duplicate-calls", "too-many-blocks", "empty-blocks"])
def test_invalid_response_block_collections_are_rejected(backend, case):
    blocks = {
        "no-calls": [_text(backend)],
        "duplicate-calls": [_call(backend), _call(backend)],
        "too-many-blocks": [_text(backend)] * MAX_CONTENT_BLOCKS + [_call(backend)],
        "empty-blocks": [],
    }[case]
    with _adapter(backend, _response(backend, blocks, "tool_use")) as (provider, _):
        with pytest.raises(ProviderUnavailable):
            provider.chat(_request())


@pytest.mark.parametrize("backend", ["converse", "mantle"])
def test_reusing_a_historical_call_id_is_rejected(backend):
    request = _request(
        messages=[
            LLMMessage("user", "Hello"),
            LLMMessage("assistant", [LLMToolUseBlock("call-1.a:b", "lookup", {})]),
            LLMMessage("user", [LLMToolResultBlock("call-1.a:b", "Done")]),
        ]
    )
    with _adapter(backend, _response(backend, [_call(backend)], "tool_use")) as (provider, _):
        with pytest.raises(ProviderUnavailable):
            provider.chat(request)


@pytest.mark.parametrize("backend", ["converse", "mantle"])
@pytest.mark.parametrize("field", ["usage", "input", "output", "stop", "role", "content"])
@pytest.mark.parametrize("value", [None, 7.5, False, [], {}, _PRIVATE])
def test_invalid_usage_and_response_headers_never_escape_as_500s(backend, field, value):
    response = _response(backend, [_call(backend)], "tool_use")
    message = response["output"]["message"] if backend == "converse" else response
    if field in ("input", "output"):
        name = f"{field}Tokens" if backend == "converse" else f"{field}_tokens"
        response["usage"][name] = value
    elif field == "stop":
        response["stopReason" if backend == "converse" else "stop_reason"] = value
    elif field == "usage":
        response["usage"] = value
    else:
        message[field] = value
    with _adapter(backend, response) as (provider, _):
        with pytest.raises(ProviderUnavailable) as error:
            provider.chat(_request())
    assert _PRIVATE not in str(error.value)


@pytest.mark.parametrize("backend", ["converse", "mantle"])
@pytest.mark.parametrize("field", ["usage", "input", "output", "stop", "role", "content"])
def test_missing_usage_and_response_headers_are_normalized(backend, field):
    response = _response(backend, [_call(backend)], "tool_use")
    message = response["output"]["message"] if backend == "converse" else response
    if field in ("input", "output"):
        del response["usage"][f"{field}Tokens" if backend == "converse" else f"{field}_tokens"]
    elif field == "stop":
        del response["stopReason" if backend == "converse" else "stop_reason"]
    elif field == "usage":
        del response["usage"]
    else:
        del message[field]
    with _adapter(backend, response) as (provider, _):
        with pytest.raises(ProviderUnavailable):
            provider.chat(_request())


@pytest.mark.parametrize("body", [b"not json", b"\xff", b"[]", b"null", b"[" * 1100])
def test_mantle_invalid_raw_json_is_normalized(body):
    with _adapter("mantle", httpx.Response(200, content=body)) as (provider, _):
        with pytest.raises(ProviderUnavailable):
            provider.chat(_request())


def test_mantle_duplicate_raw_json_fields_are_rejected_before_sdk_coercion():
    body = json.dumps(_response("mantle", [_call("mantle")], "tool_use"))
    body = body.replace('"id": "call-1.a:b"', '"id": 123, "id": "call-1.a:b"')
    with _adapter("mantle", httpx.Response(200, content=body)) as (provider, _):
        with pytest.raises(ProviderUnavailable):
            provider.chat(_request())


@pytest.mark.parametrize("value", [None, 123, False, [], {}, "", "\ud800"])
def test_mantle_invalid_raw_model_is_rejected(value):
    response = _response("mantle")
    response["model"] = value
    with _adapter("mantle", response) as (provider, _):
        with pytest.raises(ProviderUnavailable):
            provider.chat(_request())


def test_mantle_raw_response_accepts_only_harmless_known_block_metadata():
    blocks = [
        {"type": "text", "text": "Hello", "citations": None},
        {**_call("mantle"), "caller": {"type": "direct"}},
    ]
    with _adapter("mantle", _response("mantle", blocks, "tool_use")) as (provider, _):
        result = provider.chat(_request())
    assert result.content_blocks == [
        LLMTextBlock("Hello"),
        LLMToolUseBlock("call-1.a:b", "lookup", {"city": "Toronto"}),
    ]


def _bad_requests():
    yield _request(tools=[LLMTool("bad.name", {"type": "object"})])
    yield _request(tools=[LLMTool("lookup", {"type": "array"})])
    yield _request(tools=[LLMTool("lookup", {"type": "object", "x": float("nan")})])
    yield _request(tools=[LLMTool("lookup", {"type": "object"}, description="")])
    yield _request(tools=[_TOOLS[0], _TOOLS[0]])
    yield _request(tools=[LLMTool(f"tool{i}", {"type": "object"}) for i in range(MAX_TOOLS + 1)])
    for content in [
        [],
        [object()],
        [LLMTextBlock(7)],
        [LLMTextBlock("x" * MAX_TOOL_PAYLOAD_BYTES)],
        [LLMToolUseBlock("call", "lookup", {})],
        [LLMToolResultBlock("orphan", "Hello")],
        [LLMReasoningBlock("reasoning", "signature")],
        [LLMRedactedReasoningBlock(_DATA)],
    ]:
        yield _request(messages=[LLMMessage("user", content)])
    for block in [
        LLMToolUseBlock("bad id", "lookup", {}),
        LLMToolUseBlock("call", "undeclared", {}),
        LLMToolUseBlock("call", "lookup", []),
        LLMToolUseBlock("call", "lookup", {"x": float("nan")}),
        LLMToolUseBlock("call", "lookup", {1: "non-string key"}),
        LLMToolUseBlock("call", "lookup", {"x": (1, 2)}),
        LLMReasoningBlock("reasoning", ""),
        LLMReasoningBlock(None, "signature"),
        LLMRedactedReasoningBlock("not base64"),
        LLMToolResultBlock("call", "assistant result"),
    ]:
        yield _request(messages=[LLMMessage("assistant", [block])])
    for result in [
        LLMToolResultBlock("different-id", "Wrong"),
        LLMToolResultBlock("call", {"x": float("nan")}),
        LLMToolResultBlock("call", "Wrong flag", is_error="false"),
    ]:
        yield _request(
            messages=[
                LLMMessage("assistant", [LLMToolUseBlock("call", "lookup", {})]),
                LLMMessage("user", [result]),
            ]
        )


@pytest.mark.parametrize("backend", ["converse", "mantle"])
@pytest.mark.parametrize("llm_request", list(_bad_requests()))
def test_unsupported_neutral_input_fails_before_invocation(backend, llm_request):
    with _adapter(backend, _response(backend)) as (provider, captured):
        with pytest.raises(ProviderUnavailable):
            provider.chat(llm_request)
        assert captured == []


@pytest.mark.parametrize(
    "code,expected",
    [
        ("ThrottlingException", ProviderRateLimited),
        ("AccessDeniedException", ProviderUnavailable),
    ],
)
def test_converse_tool_errors_are_normalized_without_payloads(code, expected):
    error = botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": _PRIVATE}}, "Converse"
    )
    with _adapter("converse", error) as (provider, _):
        with pytest.raises(expected) as caught:
            provider.chat(_request())
    assert _PRIVATE not in str(caught.value)


@pytest.mark.parametrize(
    "status,expected",
    [(429, ProviderRateLimited), (400, ProviderUnavailable), (503, ProviderUnavailable)],
)
def test_mantle_tool_errors_are_normalized_without_payloads(status, expected):
    response = httpx.Response(status, json={"error": {"type": "error", "message": _PRIVATE}})
    with _adapter("mantle", response) as (provider, _):
        with pytest.raises(expected) as caught:
            provider.chat(_request())
    assert _PRIVATE not in str(caught.value)


@pytest.mark.parametrize("backend", ["converse", "mantle"])
def test_tool_timeouts_keep_the_normalized_error_class(backend):
    error = (
        botocore.exceptions.ReadTimeoutError(endpoint_url="https://bedrock.invalid")
        if backend == "converse"
        else httpx.ReadTimeout(_PRIVATE)
    )
    with _adapter(backend, error) as (provider, _):
        with pytest.raises(ProviderTimeout) as caught:
            provider.chat(_request())
    assert _PRIVATE not in str(caught.value)


def test_converse_does_not_hide_programmer_errors():
    with _adapter("converse", RuntimeError("programmer defect")) as (provider, _):
        with pytest.raises(RuntimeError, match="programmer defect"):
            provider.chat(_request())


@pytest.mark.parametrize("backend", ["converse", "mantle"])
def test_individually_bounded_schemas_can_exceed_one_json_values_node_budget(backend):
    tools = [
        LLMTool(
            name,
            {"type": "object", "properties": {"value": {"type": "integer", "enum": [0] * 6000}}},
        )
        for name in ("lookup", "notify")
    ]
    with _adapter(backend, _response(backend)) as (provider, sent):
        assert provider.chat(_request(tools=tools)).content == "Done"
        assert len(sent) == 1


@pytest.mark.parametrize("backend", ["converse", "mantle"])
@pytest.mark.parametrize(
    "order",
    [
        pytest.param((2, 0, 3), id="text-reasoning-call"),
        pytest.param((2, 1, 3), id="text-redacted-call"),
        pytest.param((0, 3, 0, 5), id="interleaved-reasoning-calls"),
        pytest.param((0, 3, 1, 5), id="interleaved-redacted-call"),
    ],
)
def test_adaptive_reasoning_order_is_preserved_through_tool_replay(backend, order):
    vendor_blocks = [_vendor_turn(backend)[index] for index in order]
    neutral_blocks = [_neutral_turn()[index] for index in order]
    with _adapter(backend, _response(backend, vendor_blocks, "tool_use"), _response(backend)) as (
        provider,
        sent,
    ):
        request = _request()
        first = provider.chat(request)
        assert first.content_blocks == neutral_blocks
        assert first.content == "".join(
            block.text for block in neutral_blocks if isinstance(block, LLMTextBlock)
        )
        request.messages += [
            LLMMessage("assistant", first.content_blocks),
            LLMMessage(
                "user",
                [
                    LLMToolResultBlock(block.id, {"ok": True})
                    for block in neutral_blocks
                    if isinstance(block, LLMToolUseBlock)
                ],
            ),
        ]
        assert provider.chat(request).content == "Done"
        assert sent[1]["messages"][1] == {"role": "assistant", "content": vendor_blocks}
        for invocation in sent:
            if backend == "converse":
                assert invocation["additionalModelRequestFields"] == {
                    "thinking": {"type": "adaptive"}
                }
            else:
                assert invocation["thinking"] == {"type": "adaptive"}


@pytest.mark.parametrize("backend", ["converse", "mantle"])
def test_reasoning_after_ordinary_history_is_preserved(backend):
    request = _request(
        messages=[
            LLMMessage("assistant", [LLMTextBlock("Answer"), LLMReasoningBlock("Late", "s")]),
            LLMMessage("user", "Continue"),
        ]
    )
    with _adapter(backend, _response(backend)) as (provider, sent):
        assert provider.chat(request).content == "Done"
        reasoning = (
            {"reasoningContent": {"reasoningText": {"text": "Late", "signature": "s"}}}
            if backend == "converse"
            else {"type": "thinking", "thinking": "Late", "signature": "s"}
        )
        assert sent[0]["messages"][0]["content"] == [_text(backend, "Answer"), reasoning]


@pytest.mark.parametrize("backend", ["converse", "mantle"])
def test_adaptive_tool_only_response_can_be_replayed_without_reasoning(backend):
    with _adapter(
        backend, _response(backend, [_call(backend)], "tool_use"), _response(backend)
    ) as (provider, sent):
        request = _request()
        first = provider.chat(request)
        assert first.content == ""
        assert first.content_blocks == [
            LLMToolUseBlock("call-1.a:b", "lookup", {"city": "Toronto"})
        ]
        request.messages += [
            LLMMessage("assistant", first.content_blocks),
            LLMMessage("user", [LLMToolResultBlock("call-1.a:b", None)]),
        ]
        assert provider.chat(request).content == "Done"
        assert sent[1]["messages"][1]["content"] == [_call(backend)]
        for invocation in sent:
            if backend == "converse":
                assert invocation["additionalModelRequestFields"] == {
                    "thinking": {"type": "adaptive"}
                }
            else:
                assert invocation["thinking"] == {"type": "adaptive"}


@pytest.mark.parametrize("backend", ["converse", "mantle"])
def test_thinking_can_start_after_a_completed_assistant_turn(backend):
    with _adapter(
        backend,
        _response(backend),
        _response(backend, [_vendor_turn(backend)[0], _text(backend, "Next answer")]),
    ) as (provider, sent):
        request = _request(thinking=False)
        first = provider.chat(request)
        assert first.content_blocks == [LLMTextBlock("Done")]
        request.messages += [
            LLMMessage("assistant", first.content_blocks),
            LLMMessage("user", "Consider the next question"),
        ]
        request.thinking = True
        final = provider.chat(request)
        assert final.content == "Next answer"
        assert final.content_blocks == [_neutral_turn()[0], LLMTextBlock("Next answer")]
        assert sent[1]["messages"][1]["content"] == [_text(backend)]
        if backend == "converse":
            assert "additionalModelRequestFields" not in sent[0]
            assert sent[1]["additionalModelRequestFields"] == {"thinking": {"type": "adaptive"}}
        else:
            assert "thinking" not in sent[0]
            assert sent[1]["thinking"] == {"type": "adaptive"}


@pytest.mark.parametrize("backend", ["converse", "mantle"])
@pytest.mark.parametrize("value", [-1, "11", 11.0, True, float("nan"), float("inf")])
def test_non_integer_or_negative_token_counts_are_rejected(backend, value):
    response = _response(backend)
    response["usage"]["inputTokens" if backend == "converse" else "input_tokens"] = value
    with _adapter(backend, response) as (provider, _):
        with pytest.raises(ProviderUnavailable):
            provider.chat(_request())


@pytest.mark.parametrize("backend", ["converse", "mantle"])
def test_no_declared_tools_cannot_produce_a_tool_call(backend):
    with _adapter(backend, _response(backend, [_call(backend)], "tool_use")) as (provider, _):
        with pytest.raises(ProviderUnavailable):
            provider.chat(_request(tools=[], messages=[LLMMessage("user", [LLMTextBlock("Hi")])]))


@pytest.mark.parametrize("backend", ["converse", "mantle"])
def test_null_tool_inputs_are_rejected(backend):
    call = _call(backend)
    (call["toolUse"] if backend == "converse" else call)["input"] = None
    with _adapter(backend, _response(backend, [call], "tool_use")) as (provider, _):
        with pytest.raises(ProviderUnavailable):
            provider.chat(_request())


@pytest.mark.parametrize("field", ["id", "type", "model"])
def test_missing_mantle_envelope_fields_are_normalized(field):
    response = _response("mantle")
    del response[field]
    with _adapter("mantle", response) as (provider, _):
        with pytest.raises(ProviderUnavailable):
            provider.chat(_request())


@pytest.mark.parametrize("caller", [{"type": "code_execution_20260120"}, [], "direct", False])
def test_mantle_server_tool_callers_are_not_silently_downgraded(caller):
    call = {**_call("mantle"), "caller": caller}
    with _adapter("mantle", _response("mantle", [call], "tool_use")) as (provider, _):
        with pytest.raises(ProviderUnavailable):
            provider.chat(_request())


def test_converse_server_tool_calls_are_not_silently_downgraded():
    call = _call("converse")
    call["toolUse"]["type"] = "server_tool_use"
    with _adapter("converse", _response("converse", [call], "tool_use")) as (provider, _):
        with pytest.raises(ProviderUnavailable):
            provider.chat(_request())


def test_mantle_tool_path_never_constructs_sdk_models_and_sets_request_timeout(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("SDK model construction must not run")

    with _adapter("mantle", _response("mantle")) as (provider, _):
        monkeypatch.setattr(provider._client, "_process_response_data", forbidden)
        create = provider._client.messages.with_raw_response.create
        options = {}

        def capture(**kwargs):
            options.update(kwargs)
            return create(**kwargs)

        monkeypatch.setattr(provider._client.messages.with_raw_response, "create", capture)
        assert provider.chat(_request()).content_blocks == [LLMTextBlock("Done")]
        assert options["timeout"] == 30.0


def test_mantle_does_not_hide_programmer_errors(monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("programmer defect")

    with _adapter("mantle", _response("mantle")) as (provider, sent):
        monkeypatch.setattr(provider._client.messages.with_raw_response, "create", broken)
        with pytest.raises(RuntimeError, match="programmer defect"):
            provider.chat(_request())
        assert sent == []


class _RawConverseBody:
    def __init__(self, body):
        self.body = body

    def stream(self):
        yield self.body


def _aws_response(request, body, status=200, headers=None):
    if not isinstance(body, bytes):
        body = json.dumps(body, separators=(",", ":")).encode("utf-8")
    return AWSResponse(
        request.url,
        status,
        {"content-type": "application/json", **(headers or {})},
        _RawConverseBody(body),
    )


@contextmanager
def _raw_converse(monkeypatch, transport, *, attempts=1):
    sdk = boto3.Session(
        aws_access_key_id="offline-access-key", aws_secret_access_key="offline-secret-key"
    ).client(
        "bedrock-runtime",
        region_name="us-east-1",
        endpoint_url="https://bedrock.invalid",
        config=Config(
            signature_version=UNSIGNED,
            proxies={},
            retries={"total_max_attempts": attempts, "mode": "standard"},
        ),
    )
    with closing(sdk):
        monkeypatch.setattr(sdk._endpoint.http_session, "send", transport)
        yield _provider("converse", sdk), sdk


def _bad_raw_converse_responses():
    yield pytest.param(200, b"\xff", id="invalid-utf8")
    yield pytest.param(400, b'{"__type":7}', id="invalid-error-type")
    yield pytest.param(400, b"[]", id="invalid-error-envelope")
    for path, value in [
        (("usage", "inputTokens"), True),
        (("usage", "outputTokens"), 7.9),
        (("usage", "totalTokens"), "18"),
        (("usage", "cacheReadInputTokens"), False),
        (("metrics", "latencyMs"), {}),
        (("metrics", "latencyMs"), -0.5),
    ]:
        response = _response("converse")
        response[path[0]][path[1]] = value
        yield pytest.param(200, response, id="-".join(path))
    yield pytest.param(
        200,
        _response("converse", [{_PRIVATE: {"value": _PRIVATE}}]),
        id="unknown-union",
    )
    yield pytest.param(
        200,
        _response("converse", [{"reasoningContent": {"redactedContent": "Z!g=="}}]),
        id="noncanonical-blob",
    )
    yield pytest.param(
        200,
        _response(
            "converse", [{"reasoningContent": {"reasoningText": {"text": None, "signature": "s"}}}]
        ),
        id="explicit-null-text",
    )
    call = _call("converse")
    call["toolUse"]["unexpected"] = _PRIVATE
    yield pytest.param(200, _response("converse", [call], "tool_use"), id="unknown-call-field")
    body = json.dumps(_response("converse"), separators=(",", ":")).encode()
    yield pytest.param(
        200,
        body.replace(b'"inputTokens":11', b'"inputTokens":true,"inputTokens":11'),
        id="duplicate-json-field",
    )
    yield pytest.param(
        400,
        {"__type": "ModelErrorException", "message": "failure", "originalStatusCode": {}},
        id="modeled-error-field",
    )


@pytest.mark.parametrize("status,body", list(_bad_raw_converse_responses()))
def test_converse_rejects_bad_http_body_before_constructing_sdk_parser(
    monkeypatch, status, body, caplog
):
    def transport(request):
        return _aws_response(request, body, status)

    def parser_must_not_run(*args, **kwargs):
        raise AssertionError("invalid raw response reached SDK parser")

    with _raw_converse(monkeypatch, transport) as (provider, sdk):
        monkeypatch.setattr(
            sdk._endpoint._response_parser_factory, "create_parser", parser_must_not_run
        )
        with pytest.raises(ProviderUnavailable) as caught:
            provider.chat(_request())
        assert _PRIVATE not in str(caught.value)
        assert _PRIVATE not in caplog.text


@pytest.mark.parametrize("separate_providers", [False, True])
def test_converse_raw_validation_is_isolated_across_concurrent_tool_and_legacy_calls(
    monkeypatch, separate_providers
):
    tool_entered = Event()
    legacy_entered = Event()
    release_legacy = Event()
    captured = []
    malformed = _response("converse")
    malformed["usage"]["inputTokens"] = True

    def transport(request):
        body = json.loads(request.body)
        captured.append(body)
        marker = body["messages"][0]["content"][0]["text"]
        if marker == "tool":
            tool_entered.set()
            assert legacy_entered.wait(5)
            return _aws_response(request, malformed)
        if marker == "legacy":
            legacy_entered.set()
            assert release_legacy.wait(5)
            return _aws_response(request, malformed)
        return _aws_response(request, _response("converse"))

    with _raw_converse(monkeypatch, transport) as (provider, sdk):
        legacy_provider = _provider("converse", sdk) if separate_providers else provider
        with ThreadPoolExecutor(max_workers=2) as executor:
            tool = executor.submit(provider.chat, _request(messages=[LLMMessage("user", "tool")]))
            assert tool_entered.wait(5)
            legacy = executor.submit(
                legacy_provider.chat,
                _request(tools=[], messages=[LLMMessage("user", "legacy")]),
            )
            try:
                assert legacy_entered.wait(5)
                with pytest.raises(ProviderUnavailable):
                    tool.result(timeout=5)
            finally:
                release_legacy.set()
            result = legacy.result(timeout=5)
        assert result.content == "Done"
        assert result.input_tokens == 1
        assert result.content_blocks is None
        assert provider.chat(_request()).content_blocks == [LLMTextBlock("Done")]
    assert len(captured) == 3
    assert "toolConfig" in captured[0]
    assert "toolConfig" not in captured[1]


@pytest.mark.parametrize(
    "failure",
    ["invalid-json", "malformed-error", "timeout", "runtime", "type", "attribute", "success"],
)
def test_converse_raw_validation_context_resets_after_every_exit(monkeypatch, failure):
    failures = {
        "timeout": botocore.exceptions.ReadTimeoutError(endpoint_url="https://bedrock.invalid"),
        "runtime": RuntimeError("programmer defect"),
        "type": TypeError("programmer defect"),
        "attribute": AttributeError("programmer defect"),
    }
    calls = []

    def transport(request):
        calls.append(request)
        if len(calls) == 1:
            if failure in failures:
                raise failures[failure]
            if failure == "invalid-json":
                return _aws_response(request, b"\xff")
            if failure == "malformed-error":
                return _aws_response(request, {"__type": []}, 400)
        body = _response("converse")
        if len(calls) == 2:
            body["usage"]["inputTokens"] = True
        return _aws_response(request, body)

    with _raw_converse(monkeypatch, transport) as (provider, _):
        if failure == "success":
            assert provider.chat(_request()).content == "Done"
        else:
            expected = ProviderUnavailable
            if failure == "timeout":
                expected = ProviderTimeout
            elif failure in failures:
                expected = type(failures[failure])
            with pytest.raises(expected) as caught:
                provider.chat(_request())
            if failure in ("runtime", "type", "attribute"):
                assert caught.value is failures[failure]
        legacy = provider.chat(_request(tools=[]))
        assert legacy.input_tokens == 1
        assert legacy.content_blocks is None
        assert provider.chat(_request()).content_blocks == [LLMTextBlock("Done")]
    assert len(calls) == 3


@pytest.mark.parametrize("separate_providers", [False, True])
def test_converse_nested_legacy_call_does_not_inherit_tool_validation(
    monkeypatch, separate_providers
):
    legacy_results = []
    body = _response("converse")
    body["usage"]["inputTokens"] = True

    def transport(request):
        marker = json.loads(request.body)["messages"][0]["content"][0]["text"]
        if marker == "outer":
            legacy_results.append(legacy_provider.chat(_request(tools=[])))
        return _aws_response(request, body)

    with _raw_converse(monkeypatch, transport) as (provider, sdk):
        legacy_provider = _provider("converse", sdk) if separate_providers else provider
        with pytest.raises(ProviderUnavailable):
            provider.chat(_request(messages=[LLMMessage("user", "outer")]))
    assert len(legacy_results) == 1
    assert legacy_results[0].input_tokens == 1
    assert legacy_results[0].content_blocks is None


@pytest.mark.parametrize(
    "body,headers",
    [
        ({"__type": "ThrottlingException", "message": "retry"}, {}),
        ({"code": "ThrottlingException", "message": "retry"}, {}),
        ({"Code": "ThrottlingException", "Message": "retry"}, {}),
        ({"message": "retry"}, {"x-amzn-errortype": "ThrottlingException:http"}),
        ({"__type": "OtherError"}, {"x-amzn-query-error": "ThrottlingException;Sender"}),
    ],
)
def test_converse_valid_error_code_sources_keep_rate_limit_mapping(monkeypatch, body, headers):
    def transport(request):
        return _aws_response(request, body, 429, headers)

    with _raw_converse(monkeypatch, transport) as (provider, _):
        with pytest.raises(ProviderRateLimited):
            provider.chat(_request())


def test_converse_valid_upstream_error_keeps_sdk_retries(monkeypatch):
    calls = []

    def transport(request):
        calls.append(request.body)
        if len(calls) == 1:
            return _aws_response(
                request, {"__type": "ThrottlingException", "message": "retry"}, 429
            )
        return _aws_response(request, _response("converse"))

    with _raw_converse(monkeypatch, transport, attempts=2) as (provider, _):
        assert provider.chat(_request()).content == "Done"
    assert len(calls) == 2
    assert calls[0] == calls[1]


def test_converse_raw_signature_only_reasoning_survives_without_body_mutation(monkeypatch):
    response = _response(
        "converse",
        [{"reasoningContent": {"reasoningText": {"signature": "opaque"}}}, _call("converse")],
        "tool_use",
    )
    body = json.dumps(response).encode()
    seen = []

    def transport(request):
        return _aws_response(request, body)

    def after_parse(response_dict, **kwargs):
        seen.append(response_dict["body"])

    with _raw_converse(monkeypatch, transport) as (provider, sdk):
        sdk.meta.events.register("response-received.bedrock-runtime.Converse", after_parse)
        result = provider.chat(_request())
    assert seen == [body]
    assert result.content_blocks == [
        LLMReasoningBlock("", "opaque"),
        LLMToolUseBlock("call-1.a:b", "lookup", {"city": "Toronto"}),
    ]


@pytest.mark.parametrize(
    "field,value,valid",
    [
        ("score", 0.75, True),
        ("score", 10**400, False),
        ("score", True, False),
        ("detected", False, True),
        ("detected", "false", False),
    ],
    ids=["double", "double-overflow", "boolean-double", "boolean", "string-boolean"],
)
def test_converse_raw_boundary_covers_ignored_nested_metadata(monkeypatch, field, value, valid):
    assessment_filter = {
        "type": "GROUNDING",
        "threshold": 0.5,
        "score": 0.75,
        "action": "NONE",
        "detected": False,
    }
    assessment_filter[field] = value
    body = _response("converse")
    body["trace"] = {
        "guardrail": {
            "outputAssessments": {
                "example": [{"contextualGroundingPolicy": {"filters": [assessment_filter]}}]
            }
        }
    }

    def transport(request):
        return _aws_response(request, body)

    def parser_must_not_run(*args, **kwargs):
        raise AssertionError("invalid raw metadata reached SDK parser")

    with _raw_converse(monkeypatch, transport) as (provider, sdk):
        if valid:
            assert provider.chat(_request()).content == "Done"
        else:
            monkeypatch.setattr(
                sdk._endpoint._response_parser_factory, "create_parser", parser_must_not_run
            )
            with pytest.raises(ProviderUnavailable):
                provider.chat(_request())


def test_converse_raw_response_byte_limit_counts_whitespace(monkeypatch):
    body = json.dumps(_response("converse")).encode()
    padded = body + b" " * (MAX_TOOL_PAYLOAD_BYTES - len(body))
    replies = [padded, padded + b" "]

    def transport(request):
        return _aws_response(request, replies.pop(0))

    with _raw_converse(monkeypatch, transport) as (provider, _):
        assert provider.chat(_request()).content == "Done"
        with pytest.raises(ProviderUnavailable):
            provider.chat(_request())
    assert replies == []
