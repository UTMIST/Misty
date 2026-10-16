"""OpenAI Responses behind the existing stateless chat Protocol."""

import json
import threading

from contracts.models import OPENAI_MODELS
from contracts.tool_validation import validate_reasoning_state
from src.providers.base import (
    LLMReasoningStateBlock,
    LLMRequest,
    LLMResult,
    LLMTextBlock,
    LLMToolResultBlock,
    LLMToolUseBlock,
    ProviderError,
    ProviderInvalidRequest,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    ProviderUnsupportedModel,
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


def _json(value) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"))


def _input(request: LLMRequest) -> list[dict]:
    items = []
    for message in request.messages:
        if isinstance(message.content, str):
            items.append({"role": message.role, "content": message.content})
            continue
        for block in message.content:
            if isinstance(block, LLMTextBlock):
                item = {"role": message.role, "content": block.text}
                if block.phase is not None:
                    if message.role != "assistant":
                        raise ValueError("phase requires an assistant message")
                    item["phase"] = block.phase
                items.append(item)
            elif isinstance(block, LLMReasoningStateBlock):
                items.append(validate_reasoning_state(block.data))
            elif isinstance(block, LLMToolUseBlock):
                items.append(
                    {
                        "type": "function_call",
                        "call_id": block.id,
                        "name": block.name,
                        "arguments": _json(block.input),
                    }
                )
            elif isinstance(block, LLMToolResultBlock):
                # Responses has no is_error field. Preserve error status in
                # the output instead of presenting failed execution as success.
                output = (
                    _json({"content": block.content, "is_error": True})
                    if block.is_error
                    else block.content
                    if isinstance(block.content, str)
                    else _json(block.content)
                )
                items.append(
                    {"type": "function_call_output", "call_id": block.tool_use_id, "output": output}
                )
            else:
                # Bedrock continuation signatures cannot be replayed to OpenAI.
                raise ValueError("unsupported continuation block")
    return items


def _result(payload: dict, *, structured: bool, tool_names: set[str], used_ids: set[str]):
    # Validate the raw envelope and output items before SDK coercion. These are
    # the documented response metadata fields; unknown fields fail closed.
    object_fields(
        payload,
        {"id", "object", "created_at", "model", "status", "output", "usage"},
        {
            "error",
            "incomplete_details",
            "instructions",
            "max_output_tokens",
            "metadata",
            "parallel_tool_calls",
            "temperature",
            "tool_choice",
            "tools",
            "top_p",
            "background",
            "completed_at",
            "conversation",
            "max_tool_calls",
            "previous_response_id",
            "prompt",
            "prompt_cache_key",
            "prompt_cache_retention",
            "reasoning",
            "safety_identifier",
            "service_tier",
            "store",
            "text",
            "top_logprobs",
            "truncation",
            "user",
            "context_management",
            "prompt_cache_options",
            "access_programs",
            "moderation",
            "retention",
        },
    )
    if payload["object"] != "response" or payload.get("error") is not None:
        raise ValueError("invalid response")
    model = text_value(payload["model"], nonempty=True)
    if model not in OPENAI_MODELS:
        raise ValueError("unexpected response model")
    status = payload["status"]
    if status == "completed" and payload.get("incomplete_details") is None:
        stop_reason = "end_turn"
    elif status == "incomplete":
        details = object_fields(payload.get("incomplete_details"), {"reason"})
        stop_reason = {"max_output_tokens": "max_tokens", "content_filter": "content_filtered"}[
            details["reason"]
        ]
    else:
        raise ValueError("unexpected response status")
    blocks = []
    refused = False
    for item in content_list(payload["output"], allow_empty=True):
        if not isinstance(item, dict):
            raise ValueError("invalid response item")
        kind = item.get("type")
        if kind == "reasoning":
            # Even text-only requests validate the state, but only structured
            # responses expose it for consumer-managed replay.
            blocks.append(LLMReasoningStateBlock(validate_reasoning_state(item)))
        elif kind == "message":
            object_fields(item, {"id", "type", "role", "status", "content"}, {"phase"})
            if item["role"] != "assistant" or item["status"] not in ("completed", "incomplete"):
                raise ValueError("invalid response message")
            if status == "completed" and item["status"] != "completed":
                raise ValueError("incomplete message in completed response")
            phase = item.get("phase")
            if phase not in (None, "commentary", "final_answer"):
                raise ValueError("invalid message phase")
            for part in content_list(item["content"], allow_empty=True):
                if not isinstance(part, dict):
                    raise ValueError("invalid message content")
                if part.get("type") == "output_text":
                    object_fields(part, {"type", "text", "annotations"}, {"logprobs"})
                    if part["annotations"] != []:
                        raise ValueError("unsupported message annotations")
                    text = text_value(part["text"])
                elif part.get("type") == "refusal":
                    object_fields(part, {"type", "refusal"})
                    text = text_value(part["refusal"])
                    refused = True
                else:
                    raise ValueError("unsupported message content")
                if text:
                    blocks.append(LLMTextBlock(text, phase=phase))
        elif kind == "function_call":
            object_fields(item, {"type", "call_id", "name", "arguments"}, {"id", "status"})
            # Withhold the entire truncated parallel batch, without attempting
            # to parse partial arguments as executable JSON.
            if stop_reason == "max_tokens":
                continue
            if status != "completed" or item.get("status") not in (None, "completed"):
                raise ValueError("incomplete executable call")
            arguments = text_value(item["arguments"])
            blocks.append(
                LLMToolUseBlock(
                    id=item["call_id"],
                    name=item["name"],
                    input=decode_json_object(arguments.encode("utf-8")),
                )
            )
        else:
            raise ValueError("unsupported response item")
    has_calls = any(isinstance(block, LLMToolUseBlock) for block in blocks)
    if has_calls:
        if refused:
            raise ValueError("tool call contradicts refusal")
        stop_reason = "tool_use"
    elif refused and stop_reason == "end_turn":
        stop_reason = "refusal"
    text = validate_response_blocks(
        blocks, tool_names=tool_names, used_ids=used_ids, stop_reason=stop_reason
    )
    input_tokens, output_tokens = usage_values(payload["usage"], "input_tokens", "output_tokens")
    return LLMResult(
        content=text,
        model=model,
        stop_reason=stop_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        content_blocks=blocks if structured else None,
    )


class OpenAIChatProvider:
    def __init__(self, *, api_key: str, default_model: str, timeout_s: float, client=None):
        if default_model not in OPENAI_MODELS:
            raise ValueError("unsupported default OpenAI chat model")
        self._api_key = api_key
        self._default_model = default_model
        self._timeout_s = timeout_s
        self._client = client
        self._client_lock = threading.Lock()

    def _get_client(self):
        if self._client is None:
            if not self._api_key:
                raise ProviderNotConfigured("OPENAI_API_KEY is not set")
            with self._client_lock:
                if self._client is None:
                    from openai import OpenAI

                    self._client = OpenAI(
                        api_key=self._api_key, timeout=self._timeout_s, max_retries=0
                    )
        return self._client

    def chat(self, request: LLMRequest) -> LLMResult:
        model = request.model or self._default_model
        if model not in OPENAI_MODELS:
            raise ProviderUnsupportedModel("unsupported OpenAI chat model")
        structured = tool_mode(request)
        try:
            if type(request.max_tokens) is not int or not 16 <= request.max_tokens <= 64000:
                raise ValueError("Responses requires at least 16 output tokens")
            tool_names, used_ids = validate_tool_request(request) if structured else (set(), set())
            kwargs = {
                "model": model,
                "input": _input(request),
                "max_output_tokens": request.max_tokens,
                # GPT-6.1 Sol always reasons. Low is its smallest supported effort.
                "reasoning": {"effort": "medium" if request.thinking else "low"},
                "store": False,
                "include": ["reasoning.encrypted_content"],
            }
            if request.system is not None:
                kwargs["instructions"] = request.system
            if request.tools:
                tools = []
                for tool in request.tools:
                    function = {
                        "type": "function",
                        "name": tool.name,
                        "parameters": tool.input_schema,
                        "strict": False,
                    }
                    if tool.description is not None:
                        function["description"] = tool.description
                    tools.append(function)
                kwargs["tools"] = tools
        except (TypeError, ValueError):
            raise ProviderInvalidRequest("unsupported OpenAI chat request") from None

        try:
            from openai import APIConnectionError, APITimeoutError, RateLimitError
        except ImportError:
            raise ProviderUnavailable("the openai package is not installed") from None

        try:
            response = self._get_client().responses.with_raw_response.create(**kwargs)
            payload = decode_json_object(response.http_response.content)
            return _result(payload, structured=structured, tool_names=tool_names, used_ids=used_ids)
        except RateLimitError:
            raise ProviderRateLimited("OpenAI chat rate limited") from None
        except (APITimeoutError, APIConnectionError):
            raise ProviderTimeout("OpenAI chat timeout") from None
        except ProviderError:
            raise
        except Exception:
            raise ProviderUnavailable("OpenAI chat request failed") from None
