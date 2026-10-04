"""Claude via Amazon Bedrock using the Converse API (bedrock-runtime).

Used because this account's Bedrock model access is US-regional (cross-region
inference profiles), which the Messages/Mantle endpoint cannot target. Still
standard Amazon Bedrock billing (credits apply) — NOT AnthropicAWS / Claude
Platform on AWS. Credentials come from the standard AWS chain (incl.
AWS_BEARER_TOKEN_BEDROCK).
"""

import base64
from contextvars import ContextVar

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from contracts.tool_validation import MAX_TOOL_PAYLOAD_BYTES
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
from src.providers.raw_responses import validate_converse_response
from src.providers.tool_blocks import (
    content_list,
    object_fields,
    response_blocks,
    tool_mode,
    usage_values,
    validate_response_blocks,
    validate_tool_request,
)

# Neutral model name -> the exact Bedrock inference-profile id this account can
# invoke. Per-model: prefixes/suffixes differ, so this is an explicit table (not
# a formula). Pinned by live probes against the account.
_MODEL_IDS = {
    "claude-sonnet-4-6": "us.anthropic.claude-sonnet-4-6",
    "claude-opus-4-6": "us.anthropic.claude-opus-4-6-v1",
}

_RATE_LIMIT_CODES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceQuotaExceededException",
}

# Genuine timeout/connection failures (map to 504). Other BotoCoreError subtypes
# — NoCredentialsError, ParamValidationError, etc. — are config faults, not timeouts.
_TIMEOUT_ERRORS = (ReadTimeoutError, ConnectTimeoutError, EndpointConnectionError)


_STOP_REASONS = {
    "end_turn",
    "tool_use",
    "max_tokens",
    "stop_sequence",
    "guardrail_intervened",
    "content_filtered",
    "malformed_model_output",
    "malformed_tool_use",
    "model_context_window_exceeded",
}

_ACTIVE_TOOL_PROVIDER: ContextVar[object | None] = ContextVar(
    "bedrock_converse_tool_provider", default=None
)


def _to_converse_block(block: LLMContentBlock) -> dict:
    if isinstance(block, LLMTextBlock):
        return {"text": block.text}
    if isinstance(block, LLMReasoningBlock):
        return {
            "reasoningContent": {
                "reasoningText": {"text": block.text, "signature": block.signature}
            }
        }
    if isinstance(block, LLMRedactedReasoningBlock):
        return {
            "reasoningContent": {"redactedContent": base64.b64decode(block.data, validate=True)}
        }
    if isinstance(block, LLMToolUseBlock):
        return {"toolUse": {"toolUseId": block.id, "name": block.name, "input": block.input}}
    if isinstance(block, LLMToolResultBlock):
        content = (
            {"text": block.content}
            if isinstance(block.content, str) and block.content
            else {"json": block.content}
        )
        return {
            "toolResult": {
                "toolUseId": block.tool_use_id,
                "content": [content],
                "status": "error" if block.is_error else "success",
            }
        }
    raise ValueError("unsupported content block")


def _from_converse_block(value) -> LLMContentBlock:
    if not isinstance(value, dict) or len(value) != 1:
        raise ValueError("invalid Converse content block")
    if "text" in value:
        return LLMTextBlock(value["text"])
    if "toolUse" in value:
        call = object_fields(value["toolUse"], {"toolUseId", "name", "input"})
        return LLMToolUseBlock(call["toolUseId"], call["name"], call["input"])
    if "reasoningContent" in value:
        reasoning = value["reasoningContent"]
        if not isinstance(reasoning, dict) or len(reasoning) != 1:
            raise ValueError("invalid Converse reasoning block")
        if "reasoningText" in reasoning:
            text = object_fields(reasoning["reasoningText"], {"signature"}, {"text"})
            return LLMReasoningBlock(text.get("text", ""), text["signature"])
        if "redactedContent" in reasoning:
            data = reasoning["redactedContent"]
            if not isinstance(data, bytes) or not 0 < len(data) <= MAX_TOOL_PAYLOAD_BYTES * 3 // 4:
                raise ValueError("invalid Converse redacted reasoning")
            return LLMRedactedReasoningBlock(base64.b64encode(data).decode("ascii"))
    raise ValueError("unsupported Converse content block")


class BedrockConverseProvider:
    def __init__(self, *, aws_region: str, default_model: str, timeout_s: float, client=None):
        if default_model not in _MODEL_IDS:
            raise ValueError(
                f"unsupported default model {default_model!r}; expected one of {sorted(_MODEL_IDS)}"
            )
        self._client = client or boto3.client(
            "bedrock-runtime",
            region_name=aws_region,
            config=Config(read_timeout=timeout_s, connect_timeout=min(timeout_s, 10.0)),
        )
        self._default_model = default_model
        self._timeout_s = timeout_s
        if isinstance(self._client, BaseClient):
            self._client.meta.events.register(
                "before-parse.bedrock-runtime.Converse",
                self._before_parse,
                unique_id=f"llm-tool-response-{id(self)}",
            )

    def _before_parse(self, operation_model, response_dict, **kwargs) -> None:
        if _ACTIVE_TOOL_PROVIDER.get() is self:
            try:
                validate_converse_response(response_dict, operation_model)
            except ValueError:
                raise ProviderUnavailable("invalid Bedrock tool response") from None

    def _model_id(self, model: str) -> str:
        try:
            return _MODEL_IDS[model]
        except KeyError:
            raise ProviderUnavailable(f"unsupported model: {model!r}")

    def chat(self, request: LLMRequest) -> LLMResult:
        if tool_mode(request):
            return self._chat_with_tools(request)
        model = request.model or self._default_model
        bedrock_id = self._model_id(model)
        kwargs: dict = {
            "modelId": bedrock_id,
            "messages": [
                {"role": m.role, "content": [{"text": m.content}]} for m in request.messages
            ],
            "inferenceConfig": {"maxTokens": request.max_tokens},
        }
        if request.system:
            kwargs["system"] = [{"text": request.system}]
        if request.thinking:
            # Adaptive thinking returns an extra `reasoningContent` block, which
            # we ignore when extracting the answer text below.
            kwargs["additionalModelRequestFields"] = {"thinking": {"type": "adaptive"}}

        token = _ACTIVE_TOOL_PROVIDER.set(None)
        try:
            response = self._client.converse(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _RATE_LIMIT_CODES:
                raise ProviderRateLimited(str(exc)) from exc
            raise ProviderUnavailable(str(exc)) from exc
        except _TIMEOUT_ERRORS as exc:
            raise ProviderTimeout(str(exc)) from exc
        except BotoCoreError as exc:  # credential/param/other boto-side faults
            raise ProviderUnavailable(str(exc)) from exc
        finally:
            _ACTIVE_TOOL_PROVIDER.reset(token)

        try:
            blocks = response["output"]["message"]["content"]
        except (KeyError, TypeError) as exc:
            # Content-filtered / guardrail / unexpected-shape responses.
            raise ProviderUnavailable(f"unexpected Bedrock response shape: {exc}") from exc
        text = "".join(b["text"] for b in blocks if isinstance(b, dict) and "text" in b)
        usage = response.get("usage", {})
        return LLMResult(
            content=text,
            model=bedrock_id,
            stop_reason=response.get("stopReason", ""),
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
        )

    def _chat_with_tools(self, request: LLMRequest) -> LLMResult:
        try:
            tool_names, used_ids = validate_tool_request(request)
            model = request.model or self._default_model
            if model not in _MODEL_IDS:
                raise ValueError("unsupported model")
            bedrock_id = self._model_id(model)
            kwargs = {
                "modelId": bedrock_id,
                "messages": [
                    {
                        "role": message.role,
                        "content": (
                            [{"text": message.content}]
                            if isinstance(message.content, str)
                            else [_to_converse_block(block) for block in message.content]
                        ),
                    }
                    for message in request.messages
                ],
                "inferenceConfig": {"maxTokens": request.max_tokens},
            }
            if request.tools:
                tools = []
                for tool in request.tools:
                    spec = {"name": tool.name, "inputSchema": {"json": tool.input_schema}}
                    if tool.description is not None:
                        spec["description"] = tool.description
                    tools.append({"toolSpec": spec})
                kwargs["toolConfig"] = {"tools": tools}
            if request.system:
                kwargs["system"] = [{"text": request.system}]
            if request.thinking:
                kwargs["additionalModelRequestFields"] = {"thinking": {"type": "adaptive"}}
        except ValueError:
            raise ProviderUnavailable("unsupported Bedrock tool request") from None

        token = _ACTIVE_TOOL_PROVIDER.set(self)
        try:
            response = self._client.converse(**kwargs)
        except ClientError as exc:
            error = exc.response.get("Error")
            code = error.get("Code") if isinstance(error, dict) else None
            if isinstance(code, str) and code in _RATE_LIMIT_CODES:
                raise ProviderRateLimited("Bedrock rate limited") from None
            raise ProviderUnavailable("Bedrock request failed") from None
        except _TIMEOUT_ERRORS:
            raise ProviderTimeout("Bedrock request timed out") from None
        except BotoCoreError:
            raise ProviderUnavailable("Bedrock request failed") from None
        finally:
            _ACTIVE_TOOL_PROVIDER.reset(token)

        try:
            if not isinstance(response, dict):
                raise ValueError("invalid Converse response")
            output = object_fields(response.get("output"), {"message"})
            message = object_fields(output["message"], {"role", "content"})
            if message["role"] != "assistant":
                raise ValueError("invalid response role")
            stop_reason = response.get("stopReason")
            if not isinstance(stop_reason, str) or stop_reason not in _STOP_REASONS:
                raise ValueError("invalid stop reason")
            input_tokens, output_tokens = usage_values(
                response.get("usage"), "inputTokens", "outputTokens"
            )
            blocks = response_blocks(
                [
                    _from_converse_block(block)
                    for block in content_list(message["content"], allow_empty=True)
                ],
                stop_reason,
            )
            text = validate_response_blocks(
                blocks, tool_names=tool_names, used_ids=used_ids, stop_reason=stop_reason
            )
        except ValueError:
            raise ProviderUnavailable("invalid Bedrock tool response") from None
        return LLMResult(
            content=text,
            model=bedrock_id,
            stop_reason=stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            content_blocks=blocks,
        )
