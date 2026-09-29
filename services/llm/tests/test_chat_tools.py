import copy
import json

import pytest
from fastapi.testclient import TestClient

from platform_auth import InMemoryKeyStore

from src.api.deps import get_key_store, get_llm
from src.api.hashing import generate_key
from src.providers.base import (
    LLMMessage,
    LLMReasoningBlock,
    LLMRedactedReasoningBlock,
    LLMResult,
    LLMTextBlock,
    LLMTool,
    LLMToolResultBlock,
    LLMToolUseBlock,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)


class _FakeProvider:
    def __init__(self, result=None, exc=None):
        self.result = _ok_result() if result is None else result
        self.exc = exc
        self.requests = []

    def chat(self, request):
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        return self.result


@pytest.fixture
def env_key(monkeypatch):
    monkeypatch.setenv("API_KEY", "offline-tool-contract-key")
    monkeypatch.setenv("LLM_ENV", "local")
    monkeypatch.setenv("CONSUMER_KEYS", "")
    monkeypatch.setenv("THINKING_DEFAULT", "true")
    from src.config import get_settings

    get_settings.cache_clear()
    return "offline-tool-contract-key"


def _client(env_key, provider, store=None):
    from src.api.app import create_app

    app = create_app()
    app.dependency_overrides[get_key_store] = lambda: store or InMemoryKeyStore()
    app.dependency_overrides[get_llm] = lambda: provider
    return TestClient(app, raise_server_exceptions=False), {"X-API-Key": env_key}


def _ok_result():
    return LLMResult(
        content="hello",
        model="claude-sonnet-4-6",
        stop_reason="end_turn",
        input_tokens=5,
        output_tokens=3,
        content_blocks=[LLMTextBlock("hello")],
    )


def _tools():
    return [{"name": "lookup", "input_schema": {"type": "object"}, "description": "PRIVATE-SCHEMA"}]


def _initial():
    return {"messages": [{"role": "user", "content": "PRIVATE-PROMPT"}], "tools": _tools()}


def _continuation():
    return {
        **_initial(),
        "messages": [
            {"role": "user", "content": "PRIVATE-PROMPT"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "reasoning",
                        "text": "  PRIVATE-REASONING\n",
                        "signature": " sig\n é ",
                    },
                    {"type": "redacted_reasoning", "data": "AP8="},
                    {"type": "text", "text": "PRIVATE-ASSISTANT"},
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "lookup",
                        "input": {"q": "PRIVATE-ARGUMENT"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_1",
                        "content": {"answer": "PRIVATE-RESULT"},
                    }
                ],
            },
        ],
    }


@pytest.mark.parametrize("tools", ["absent", None, []])
@pytest.mark.parametrize("blocks", [None, [LLMTextBlock("hello")]])
def test_legacy_json_bytes_unchanged_with_or_without_neutral_blocks(env_key, tools, blocks):
    provider = _FakeProvider()
    provider.result.content_blocks = blocks
    client, headers = _client(env_key, provider)
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    if tools != "absent":
        payload["tools"] = tools
    response = client.post("/chat", headers=headers, json=payload)
    assert response.status_code == 200
    assert response.content == (
        b'{"content":"hello","model":"claude-sonnet-4-6","stop_reason":"end_turn",'
        b'"usage":{"input_tokens":5,"output_tokens":3}}'
    )
    assert provider.requests[0].messages == [LLMMessage("user", "hi")]
    assert provider.requests[0].tools == []
    assert provider.requests[0].thinking is True


def test_initial_tool_request_and_interleaved_assistant_response_are_translated(env_key):
    provider = _FakeProvider(
        result=LLMResult(
            content="Checking",
            model="claude-sonnet-4-6",
            stop_reason="tool_use",
            input_tokens=5,
            output_tokens=3,
            content_blocks=[
                LLMTextBlock("Checking"),
                LLMToolUseBlock("call_1", "lookup", {"q": [False, 0, None], "nullable": None}),
                LLMReasoningBlock("  thinking\n", " signed\t"),
                LLMToolUseBlock("call_2", "lookup", {}),
                LLMRedactedReasoningBlock("AP8="),
            ],
        )
    )
    client, headers = _client(env_key, provider)
    payload = {**_initial(), "model": "claude-opus-4-6", "system": "system", "max_tokens": 500}
    response = client.post("/chat", headers=headers, json=payload)
    assert response.status_code == 200
    assert response.json()["content_blocks"] == [
        {"type": "text", "text": "Checking"},
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "lookup",
            "input": {"q": [False, 0, None], "nullable": None},
        },
        {"type": "reasoning", "text": "  thinking\n", "signature": " signed\t"},
        {"type": "tool_use", "id": "call_2", "name": "lookup", "input": {}},
        {"type": "redacted_reasoning", "data": "AP8="},
    ]
    request = provider.requests[0]
    assert request.tools == [LLMTool("lookup", {"type": "object"}, "PRIVATE-SCHEMA")]
    assert request.messages == [LLMMessage("user", "PRIVATE-PROMPT")]
    assert request.model == "claude-opus-4-6"
    assert request.system == "system"
    assert request.max_tokens == 500
    assert request.thinking is True
    payload["messages"].extend(
        [
            {"role": "assistant", "content": response.json()["content_blocks"]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call_2", "content": False},
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_1",
                        "content": {"error": "offline failure"},
                        "is_error": True,
                    },
                    {"type": "text", "text": "Continue"},
                ],
            },
        ]
    )
    assistant_blocks = provider.result.content_blocks
    provider.result = _ok_result()
    continuation = client.post("/chat", headers=headers, json=payload)
    assert continuation.status_code == 200
    assert len(provider.requests) == 2
    assert provider.requests[1].messages[-2].content == assistant_blocks
    assert provider.requests[1].messages[-1].content == [
        LLMToolResultBlock("call_2", False),
        LLMToolResultBlock("call_1", {"error": "offline failure"}, is_error=True),
        LLMTextBlock("Continue"),
    ]


@pytest.mark.parametrize("tools", [None, []])
def test_structured_message_alone_enables_tool_mode(env_key, tools):
    provider = _FakeProvider()
    client, headers = _client(env_key, provider)
    response = client.post(
        "/chat",
        headers=headers,
        json={
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            "tools": tools,
        },
    )
    assert response.status_code == 200
    assert response.json()["content_blocks"] == [{"type": "text", "text": "hello"}]
    assert provider.requests[0].messages == [LLMMessage("user", [LLMTextBlock("hi")])]
    assert provider.requests[0].tools == []


@pytest.mark.parametrize("tools", [None, [], _tools()])
def test_empty_structured_text_422_before_provider(env_key, tools):
    provider = _FakeProvider()
    client, headers = _client(env_key, provider)
    response = client.post(
        "/chat",
        headers=headers,
        json={
            "messages": [{"role": "user", "content": [{"type": "text", "text": ""}]}],
            "tools": tools,
        },
    )
    assert response.status_code == 422
    assert not provider.requests


@pytest.mark.parametrize("value", [False, 0, None, "", [], {"nested": [True, 1.5]}])
def test_continuation_preserves_signed_reasoning_and_result_json(env_key, value):
    provider = _FakeProvider()
    client, headers = _client(env_key, provider)
    payload = _continuation()
    payload["messages"][-1]["content"][0]["content"] = value
    payload["messages"][-1]["content"].append({"type": "text", "text": "Continue"})
    response = client.post("/chat", headers=headers, json=payload)
    assert response.status_code == 200
    assert provider.requests[0].messages[1].content == [
        LLMReasoningBlock("  PRIVATE-REASONING\n", " sig\n é "),
        LLMRedactedReasoningBlock("AP8="),
        LLMTextBlock("PRIVATE-ASSISTANT"),
        LLMToolUseBlock("call_1", "lookup", {"q": "PRIVATE-ARGUMENT"}),
    ]
    assert provider.requests[0].messages[2].content == [
        LLMToolResultBlock("call_1", value),
        LLMTextBlock("Continue"),
    ]
    assert type(provider.requests[0].messages[2].content[0].content) is type(value)


@pytest.mark.parametrize(
    "default,override",
    [(True, None), (True, True), (False, True), (False, None), (True, False)],
)
def test_adaptive_tool_only_replay_preserves_resolved_thinking_setting(
    env_key, monkeypatch, default, override
):
    monkeypatch.setenv("THINKING_DEFAULT", str(default).lower())
    provider = _FakeProvider()
    client, headers = _client(env_key, provider)
    payload = _continuation()
    payload["messages"][1]["content"] = payload["messages"][1]["content"][-1:]
    payload["thinking"] = override
    response = client.post("/chat", headers=headers, json=payload)
    assert response.status_code == 200
    assert len(provider.requests) == 1
    assert provider.requests[0].thinking is (default if override is None else override)
    assert provider.requests[0].messages[1].content == [
        LLMToolUseBlock("call_1", "lookup", {"q": "PRIVATE-ARGUMENT"})
    ]


def test_thinking_can_change_after_completed_tool_turn(env_key):
    provider = _FakeProvider()
    client, headers = _client(env_key, provider)
    payload = _continuation()
    payload["messages"][1]["content"] = payload["messages"][1]["content"][-1:]
    payload["thinking"] = False
    completed = client.post("/chat", headers=headers, json=payload)
    assert completed.status_code == 200
    payload["messages"].extend(
        [
            {"role": "assistant", "content": completed.json()["content_blocks"]},
            {"role": "user", "content": "A new ordinary question"},
        ]
    )
    payload["thinking"] = True
    response = client.post("/chat", headers=headers, json=payload)
    assert response.status_code == 200
    assert len(provider.requests) == 2
    assert provider.requests[0].thinking is False
    assert provider.requests[1].thinking is True
    assert provider.requests[1].messages[1].content == provider.requests[0].messages[1].content
    assert provider.requests[1].messages[-1] == LLMMessage("user", "A new ordinary question")


def test_redacted_reasoning_preserved_without_textual_reasoning(env_key):
    provider = _FakeProvider()
    client, headers = _client(env_key, provider)
    payload = _continuation()
    payload["messages"][1]["content"] = payload["messages"][1]["content"][1:]
    response = client.post("/chat", headers=headers, json=payload)
    assert response.status_code == 200
    assert provider.requests[0].messages[1].content[0] == LLMRedactedReasoningBlock("AP8=")


@pytest.mark.parametrize(
    "location,value",
    [
        ("input", float("nan")),
        ("input", float("inf")),
        ("result", float("-inf")),
        ("result", "\ud800"),
        ("schema", {"\ud800": "PRIVATE-KEY"}),
        ("schema", float("nan")),
        ("system", "\ud800"),
        ("tag", "PRIVATE-DISCRIMINATOR"),
        ("tag", "\ud800"),
        ("tag", {"PRIVATE-DISCRIMINATOR": 1}),
        ("extra", "PRIVATE-EXTRA-FIELD"),
    ],
)
def test_invalid_tool_payload_safely_422_without_echo_or_provider_call(
    env_key, capsys, caplog, location, value
):
    provider = _FakeProvider()
    client, headers = _client(env_key, provider)
    payload = _continuation()
    if location == "input":
        payload["messages"][1]["content"][-1]["input"]["bad"] = value
    elif location == "result":
        payload["messages"][2]["content"][0]["content"] = value
    elif location == "schema":
        payload["tools"][0]["input_schema"]["default"] = value
    elif location == "system":
        payload["system"] = value
    elif location == "tag":
        payload["messages"][1]["content"][-1]["type"] = value
    else:
        payload["tools"][0][value] = "PRIVATE-EXTRA-VALUE"
    response = client.post(
        "/chat",
        headers={**headers, "Content-Type": "application/json"},
        content=json.dumps(payload, allow_nan=True, ensure_ascii=True).encode("ascii"),
    )
    assert response.status_code == 422
    assert not provider.requests
    observed = response.text + capsys.readouterr().out + caplog.text
    assert "PRIVATE-" not in observed
    assert env_key not in observed
    assert all(set(error) == {"type", "loc", "msg"} for error in response.json()["detail"])


@pytest.mark.parametrize(
    "mutation",
    ["orphan", "unanswered", "mismatch", "no-tools", "interrupted", "text-first", "duplicate"],
)
def test_invalid_history_rejected_before_provider(env_key, mutation):
    provider = _FakeProvider()
    client, headers = _client(env_key, provider)
    payload = _continuation()
    if mutation == "orphan":
        del payload["messages"][1]
    elif mutation == "unanswered":
        payload["messages"].pop()
    elif mutation == "mismatch":
        payload["messages"][-1]["content"][0]["tool_use_id"] = "different"
    elif mutation == "no-tools":
        del payload["tools"]
    elif mutation == "interrupted":
        payload["messages"].insert(2, {"role": "assistant", "content": "interruption"})
    elif mutation == "text-first":
        payload["messages"][-1]["content"].insert(0, {"type": "text", "text": "too early"})
    else:
        payload["messages"][-1]["content"] *= 2
    response = client.post("/chat", headers=headers, json=payload)
    assert response.status_code == 422
    assert not provider.requests


@pytest.mark.parametrize(
    "scopes,status", [(None, 401), ([], 403), (["chat"], 200), (["admin"], 200)]
)
def test_tool_mode_keeps_chat_scope_authorization(env_key, scopes, status):
    store = InMemoryKeyStore()
    provider = _FakeProvider()
    client, _ = _client(env_key, provider, store)
    headers = {}
    if scopes is not None:
        plaintext, prefix, key_hash = generate_key()
        store.add(prefix=prefix, key_hash=key_hash, name="consumer", scopes=scopes)
        headers["X-API-Key"] = plaintext
    response = client.post("/chat", headers=headers, json=_initial())
    assert response.status_code == status
    assert len(provider.requests) == (1 if status == 200 else 0)


def test_audit_log_omits_tool_data_on_success(env_key, capsys):
    provider = _FakeProvider()
    client, headers = _client(env_key, provider)
    response = client.post("/chat", headers=headers, json=_continuation())
    assert response.status_code == 200
    output = capsys.readouterr().out
    assert "PRIVATE-" not in output
    assert env_key not in output
    entry = json.loads(output.strip().splitlines()[-1])
    assert entry["model"] == "claude-sonnet-4-6"
    assert entry["input_tokens"] == 5
    assert entry["output_tokens"] == 3


@pytest.mark.parametrize(
    "exc,status", [(ProviderRateLimited, 429), (ProviderTimeout, 504), (ProviderUnavailable, 502)]
)
def test_provider_errors_stay_normalized_and_private(env_key, capsys, exc, status):
    provider = _FakeProvider(exc=exc("PRIVATE-UPSTREAM-ERROR"))
    client, headers = _client(env_key, provider)
    response = client.post("/chat", headers=headers, json=_initial())
    assert response.status_code == status
    assert "PRIVATE-" not in response.text + capsys.readouterr().out


@pytest.mark.parametrize(
    "blocks,content,stop_reason",
    [
        ([LLMToolResultBlock("call_1", "PRIVATE-UPSTREAM")], "", "end_turn"),
        ([{"type": "text", "text": "PRIVATE-UPSTREAM"}], "", "end_turn"),
        ([LLMToolUseBlock("call_1", "undeclared", {})], "", "tool_use"),
        ([LLMToolUseBlock("bad id", "lookup", {})], "", "tool_use"),
        (
            [LLMToolUseBlock("call_1", "lookup", {}), LLMToolUseBlock("call_1", "lookup", {})],
            "",
            "tool_use",
        ),
        ([LLMToolUseBlock("call_1", "lookup", {"secret": float("nan")})], "", "tool_use"),
        ([LLMToolUseBlock("call_1", "lookup", {"secret": "\ud800"})], "", "tool_use"),
        ([LLMToolUseBlock("call_1", "lookup", [])], "", "tool_use"),
        ([LLMReasoningBlock("PRIVATE-UPSTREAM", "")], "", "end_turn"),
        ([LLMRedactedReasoningBlock("Zh==")], "", "end_turn"),
        ([LLMTextBlock("\ud800")], "\ud800", "end_turn"),
        ([LLMTextBlock(False)], "", "end_turn"),
        ([LLMTextBlock("")], "", "end_turn"),
        ([LLMTextBlock("PRIVATE-UPSTREAM")], "different", "end_turn"),
        ([LLMTextBlock("hello")], "hello", "tool_use"),
        (None, "", "tool_use"),
        ([], "", "end_turn"),
        ([LLMTextBlock("a")] * 129, "a" * 129, "end_turn"),
    ],
)
def test_invalid_neutral_response_becomes_private_502(
    env_key, capsys, caplog, blocks, content, stop_reason
):
    result = _ok_result()
    result.content_blocks = copy.deepcopy(blocks)
    result.content = content
    result.stop_reason = stop_reason
    provider = _FakeProvider(result=result)
    client, headers = _client(env_key, provider)
    response = client.post("/chat", headers=headers, json=_initial())
    assert response.status_code == 502
    assert response.json() == {"detail": "LLM provider error"}
    assert len(provider.requests) == 1
    assert "PRIVATE-" not in response.text + capsys.readouterr().out + caplog.text


@pytest.mark.parametrize("mode", ["tools", "structured"])
@pytest.mark.parametrize("blocks", [None, []])
def test_extended_final_response_requires_nonempty_blocks(env_key, mode, blocks):
    provider = _FakeProvider()
    provider.result.content_blocks = blocks
    client, headers = _client(env_key, provider)
    payload = _initial()
    if mode == "structured":
        del payload["tools"]
        payload["messages"][0]["content"] = [{"type": "text", "text": "hi"}]
    response = client.post("/chat", headers=headers, json=payload)
    assert response.status_code == 502
    assert response.json() == {"detail": "LLM provider error"}
    assert len(provider.requests) == 1


def test_invalid_neutral_usage_is_not_written_to_audit_log(env_key, capsys):
    result = _ok_result()
    result.input_tokens = "PRIVATE-UPSTREAM-USAGE"
    client, headers = _client(env_key, _FakeProvider(result=result))
    response = client.post("/chat", headers=headers, json=_initial())
    assert response.status_code == 502
    assert "PRIVATE-" not in response.text + capsys.readouterr().out


@pytest.mark.parametrize("limit", ["tools", "messages", "blocks", "schema", "payload"])
def test_tool_limits_422_before_provider(env_key, limit):
    provider = _FakeProvider()
    client, headers = _client(env_key, provider)
    payload = _initial()
    if limit == "tools":
        payload["tools"] = [{**_tools()[0], "name": f"tool_{i}"} for i in range(33)]
    elif limit == "messages":
        payload["messages"] *= 257
    elif limit == "blocks":
        payload["messages"][0]["content"] = [{"type": "text", "text": "hi"}] * 129
    elif limit == "schema":
        payload["tools"][0]["input_schema"]["description"] = "x" * 65_536
    else:
        payload["system"] = "x" * 1_048_576
    response = client.post("/chat", headers=headers, json=payload)
    assert response.status_code == 422
    assert not provider.requests
    assert "PRIVATE-" not in response.text


@pytest.mark.parametrize(
    "blocks",
    [
        [LLMReasoningBlock("thinking only", "signature")],
        [LLMReasoningBlock("", " signature\nonly ")],
        [LLMRedactedReasoningBlock("YQ==")],
    ],
)
def test_reasoning_only_response_preserves_empty_content(env_key, blocks):
    result = _ok_result()
    result.content = ""
    result.content_blocks = blocks
    client, headers = _client(env_key, _FakeProvider(result=result))
    response = client.post("/chat", headers=headers, json=_initial())
    assert response.status_code == 200
    assert response.json()["content"] == ""
    assert len(response.json()["content_blocks"]) == 1
    assert response.json()["usage"] == {"input_tokens": 5, "output_tokens": 3}
    if isinstance(blocks[0], LLMReasoningBlock):
        assert response.json()["content_blocks"] == [
            {"type": "reasoning", "text": blocks[0].text, "signature": blocks[0].signature}
        ]
    else:
        assert response.json()["content_blocks"] == [
            {"type": "redacted_reasoning", "data": blocks[0].data}
        ]


@pytest.mark.parametrize("usage", [False, -1, 1.5, float("nan"), float("inf")])
def test_invalid_neutral_usage_types_become_502(env_key, usage):
    result = _ok_result()
    result.output_tokens = usage
    client, headers = _client(env_key, _FakeProvider(result=result))
    response = client.post("/chat", headers=headers, json=_initial())
    assert response.status_code == 502
    assert response.json() == {"detail": "LLM provider error"}


def test_provider_cannot_reuse_a_historical_call_id(env_key):
    result = _ok_result()
    result.content = ""
    result.stop_reason = "tool_use"
    result.content_blocks = [LLMToolUseBlock("call_1", "lookup", {})]
    client, headers = _client(env_key, _FakeProvider(result=result))
    response = client.post("/chat", headers=headers, json=_continuation())
    assert response.status_code == 502
    assert response.json() == {"detail": "LLM provider error"}


def test_upstream_aggregate_payload_is_bounded(env_key):
    result = _ok_result()
    result.content_blocks = [LLMTextBlock("x" * 10_000)] * 100
    result.content = "x" * 1_000_000
    client, headers = _client(env_key, _FakeProvider(result=result))
    response = client.post("/chat", headers=headers, json=_initial())
    assert response.status_code == 502


def test_missing_neutral_result_is_502(env_key):
    provider = _FakeProvider()
    provider.result = None
    client, headers = _client(env_key, provider)
    response = client.post("/chat", headers=headers, json=_initial())
    assert response.status_code == 502
    assert response.json() == {"detail": "LLM provider error"}


def test_legacy_validation_and_extra_field_behavior_remain(env_key):
    provider = _FakeProvider()
    client, headers = _client(env_key, provider)
    payload = {
        "messages": [{"role": "user", "content": "hi", "ignored": "value"}],
        "ignored": "value",
    }
    assert client.post("/chat", headers=headers, json=payload).status_code == 200
    assert len(provider.requests) == 1
    for invalid in [
        {**payload, "model": "undeclared-model"},
        {**payload, "max_tokens": 0},
        {**payload, "messages": []},
        {**payload, "messages": [{"role": "user", "content": ""}]},
    ]:
        assert client.post("/chat", headers=headers, json=invalid).status_code == 422
    assert len(provider.requests) == 1


def test_openapi_describes_discriminated_request_and_assistant_only_response(env_key):
    client, _ = _client(env_key, _FakeProvider())
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schemas = response.json()["components"]["schemas"]
    assert "tools" in schemas["ChatRequest"]["properties"]
    response_blocks = schemas["ChatResponse"]["properties"]["content_blocks"]
    variants = next(item for item in response_blocks["anyOf"] if item.get("type") == "array")
    assert set(variants["items"]["discriminator"]["mapping"]) == {
        "text",
        "reasoning",
        "redacted_reasoning",
        "tool_use",
    }
