import base64
import binascii
import json
import math
import re
from typing import TypeAlias

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]

MAX_TOOLS = 32
MAX_TOOL_MESSAGES = 256
MAX_CONTENT_BLOCKS = 128
MAX_JSON_BYTES = 65_536
MAX_TOOL_PAYLOAD_BYTES = 1_048_576
MAX_JSON_DEPTH = 20
MAX_JSON_NODES = 10_000
TOOL_NAME_PATTERN = r"[a-zA-Z0-9_-]{1,64}"
TOOL_ID_PATTERN = r"[a-zA-Z0-9_.:-]{1,64}"


def validate_identifier(value: str, *, tool_name: bool = False) -> str:
    pattern = TOOL_NAME_PATTERN if tool_name else TOOL_ID_PATTERN
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError("invalid tool name" if tool_name else "invalid tool-use identifier")
    return value


def validate_json_value(
    value: JsonValue,
    *,
    max_bytes: int = MAX_JSON_BYTES,
    max_depth: int = MAX_JSON_DEPTH,
    max_nodes: int = MAX_JSON_NODES,
) -> JsonValue:
    pending = [(value, 0)]
    nodes = 0
    string_bytes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > max_depth or nodes > max_nodes:
            raise ValueError("JSON exceeds structural limits")
        if item is None or type(item) in (bool, int):
            continue
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("JSON numbers must be finite")
        elif isinstance(item, str):
            try:
                string_bytes += len(item.encode("utf-8"))
            except UnicodeEncodeError:
                raise ValueError("JSON strings must be valid UTF-8") from None
            if string_bytes > max_bytes:
                raise ValueError("JSON exceeds size limit")
        elif isinstance(item, (dict, list)):
            if len(item) + nodes + len(pending) > max_nodes:
                raise ValueError("JSON exceeds structural limits")
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise ValueError("JSON object keys must be strings")
                pending.extend((key, depth + 1) for key in item)
                pending.extend((child, depth + 1) for child in item.values())
            else:
                pending.extend((child, depth + 1) for child in item)
        else:
            raise ValueError("value must contain only JSON data")
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        raise ValueError("value must contain serializable JSON data") from None
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError("JSON exceeds size limit")
    return value


def validate_input_schema(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or value.get("type") != "object":
        raise ValueError("input_schema must be an object-typed JSON schema")
    validate_json_value(value)
    if "properties" in value and not isinstance(value["properties"], dict):
        raise ValueError("schema properties must be an object")
    if "required" in value:
        required = value["required"]
        if (
            not isinstance(required, list)
            or any(not isinstance(name, str) for name in required)
            or len(required) != len(set(required))
        ):
            raise ValueError("schema required must contain unique strings")
    return value


def validate_redacted_data(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("redacted reasoning must be non-empty base64")
    if len(value) > MAX_TOOL_PAYLOAD_BYTES:
        raise ValueError("redacted reasoning exceeds size limit")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("redacted reasoning must be valid base64") from None
    if not decoded or base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("redacted reasoning must be canonical base64")
    return value
