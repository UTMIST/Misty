import base64

import pytest

from contracts.tool_validation import (
    MAX_JSON_DEPTH,
    validate_identifier,
    validate_input_schema,
    validate_json_value,
    validate_redacted_data,
)


@pytest.mark.parametrize(
    "value",
    [None, False, True, 0, -1, 1.25, "", "café 雪", [], {}, {"a": [None, False, 0]}],
)
def test_json_values_are_preserved_without_coercion(value):
    assert validate_json_value(value) is value


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), -float("inf"), {"nested": [float("nan")]}],
)
def test_nonfinite_json_is_rejected(value):
    with pytest.raises(ValueError, match="finite"):
        validate_json_value(value)


@pytest.mark.parametrize("value", [b"data", (), {"a"}, {1: "value"}, {"x": object()}])
def test_non_json_types_are_rejected(value):
    with pytest.raises(ValueError):
        validate_json_value(value)


@pytest.mark.parametrize("value", ["\ud800", {"\udfff": "value"}, {"a": ["\ud800"]}])
def test_surrogates_are_rejected_without_echoing_data(value):
    with pytest.raises(ValueError, match="valid UTF-8") as caught:
        validate_json_value(value)
    assert "\\ud" not in str(caught.value)


def test_json_depth_boundary():
    value = "leaf"
    for _ in range(MAX_JSON_DEPTH):
        value = [value]
    assert validate_json_value(value) is value
    with pytest.raises(ValueError, match="structural"):
        validate_json_value([value])


def test_cycles_and_oversized_collections_fail_with_bounded_traversal():
    value = []
    value.append(value)
    with pytest.raises(ValueError, match="structural"):
        validate_json_value(value)
    with pytest.raises(ValueError, match="structural"):
        validate_json_value([0] * 10_001)
    with pytest.raises(ValueError, match="structural"):
        validate_json_value({str(i): i for i in range(6_000)})


@pytest.mark.parametrize("value,size", [("abc", 5), ("雪", 5), ("\n", 4)])
def test_size_limit_uses_compact_utf8_json_bytes(value, size):
    assert validate_json_value(value, max_bytes=size) == value
    with pytest.raises(ValueError, match="size"):
        validate_json_value(value, max_bytes=size - 1)


def test_large_integer_serialization_failure_is_normalized():
    with pytest.raises(ValueError, match="serializable JSON"):
        validate_json_value(10**5_000)


@pytest.mark.parametrize("name", ["lookup", "tool_1", "Tool-1", "a" * 64])
def test_valid_tool_names(name):
    assert validate_identifier(name, tool_name=True) == name


@pytest.mark.parametrize("value", ["", "a" * 65, "bad.name", "bad:name", "name\n", True, 7])
def test_invalid_tool_names(value):
    with pytest.raises(ValueError, match="invalid tool name"):
        validate_identifier(value, tool_name=True)


@pytest.mark.parametrize("value", ["call_1", "call.1:part-2", "a" * 64])
def test_tool_ids_match_current_converse_character_set(value):
    assert validate_identifier(value) == value


@pytest.mark.parametrize("value", ["", "a" * 65, "bad/name", "雪", "id\n", False])
def test_invalid_tool_ids(value):
    with pytest.raises(ValueError, match="identifier"):
        validate_identifier(value)


def test_valid_object_schema_remains_unchanged_and_refs_are_not_resolved():
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "$ref": "https://example.invalid/not-fetched",
    }
    assert validate_input_schema(schema) is schema


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        [],
        {},
        {"type": "array"},
        {"type": "object", "properties": []},
        {"type": "object", "required": "query"},
        {"type": "object", "required": ["query", "query"]},
        {"type": "object", "required": [[]]},
        {"type": "object", "required": [True]},
    ],
)
def test_invalid_schema_structure_is_rejected(value):
    with pytest.raises(ValueError):
        validate_input_schema(value)


def test_redacted_data_roundtrips_binary_without_interpretation():
    value = base64.b64encode(b"\x00\xff\x01\x80").decode("ascii")
    assert validate_redacted_data(value) is value


@pytest.mark.parametrize("value", ["", "not base64!", "AA", "AB==", "AA==\n", "雪", b"AA=="])
def test_invalid_or_noncanonical_redacted_data_is_rejected(value):
    with pytest.raises(ValueError):
        validate_redacted_data(value)


def test_empty_structured_text_is_rejected_at_neutral_request_boundary():
    from src.providers.base import LLMMessage, LLMRequest, LLMTextBlock
    from src.providers.tool_blocks import validate_tool_request

    request = LLMRequest(messages=[LLMMessage("user", [LLMTextBlock("")])])
    with pytest.raises(ValueError, match="string"):
        validate_tool_request(request)


def test_empty_structured_text_is_rejected_at_neutral_response_boundary():
    from src.providers.base import LLMTextBlock
    from src.providers.tool_blocks import validate_response_blocks

    with pytest.raises(ValueError, match="string"):
        validate_response_blocks(
            [LLMTextBlock("")], tool_names=set(), used_ids=set(), stop_reason="end_turn"
        )
