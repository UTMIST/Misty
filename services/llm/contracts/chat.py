from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from contracts.tool_validation import (
    MAX_CONTENT_BLOCKS,
    MAX_JSON_DEPTH,
    MAX_TOOL_MESSAGES,
    MAX_TOOL_PAYLOAD_BYTES,
    MAX_TOOLS,
    validate_json_value,
)
from contracts.tools import (
    AssistantBlock,
    ContentBlock,
    TextBlock,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
)

ALLOWED_MODELS = {"claude-sonnet-4-6", "claude-opus-4-6"}


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: (
        Annotated[str, Field(min_length=1)]
        | Annotated[
            list[ContentBlock], Field(min_length=1, max_length=MAX_CONTENT_BLOCKS, strict=True)
        ]
    )

    @model_validator(mode="after")
    def _validate_blocks(self) -> Self:
        if isinstance(self.content, str):
            return self
        for block in self.content:
            if self.role == "user" and not isinstance(block, (TextBlock, ToolResultBlock)):
                raise ValueError("user messages support only text and tool results")
            if self.role == "assistant" and isinstance(block, ToolResultBlock):
                raise ValueError("tool results require a user message")
        return self


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1)
    system: str | None = None
    model: str | None = None
    max_tokens: int = Field(default=16000, ge=1, le=64000)
    thinking: bool | None = None
    tools: Annotated[list[ToolDefinition], Field(max_length=MAX_TOOLS, strict=True)] | None = None

    @property
    def tool_mode(self) -> bool:
        return bool(self.tools) or any(
            isinstance(message.content, list) for message in self.messages
        )

    @model_validator(mode="before")
    @classmethod
    def _validate_tool_payload(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        messages = data.get("messages")
        tools = data.get("tools")
        structured = isinstance(messages, list) and any(
            isinstance(message.content, list)
            if isinstance(message, Message)
            else isinstance(message, dict) and isinstance(message.get("content"), list)
            for message in messages
        )
        if not tools and not structured:
            return data
        if isinstance(messages, list) and len(messages) > MAX_TOOL_MESSAGES:
            raise ValueError("tool mode supports at most 256 messages")
        payload = dict(data)
        if isinstance(messages, list):
            payload["messages"] = [
                message.model_dump() if isinstance(message, Message) else message
                for message in messages
            ]
        if isinstance(tools, list):
            payload["tools"] = [
                tool.model_dump() if isinstance(tool, ToolDefinition) else tool for tool in tools
            ]
        validate_json_value(
            payload,
            max_bytes=MAX_TOOL_PAYLOAD_BYTES,
            max_depth=MAX_JSON_DEPTH + 8,
            max_nodes=MAX_TOOL_PAYLOAD_BYTES,
        )
        return data

    @model_validator(mode="after")
    def _validate_tool_history(self) -> Self:
        names = {tool.name for tool in self.tools or []}
        if len(names) != len(self.tools or []):
            raise ValueError("tool names must be unique")
        pending: set[str] = set()
        seen: set[str] = set()
        for message in self.messages:
            if pending:
                if message.role != "user" or not isinstance(message.content, list):
                    raise ValueError(
                        "tool calls require results in the immediately following message"
                    )
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        if block.tool_use_id not in pending:
                            raise ValueError(
                                "tool results must match outstanding calls exactly once"
                            )
                        pending.remove(block.tool_use_id)
                    elif pending:
                        raise ValueError("all tool results must precede ordinary user content")
                if pending:
                    raise ValueError("the following message must answer every tool call")
                continue
            if isinstance(message.content, str):
                continue
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    raise ValueError("tool results require an immediately preceding tool call")
                if isinstance(block, ToolUseBlock):
                    if block.name not in names:
                        raise ValueError("tool calls require a declared tool")
                    if block.id in seen:
                        raise ValueError("tool-use identifiers must be unique")
                    seen.add(block.id)
                    pending.add(block.id)
        if pending:
            raise ValueError("every tool call must have a result")
        if self.tool_mode:
            validate_json_value(
                {
                    "tools": [tool.model_dump(exclude_none=True) for tool in self.tools or []],
                    "messages": [message.model_dump() for message in self.messages],
                    "system": self.system,
                },
                max_bytes=MAX_TOOL_PAYLOAD_BYTES,
                max_depth=MAX_JSON_DEPTH + 8,
                max_nodes=MAX_TOOL_PAYLOAD_BYTES,
            )
        return self

    @field_validator("model")
    @classmethod
    def _validate_model(cls, v: str | None) -> str | None:
        if v is not None and v not in ALLOWED_MODELS:
            raise ValueError(f"model must be one of {sorted(ALLOWED_MODELS)}")
        return v

    @field_validator("system")
    @classmethod
    def _validate_system(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                v.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("system must be valid UTF-8") from None
        return v


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int


class ChatResponse(BaseModel):
    content: str
    model: str
    stop_reason: str
    usage: Usage
    content_blocks: list[AssistantBlock] | None = Field(default=None, max_length=MAX_CONTENT_BLOCKS)
