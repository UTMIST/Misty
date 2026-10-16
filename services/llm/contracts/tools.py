from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictStr,
)

from contracts.tool_validation import (
    MAX_TOOL_PAYLOAD_BYTES,
    validate_identifier,
    validate_input_schema,
    validate_json_value,
    validate_redacted_data,
)


def _validate_text(value: str) -> str:
    validate_json_value(value, max_bytes=MAX_TOOL_PAYLOAD_BYTES)
    return value


def _validate_tool_name(value: str) -> str:
    return validate_identifier(value, tool_name=True)


UTF8String = Annotated[StrictStr, AfterValidator(_validate_text)]
ToolName = Annotated[StrictStr, AfterValidator(_validate_tool_name)]
ToolID = Annotated[StrictStr, AfterValidator(validate_identifier)]
JSONObject = Annotated[dict[str, JsonValue], BeforeValidator(validate_json_value)]


class _ToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ToolDefinition(_ToolModel):
    name: ToolName
    input_schema: Annotated[dict[str, JsonValue], BeforeValidator(validate_input_schema)]
    description: UTF8String | None = Field(default=None, min_length=1, max_length=4096)


class TextBlock(_ToolModel):
    type: Literal["text"] = "text"
    text: UTF8String = Field(min_length=1)


class ReasoningBlock(_ToolModel):
    type: Literal["reasoning"] = "reasoning"
    text: UTF8String
    signature: UTF8String = Field(min_length=1)


class RedactedReasoningBlock(_ToolModel):
    type: Literal["redacted_reasoning"] = "redacted_reasoning"
    data: Annotated[StrictStr, AfterValidator(validate_redacted_data)]


class ToolUseBlock(_ToolModel):
    type: Literal["tool_use"] = "tool_use"
    id: ToolID
    name: ToolName
    input: JSONObject


class ToolResultBlock(_ToolModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: ToolID
    content: Annotated[JsonValue, BeforeValidator(validate_json_value)]
    is_error: StrictBool = False


ContentBlock = Annotated[
    TextBlock | ReasoningBlock | RedactedReasoningBlock | ToolUseBlock | ToolResultBlock,
    Field(discriminator="type"),
]
AssistantBlock = Annotated[
    TextBlock | ReasoningBlock | RedactedReasoningBlock | ToolUseBlock,
    Field(discriminator="type"),
]
