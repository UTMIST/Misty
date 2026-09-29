"""Claude via Amazon Bedrock (Mantle Messages endpoint).

Uses AnthropicBedrockMantle so usage bills as standard Amazon Bedrock (AWS
credits apply). Never AnthropicAWS / Claude Platform on AWS (Marketplace CCU).
"""

import json

import anthropic
from anthropic import AnthropicBedrockMantle

from src.providers.base import (
    LLMContentBlock,
    LLMReasoningBlock,
    LLMRedactedReasoningBlock,
    LLMRequest,
    LLMResult,
    LLMTextBlock,
    LLMToolResultBlock,
    LLMToolUseBlock,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from src.providers.raw_responses import decode_json_object
from src.providers.tool_blocks import (
    content_list,
    object_fields,
    text_value,
    tool_mode,
    usage_values,
    validate_response_blocks,
    validate_tool_request,
)


_STOP_REASONS = {
    "end_turn",
    "max_tokens",
    "stop_sequence",
    "tool_use",
    "pause_turn",
    "refusal",
    "model_context_window_exceeded",
}


def _to_bedrock_model_id(model: str) -> str:
    # The Mantle Messages endpoint uses a bare `anthropic.` prefix
    # (e.g. anthropic.claude-sonnet-5). If cross-region inference requires a
    # `us.`/`global.` prefix on your account, adjust here — pin via smoke test.
    return f"anthropic.{model}"


def _to_mantle_block(block: LLMContentBlock) -> dict:
    if isinstance(block, LLMTextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, LLMReasoningBlock):
        return {"type": "thinking", "thinking": block.text, "signature": block.signature}
    if isinstance(block, LLMRedactedReasoningBlock):
        return {"type": "redacted_thinking", "data": block.data}
    if isinstance(block, LLMToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, LLMToolResultBlock):
        content = (
            block.content
            if isinstance(block.content, str)
            else json.dumps(
                block.content, allow_nan=False, ensure_ascii=False, separators=(",", ":")
            )
        )
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": content,
            "is_error": block.is_error,
        }
    raise ValueError("unsupported content block")


def _from_mantle_block(value) -> LLMContentBlock:
    if not isinstance(value, dict):
        raise ValueError("invalid Mantle content block")
    kind = value.get("type")
    if kind == "text":
        object_fields(value, {"type", "text"}, {"citations"})
        if value.get("citations") not in (None, []):
            raise ValueError("unsupported Mantle citations")
        return LLMTextBlock(value["text"])
    if kind == "thinking":
        object_fields(value, {"type", "signature"}, {"thinking"})
        return LLMReasoningBlock(value.get("thinking", ""), value["signature"])
    if kind == "redacted_thinking":
        object_fields(value, {"type", "data"})
        return LLMRedactedReasoningBlock(value["data"])
    if kind == "tool_use":
        object_fields(value, {"type", "id", "name", "input"}, {"caller"})
        if value.get("caller") not in (None, {"type": "direct"}):
            raise ValueError("unsupported Mantle tool caller")
        return LLMToolUseBlock(value["id"], value["name"], value["input"])
    raise ValueError("unsupported Mantle content block")


class BedrockClaudeProvider:
    def __init__(self, *, aws_region: str, default_model: str, timeout_s: float, client=None):
        self._client = client or AnthropicBedrockMantle(aws_region=aws_region)
        self._default_model = default_model
        self._timeout_s = timeout_s

    def chat(self, request: LLMRequest) -> LLMResult:
        if tool_mode(request):
            return self._chat_with_tools(request)
        model = request.model or self._default_model
        kwargs: dict = {
            "model": _to_bedrock_model_id(model),
            "max_tokens": request.max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        if request.system:
            kwargs["system"] = request.system
        if request.thinking:
            kwargs["thinking"] = {"type": "adaptive"}

        try:
            msg = self._client.with_options(timeout=self._timeout_s).messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            raise ProviderRateLimited(str(exc)) from exc
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as exc:
            raise ProviderTimeout(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            raise ProviderUnavailable(str(exc)) from exc

        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        return LLMResult(
            content=text,
            model=msg.model,
            stop_reason=msg.stop_reason or "",
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
        )

    def _chat_with_tools(self, request: LLMRequest) -> LLMResult:
        try:
            tool_names, used_ids = validate_tool_request(request)
            kwargs = {
                "model": _to_bedrock_model_id(request.model or self._default_model),
                "max_tokens": request.max_tokens,
                "messages": [
                    {
                        "role": message.role,
                        "content": (
                            message.content
                            if isinstance(message.content, str)
                            else [_to_mantle_block(block) for block in message.content]
                        ),
                    }
                    for message in request.messages
                ],
            }
            if request.tools:
                tools = []
                for tool in request.tools:
                    definition = {"name": tool.name, "input_schema": tool.input_schema}
                    if tool.description is not None:
                        definition["description"] = tool.description
                    tools.append(definition)
                kwargs["tools"] = tools
            if request.system:
                kwargs["system"] = request.system
            if request.thinking:
                kwargs["thinking"] = {"type": "adaptive"}
        except ValueError:
            raise ProviderUnavailable("unsupported Bedrock tool request") from None

        try:
            raw = self._client.messages.with_raw_response.create(timeout=self._timeout_s, **kwargs)
        except anthropic.RateLimitError:
            raise ProviderRateLimited("Bedrock rate limited") from None
        except (anthropic.APITimeoutError, anthropic.APIConnectionError):
            raise ProviderTimeout("Bedrock request timed out") from None
        except anthropic.AnthropicError:
            raise ProviderUnavailable("Bedrock request failed") from None

        try:
            response = decode_json_object(raw.content)
            if response.get("role") != "assistant" or response.get("type") != "message":
                raise ValueError("invalid response role or type")
            text_value(response.get("id"), nonempty=True)
            model = text_value(response.get("model"), nonempty=True)
            stop_reason = response.get("stop_reason")
            if not isinstance(stop_reason, str) or stop_reason not in _STOP_REASONS:
                raise ValueError("invalid stop reason")
            input_tokens, output_tokens = usage_values(
                response.get("usage"), "input_tokens", "output_tokens"
            )
            blocks = [_from_mantle_block(block) for block in content_list(response.get("content"))]
            text = validate_response_blocks(
                blocks, tool_names=tool_names, used_ids=used_ids, stop_reason=stop_reason
            )
        except ValueError:
            raise ProviderUnavailable("invalid Bedrock tool response") from None
        return LLMResult(
            content=text,
            model=model,
            stop_reason=stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            content_blocks=blocks,
        )
