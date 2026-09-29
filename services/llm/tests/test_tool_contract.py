import copy
import json

import pytest
from pydantic import ValidationError

from contracts.chat import ChatRequest, ChatResponse
from contracts.tool_validation import (
    MAX_CONTENT_BLOCKS,
    MAX_JSON_BYTES,
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    MAX_TOOL_MESSAGES,
    MAX_TOOL_PAYLOAD_BYTES,
    MAX_TOOLS,
)


def _tool(name="lookup"):
    return {"name": name, "input_schema": {"type": "object", "properties": {}}}


def _call(call_id="call_1", name="lookup", arguments=None):
    return {
        "type": "tool_use",
        "id": call_id,
        "name": name,
        "input": {} if arguments is None else arguments,
    }


def _result(call_id="call_1", content=None):
    return {"type": "tool_result", "tool_use_id": call_id, "content": content, "is_error": False}


def _history(calls=None, results=None):
    return {
        "tools": [_tool()],
        "messages": [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": [_call()] if calls is None else calls},
            {"role": "user", "content": [_result()] if results is None else results},
        ],
    }


def _nested(depth):
    value = None
    for _ in range(depth):
        value = [value]
    return value


def test_signed_reasoning_and_all_block_types_roundtrip_exactly():
    blocks = [
        {"type": "reasoning", "text": "  signed\nthinking é\t", "signature": " sig\nunchanged "},
        {"type": "redacted_reasoning", "data": "AP8="},
        {"type": "text", "text": "Checking now"},
        _call(arguments={"number": 1.5, "nested": [False, 0, None, "é"]}),
    ]
    payload = _history(blocks, [_result(content={"ok": True}), {"type": "text", "text": "Next"}])
    request = ChatRequest.model_validate(payload)
    assert request.model_dump()["messages"] == payload["messages"]
    assert request.tools[0].name == "lookup"
    assert request.tools[0].input_schema == payload["tools"][0]["input_schema"]


@pytest.mark.parametrize("value", [False, 0, None, "", [], {}, {"data": [True, 3.5, "é"]}])
def test_tool_result_accepts_every_json_shape_without_coercion(value):
    request = ChatRequest.model_validate(_history(results=[_result(content=value)]))
    actual = request.messages[-1].content[0].content
    assert actual == value
    assert type(actual) is type(value)


def test_parallel_results_may_reorder_calls_but_must_precede_text():
    payload = _history(
        calls=[_call("first"), {"type": "text", "text": "Also"}, _call("second")],
        results=[_result("second", False), _result("first", 0), {"type": "text", "text": "Next"}],
    )
    assert ChatRequest.model_validate(payload).model_dump()["messages"] == payload["messages"]


@pytest.mark.parametrize("tools", [None, []])
def test_structured_text_and_reasoning_do_not_require_tools(tools):
    payload = {
        "tools": tools,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "question"}]},
            {"role": "assistant", "content": [{"type": "redacted_reasoning", "data": "YQ=="}]},
            {"role": "user", "content": "continue"},
        ],
    }
    assert ChatRequest.model_validate(payload).model_dump()["messages"] == payload["messages"]


@pytest.mark.parametrize(
    "tool",
    [
        {"name": "", "input_schema": {"type": "object"}},
        {"name": "x" * 65, "input_schema": {"type": "object"}},
        {"name": "bad name", "input_schema": {"type": "object"}},
        {"name": 1, "input_schema": {"type": "object"}},
        {"name": "lookup", "input_schema": {}},
        {"name": "lookup", "input_schema": {"type": "array"}},
        {"name": "lookup", "input_schema": []},
        {"name": "lookup", "input_schema": {"type": "object", "properties": []}},
        {"name": "lookup", "input_schema": {"type": "object", "required": ["x", "x"]}},
        {"name": "lookup", "input_schema": {"type": "object", "required": [False]}},
        {**_tool(), "description": 7},
        {**_tool(), "description": ""},
        {**_tool(), "description": "x" * 4097},
        {**_tool(), "unexpected": "ignored would be unsafe"},
    ],
)
def test_invalid_tool_definition_rejected(tool):
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": "hi"}], "tools": [tool]}
        )


def test_duplicate_tool_names_rejected():
    payload = _history()
    payload["tools"].append(_tool())
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(payload)


@pytest.mark.parametrize(
    "block",
    [
        {"type": "text", "text": False},
        {"type": "text", "text": None},
        {"type": "text", "text": ""},
        {"type": "reasoning", "text": "thinking", "signature": ""},
        {"type": "reasoning", "text": 7, "signature": "signed"},
        {"type": "reasoning", "text": "thinking", "signature": False},
        {"type": "redacted_reasoning", "data": ""},
        {"type": "redacted_reasoning", "data": "Zh=="},
        {"type": "redacted_reasoning", "data": "YQ"},
        {"type": "redacted_reasoning", "data": "YQ==\n"},
        {"type": "redacted_reasoning", "data": 1},
        {**_call(), "input": []},
        {**_call(), "id": "bad id"},
        {**_call(), "id": 1},
        {**_call(), "name": "bad name"},
        {"type": "unknown-private-discriminator", "text": "hidden"},
        {"type": ["text"], "text": "hidden"},
        {"text": "no discriminator"},
        {"type": "text", "text": "hi", "unknown-private-key": "hidden"},
    ],
)
def test_invalid_block_fields_rejected(block):
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(_history(calls=[block, _call()]))


@pytest.mark.parametrize("is_error", ["false", "true", 0, 1, None])
def test_tool_result_error_flag_is_strict_boolean(is_error):
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(_history(results=[{**_result(), "is_error": is_error}]))


def test_tool_result_error_flag_defaults_to_false():
    result = _result()
    del result["is_error"]
    request = ChatRequest.model_validate(_history(results=[result]))
    assert request.messages[-1].content[0].is_error is False


@pytest.mark.parametrize(
    "role,block",
    [
        ("user", _call()),
        ("user", {"type": "reasoning", "text": "thinking", "signature": "signed"}),
        ("user", {"type": "redacted_reasoning", "data": "YQ=="}),
        ("assistant", _result()),
    ],
)
def test_role_restrictions(role, block):
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(
            {"messages": [{"role": role, "content": [block]}], "tools": [_tool()]}
        )


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "assistant", "content": [_call()]}],
        [{"role": "user", "content": [_result()]}],
        [
            {"role": "assistant", "content": [_call()]},
            {"role": "user", "content": "not a result"},
        ],
        [
            {"role": "assistant", "content": [_call()]},
            {"role": "assistant", "content": "interrupted"},
            {"role": "user", "content": [_result()]},
        ],
        [
            {"role": "assistant", "content": [_call()]},
            {"role": "user", "content": [_result("wrong")]},
        ],
        [
            {"role": "assistant", "content": [_call()]},
            {"role": "user", "content": [_result(), _result()]},
        ],
        [
            {"role": "assistant", "content": [_call(), _call()]},
            {"role": "user", "content": [_result()]},
        ],
        [
            {"role": "assistant", "content": [_call()]},
            {"role": "user", "content": [_result()]},
            {"role": "assistant", "content": [_call()]},
            {"role": "user", "content": [_result()]},
        ],
        [
            {"role": "assistant", "content": [_call(name="undeclared")]},
            {"role": "user", "content": [_result()]},
        ],
        [
            {"role": "assistant", "content": [_call()]},
            {"role": "user", "content": [{"type": "text", "text": "first"}, _result()]},
        ],
        [
            {"role": "assistant", "content": [_call("first"), _call("second")]},
            {"role": "user", "content": [_result("first")]},
            {"role": "user", "content": [_result("second")]},
        ],
        [
            {"role": "assistant", "content": [_call("first"), _call("second")]},
            {
                "role": "user",
                "content": [
                    _result("first"),
                    {"type": "text", "text": "middle"},
                    _result("second"),
                ],
            },
        ],
    ],
)
def test_invalid_tool_history_rejected(messages):
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"messages": messages, "tools": [_tool()]})


@pytest.mark.parametrize("tools", [None, []])
def test_calls_and_results_require_nonempty_tools(tools):
    payload = _history()
    payload["tools"] = tools
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(payload)


@pytest.mark.parametrize(
    "blocks",
    [
        [
            {"type": "text", "text": "first"},
            {"type": "reasoning", "text": "r", "signature": "s"},
            _call(),
        ],
        [_call(), {"type": "redacted_reasoning", "data": "YQ=="}],
        [
            {"type": "reasoning", "text": "r", "signature": "s"},
            {"type": "text", "text": "middle"},
            {"type": "redacted_reasoning", "data": "YQ=="},
            _call(),
        ],
        [_call(), {"type": "reasoning", "text": "", "signature": " opaque\nsignature "}],
    ],
)
def test_adaptive_reasoning_order_roundtrips_unchanged(blocks):
    payload = {**_history(calls=blocks), "thinking": True}
    request = ChatRequest.model_validate(payload)
    assert request.model_dump()["messages"] == payload["messages"]
    response = ChatResponse.model_validate(
        {
            "content": "".join(block["text"] for block in blocks if block["type"] == "text"),
            "model": "claude-sonnet-4-6",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "content_blocks": blocks,
        }
    )
    assert response.model_dump()["content_blocks"] == blocks


@pytest.mark.parametrize(
    "field,limit",
    [("tools", MAX_TOOLS), ("messages", MAX_TOOL_MESSAGES), ("blocks", MAX_CONTENT_BLOCKS)],
)
def test_collection_limits_accept_boundary_reject_overflow(field, limit):
    payload = {"messages": [{"role": "user", "content": "hi"}], "tools": [_tool()]}
    if field == "tools":
        payload["tools"] = [_tool(f"tool_{i}") for i in range(limit)]
        target = payload["tools"]
        extra = _tool("overflow")
    elif field == "messages":
        payload["messages"] *= limit
        target = payload["messages"]
        extra = {"role": "user", "content": "hi"}
    else:
        payload["messages"][0]["content"] = [{"type": "text", "text": "hi"}] * limit
        target = payload["messages"][0]["content"]
        extra = {"type": "text", "text": "hi"}
    ChatRequest.model_validate(payload)
    target.append(extra)
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(payload)


def test_empty_content_list_rejected():
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"messages": [{"role": "user", "content": []}]})


@pytest.mark.parametrize("location", ["schema", "input", "result"])
@pytest.mark.parametrize(
    "invalid_value",
    [float("nan"), float("inf"), float("-inf"), "\ud800", {"\ud800": "bad key"}],
)
def test_non_json_values_rejected(location, invalid_value):
    payload = _history()
    if location == "schema":
        payload["tools"][0]["input_schema"]["default"] = invalid_value
    elif location == "input":
        payload["messages"][1]["content"][0]["input"] = {"value": invalid_value}
    else:
        payload["messages"][2]["content"][0]["content"] = invalid_value
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(payload)


@pytest.mark.parametrize("location", ["schema", "input", "result"])
@pytest.mark.parametrize(
    "value",
    ["é" * (MAX_JSON_BYTES // 2), _nested(MAX_JSON_DEPTH + 1), [0] * MAX_JSON_NODES],
    ids=["bytes", "depth", "nodes"],
)
def test_each_json_value_is_bounded(location, value):
    payload = _history()
    if location == "schema":
        payload["tools"][0]["input_schema"]["default"] = value
    elif location == "input":
        payload["messages"][1]["content"][0]["input"] = {"value": value}
    else:
        payload["messages"][2]["content"][0]["content"] = value
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(payload)


def test_tool_payload_limit_counts_all_utf8_fields_and_preserves_legacy_limits():
    payload = {"messages": [{"role": "user", "content": "é" * (MAX_TOOL_PAYLOAD_BYTES // 2)}]}
    ChatRequest.model_validate(payload)
    for tool_payload in [
        {**payload, "tools": [_tool()]},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": payload["messages"][0]["content"]}],
                }
            ]
        },
    ]:
        with pytest.raises(ValidationError):
            ChatRequest.model_validate(tool_payload)


def test_aggregate_tool_payload_exact_byte_boundary():
    payload = {"tools": [_tool()], "messages": [{"role": "user", "content": "hi"}], "system": ""}
    overhead = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    payload["system"] = "a" * (MAX_TOOL_PAYLOAD_BYTES - overhead)
    ChatRequest.model_validate(payload)
    payload["system"] += "a"
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(payload)


@pytest.mark.parametrize("default_field", ["system", "is_error"])
def test_normalized_payload_limits_account_for_default_fields(default_field):
    if default_field == "system":
        payload = {"tools": [_tool()], "messages": [{"role": "user", "content": ""}]}
        overhead = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        payload["messages"][0]["content"] = "a" * (MAX_TOOL_PAYLOAD_BYTES - overhead)
    else:
        payload = {**_history(), "system": ""}
        del payload["messages"][2]["content"][0]["is_error"]
        overhead = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        payload["system"] = "a" * (MAX_TOOL_PAYLOAD_BYTES - overhead)
    with pytest.raises(ValidationError, match="JSON exceeds size limit"):
        ChatRequest.model_validate(payload)


def test_legacy_message_count_not_changed():
    ChatRequest.model_validate(
        {"messages": [{"role": "user", "content": "hi"}] * (MAX_TOOL_MESSAGES + 1)}
    )


def test_response_blocks_forbid_tool_results():
    payload = {
        "content": "",
        "model": "claude-sonnet-4-6",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "content_blocks": [_result()],
    }
    with pytest.raises(ValidationError):
        ChatResponse.model_validate(payload)


def test_optional_response_blocks_do_not_change_required_fields():
    assert set(ChatResponse.model_json_schema()["required"]) == {
        "content",
        "model",
        "stop_reason",
        "usage",
    }
    payload = _history()
    original = copy.deepcopy(payload)
    ChatRequest.model_validate(payload)
    assert payload == original
