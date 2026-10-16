from contracts.tool_validation import (
    MAX_CONTENT_BLOCKS,
    MAX_JSON_DEPTH,
    MAX_TOOL_MESSAGES,
    MAX_TOOL_PAYLOAD_BYTES,
    MAX_TOOLS,
    validate_identifier,
    validate_input_schema,
    validate_json_value,
    validate_redacted_data,
)
from src.providers.base import (
    LLMContentBlock,
    LLMMessage,
    LLMReasoningBlock,
    LLMRedactedReasoningBlock,
    LLMRequest,
    LLMTextBlock,
    LLMTool,
    LLMToolResultBlock,
    LLMToolUseBlock,
)


def tool_mode(request: LLMRequest) -> bool:
    return bool(request.tools) or any(isinstance(m.content, list) for m in request.messages)


def object_fields(value, required: set[str], optional: set[str] | None = None) -> dict:
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or value.keys() - required - (optional or set())
    ):
        raise ValueError("invalid content block fields")
    return value


def content_list(value, *, allow_empty: bool = False) -> list:
    if (
        not isinstance(value, list)
        or not (0 if allow_empty else 1) <= len(value) <= MAX_CONTENT_BLOCKS
    ):
        raise ValueError("invalid content block list")
    return value


def text_value(value, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise ValueError("invalid string value")
    validate_json_value(value, max_bytes=MAX_TOOL_PAYLOAD_BYTES)
    return value


def response_blocks(
    blocks: list[LLMContentBlock] | None, stop_reason: str
) -> list[LLMContentBlock]:
    blocks = content_list(blocks, allow_empty=True)
    text_value(stop_reason, nonempty=True)
    return [
        block
        for block in blocks
        if stop_reason != "max_tokens" or not isinstance(block, LLMToolUseBlock)
    ]


def validate_payload(value) -> None:
    validate_json_value(
        value,
        max_bytes=MAX_TOOL_PAYLOAD_BYTES,
        max_depth=MAX_JSON_DEPTH + 8,
        max_nodes=MAX_TOOL_PAYLOAD_BYTES,
    )


def usage_values(value, input_key: str, output_key: str) -> tuple[int, int]:
    if not isinstance(value, dict):
        raise ValueError("invalid usage")
    validate_json_value(value)
    counts = (value.get(input_key), value.get(output_key))
    if any(type(count) is not int or count < 0 for count in counts):
        raise ValueError("invalid token counts")
    return counts


def _block_payload(block: LLMContentBlock) -> dict:
    if isinstance(block, LLMTextBlock):
        return {"type": "text", "text": text_value(block.text, nonempty=True)}
    if isinstance(block, LLMReasoningBlock):
        return {
            "type": "reasoning",
            "text": text_value(block.text),
            "signature": text_value(block.signature, nonempty=True),
        }
    if isinstance(block, LLMRedactedReasoningBlock):
        return {"type": "redacted_reasoning", "data": validate_redacted_data(block.data)}
    if isinstance(block, LLMToolUseBlock):
        validate_identifier(block.id)
        validate_identifier(block.name, tool_name=True)
        if not isinstance(block.input, dict):
            raise ValueError("tool input must be an object")
        validate_json_value(block.input)
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, LLMToolResultBlock):
        validate_identifier(block.tool_use_id)
        validate_json_value(block.content)
        if type(block.is_error) is not bool:
            raise ValueError("invalid tool result status")
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
            "is_error": block.is_error,
        }
    raise ValueError("unsupported content block")


def validate_tool_request(request: LLMRequest) -> tuple[set[str], set[str]]:
    if not isinstance(request.tools, list) or len(request.tools) > MAX_TOOLS:
        raise ValueError("invalid tool list")
    tool_names = set()
    tools = []
    for tool in request.tools:
        if not isinstance(tool, LLMTool):
            raise ValueError("invalid tool definition")
        validate_identifier(tool.name, tool_name=True)
        validate_input_schema(tool.input_schema)
        if tool.name in tool_names:
            raise ValueError("duplicate tool definition")
        tool_names.add(tool.name)
        definition = {"name": tool.name, "input_schema": tool.input_schema}
        if tool.description is not None:
            definition["description"] = text_value(tool.description, nonempty=True)
        tools.append(definition)
    if (
        not isinstance(request.messages, list)
        or not 1 <= len(request.messages) <= MAX_TOOL_MESSAGES
    ):
        raise ValueError("invalid message list")
    used_ids = set()
    pending = set()
    messages = []
    for message in request.messages:
        if not isinstance(message, LLMMessage) or message.role not in ("user", "assistant"):
            raise ValueError("invalid message")
        blocks = (
            [LLMTextBlock(text_value(message.content, nonempty=True))]
            if isinstance(message.content, str)
            else content_list(message.content)
        )
        payload = []
        calls = set()
        results = set()
        text_seen = False
        for block in blocks:
            payload.append(_block_payload(block))
            if isinstance(block, LLMToolUseBlock):
                if (
                    message.role != "assistant"
                    or block.name not in tool_names
                    or block.id in used_ids
                ):
                    raise ValueError("invalid tool call history")
                used_ids.add(block.id)
                calls.add(block.id)
            elif isinstance(block, LLMToolResultBlock):
                if (
                    message.role != "user"
                    or block.tool_use_id not in pending
                    or block.tool_use_id in results
                    or text_seen
                ):
                    raise ValueError("invalid tool result history")
                results.add(block.tool_use_id)
            elif isinstance(block, (LLMReasoningBlock, LLMRedactedReasoningBlock)):
                if message.role != "assistant":
                    raise ValueError("reasoning must be in an assistant message")
            else:
                text_seen = True
        if pending != results:
            raise ValueError("tool results must immediately resolve all pending calls")
        pending = calls
        messages.append(
            {
                "role": message.role,
                "content": message.content if isinstance(message.content, str) else payload,
            }
        )
    if pending:
        raise ValueError("unresolved tool calls")
    if request.system is not None:
        text_value(request.system)
    if request.model is not None:
        text_value(request.model, nonempty=True)
    if type(request.thinking) is not bool:
        raise ValueError("invalid thinking option")
    if type(request.max_tokens) is not int or not 1 <= request.max_tokens <= 64000:
        raise ValueError("invalid token limit")
    validate_payload({"tools": tools, "messages": messages, "system": request.system})
    return tool_names, used_ids


def validate_response_blocks(
    blocks: list[LLMContentBlock],
    *,
    tool_names: set[str],
    used_ids: set[str],
    stop_reason: str,
) -> str:
    content_list(blocks, allow_empty=True)
    text_value(stop_reason, nonempty=True)
    calls = set()
    payload = []
    text = []
    for block in blocks:
        payload.append(_block_payload(block))
        if isinstance(block, LLMToolUseBlock):
            if block.name not in tool_names or block.id in used_ids or block.id in calls:
                raise ValueError("invalid tool call response")
            calls.add(block.id)
        elif isinstance(block, LLMToolResultBlock):
            raise ValueError("unexpected tool result response")
        elif isinstance(block, LLMTextBlock):
            text.append(block.text)
    if bool(calls) != (stop_reason == "tool_use"):
        raise ValueError("tool calls contradict the stop reason")
    validate_payload(payload)
    return "".join(text)
