"""Provider-agnostic LLM adapter interface — no FastAPI, no vendor SDK.

Neutral request/response types the API layer maps to/from, plus the provider
protocol and a normalized error hierarchy the router maps to HTTP status codes.
"""

from dataclasses import dataclass, field
from typing import Protocol

from contracts.tool_validation import JsonValue


@dataclass
class LLMTool:
    name: str
    input_schema: dict[str, JsonValue]
    description: str | None = None


@dataclass
class LLMTextBlock:
    text: str


@dataclass
class LLMReasoningBlock:
    text: str
    signature: str


@dataclass
class LLMRedactedReasoningBlock:
    data: str


@dataclass
class LLMToolUseBlock:
    id: str
    name: str
    input: dict[str, JsonValue]


@dataclass
class LLMToolResultBlock:
    tool_use_id: str
    content: JsonValue
    is_error: bool = False


LLMContentBlock = (
    LLMTextBlock
    | LLMReasoningBlock
    | LLMRedactedReasoningBlock
    | LLMToolUseBlock
    | LLMToolResultBlock
)


@dataclass
class LLMMessage:
    role: str  # "user" | "assistant"
    content: str | list[LLMContentBlock]


@dataclass
class LLMRequest:
    messages: list[LLMMessage] = field(default_factory=list)
    system: str | None = None
    model: str | None = None
    max_tokens: int = 16000
    thinking: bool = True
    tools: list[LLMTool] = field(default_factory=list)


@dataclass
class LLMResult:
    content: str
    model: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    content_blocks: list[LLMContentBlock] | None = None


class LLMProvider(Protocol):
    def chat(self, request: LLMRequest) -> LLMResult: ...


class ProviderError(Exception):
    """Base for normalized provider failures."""


class ProviderRateLimited(ProviderError):
    """Upstream returned 429."""


class ProviderTimeout(ProviderError):
    """Upstream connection/timeout failure."""


class ProviderUnavailable(ProviderError):
    """Upstream 5xx / auth / other status failure."""
