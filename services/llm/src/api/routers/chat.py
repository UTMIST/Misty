from fastapi import APIRouter, Depends, HTTPException, Request

from contracts.chat import ChatRequest, ChatResponse, Usage
from contracts.tool_validation import (
    MAX_JSON_DEPTH,
    MAX_TOOL_PAYLOAD_BYTES,
    validate_json_value,
)
from contracts.tools import (
    AssistantBlock,
    ContentBlock,
    ReasoningBlock,
    RedactedReasoningBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from src.api.auth import require_scope
from src.api.deps import get_llm
from src.config import get_settings
from src.providers.base import (
    LLMContentBlock,
    LLMMessage,
    LLMProvider,
    LLMReasoningBlock,
    LLMRedactedReasoningBlock,
    LLMRequest,
    LLMResult,
    LLMTextBlock,
    LLMTool,
    LLMToolResultBlock,
    LLMToolUseBlock,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
)
from src.providers.tool_blocks import response_blocks

router = APIRouter()


def _to_llm_block(block: ContentBlock) -> LLMContentBlock:
    if isinstance(block, TextBlock):
        return LLMTextBlock(text=block.text)
    if isinstance(block, ReasoningBlock):
        return LLMReasoningBlock(text=block.text, signature=block.signature)
    if isinstance(block, RedactedReasoningBlock):
        return LLMRedactedReasoningBlock(data=block.data)
    if isinstance(block, ToolUseBlock):
        return LLMToolUseBlock(id=block.id, name=block.name, input=block.input)
    if isinstance(block, ToolResultBlock):
        return LLMToolResultBlock(
            tool_use_id=block.tool_use_id, content=block.content, is_error=block.is_error
        )
    raise ValueError("unsupported content block")


def _to_assistant_block(block: LLMContentBlock) -> AssistantBlock:
    if isinstance(block, LLMTextBlock):
        return TextBlock(text=block.text)
    if isinstance(block, LLMReasoningBlock):
        return ReasoningBlock(text=block.text, signature=block.signature)
    if isinstance(block, LLMRedactedReasoningBlock):
        return RedactedReasoningBlock(data=block.data)
    if isinstance(block, LLMToolUseBlock):
        return ToolUseBlock(id=block.id, name=block.name, input=block.input)
    raise ValueError("unsupported assistant content block")


def _to_response(result: LLMResult, body: ChatRequest) -> ChatResponse:
    blocks = None
    if body.tool_mode:
        blocks = [
            _to_assistant_block(block)
            for block in response_blocks(result.content_blocks, result.stop_reason)
        ]
        if result.content != "".join(
            block.text for block in blocks if isinstance(block, TextBlock)
        ):
            raise ValueError("assistant text does not match content blocks")
    response = ChatResponse(
        content=result.content,
        model=result.model,
        stop_reason=result.stop_reason,
        usage=Usage(input_tokens=result.input_tokens, output_tokens=result.output_tokens),
        content_blocks=blocks,
    )
    if body.tool_mode:
        if any(
            type(count) is not int or count < 0
            for count in (result.input_tokens, result.output_tokens)
        ):
            raise ValueError("invalid provider usage")
        calls = [block for block in blocks or [] if isinstance(block, ToolUseBlock)]
        if (result.stop_reason == "tool_use") != bool(calls):
            raise ValueError("tool calls do not match the stop reason")
        names = {tool.name for tool in body.tools or []}
        seen = {
            block.id
            for message in body.messages
            if isinstance(message.content, list)
            for block in message.content
            if isinstance(block, ToolUseBlock)
        }
        for call in calls:
            if call.name not in names or call.id in seen:
                raise ValueError("invalid provider tool call")
            seen.add(call.id)
        validate_json_value(
            response.model_dump(exclude_none=True),
            max_bytes=MAX_TOOL_PAYLOAD_BYTES,
            max_depth=MAX_JSON_DEPTH + 8,
            max_nodes=MAX_TOOL_PAYLOAD_BYTES,
        )
    return response


@router.post("/chat", response_model=ChatResponse, response_model_exclude_none=True)
def chat(
    body: ChatRequest,
    request: Request,
    _key=Depends(require_scope("chat")),
    llm: LLMProvider = Depends(get_llm),
) -> ChatResponse:
    settings = get_settings()
    thinking = body.thinking if body.thinking is not None else settings.thinking_default
    llm_request = LLMRequest(
        messages=[
            LLMMessage(
                role=message.role,
                content=message.content
                if isinstance(message.content, str)
                else [_to_llm_block(block) for block in message.content],
            )
            for message in body.messages
        ],
        system=body.system,
        model=body.model,
        max_tokens=body.max_tokens,
        thinking=thinking,
        tools=[
            LLMTool(name=tool.name, input_schema=tool.input_schema, description=tool.description)
            for tool in body.tools or []
        ],
    )
    request.state.audit_extra = {"model": body.model or settings.llm_model}
    try:
        result = llm.chat(llm_request)
    except ProviderRateLimited:
        raise HTTPException(status_code=429, detail="LLM provider rate limited")
    except ProviderTimeout:
        raise HTTPException(status_code=504, detail="LLM provider timeout")
    except ProviderError:
        raise HTTPException(status_code=502, detail="LLM provider error")
    try:
        response = _to_response(result, body)
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(status_code=502, detail="LLM provider error") from None
    request.state.audit_extra.update(
        {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
    )

    return response
