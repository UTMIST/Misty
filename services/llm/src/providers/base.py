"""Provider-agnostic LLM adapter interface — no FastAPI, no vendor SDK.

Neutral request/response types the API layer maps to/from, plus the provider
protocol and a normalized error hierarchy the router maps to HTTP status codes.
"""

import math
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class LLMMessage:
    role: str  # "user" | "assistant"
    content: str


@dataclass
class LLMRequest:
    messages: list[LLMMessage] = field(default_factory=list)
    system: str | None = None
    model: str | None = None
    max_tokens: int = 16000
    thinking: bool = True


@dataclass
class LLMResult:
    content: str
    model: str
    stop_reason: str
    input_tokens: int
    output_tokens: int


class LLMProvider(Protocol):
    def chat(self, request: LLMRequest) -> LLMResult: ...


@dataclass
class EmbeddingRequest:
    inputs: list[str] = field(default_factory=list)
    model: str | None = None


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]  # one per input, in input order
    model: str
    dimensions: int
    input_tokens: int


def validate_vector(vector, index: int, dimensions: int) -> None:
    """Reject a malformed vector, raising a typed failure.

    Width alone is not enough. NaN and +/-Infinity pass a length check, serialize
    as invalid JSON under a `[float]` schema, and are meaningless as pgvector
    components.
    """
    if not isinstance(vector, list):
        raise ProviderUnavailable(f"embedding {index} is not a vector")
    if len(vector) != dimensions:
        raise ProviderUnavailable(
            f"embedding {index} has {len(vector)} dimensions, expected {dimensions}"
        )
    for value in vector:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ProviderUnavailable(f"embedding {index} holds a non-numeric element")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ProviderUnavailable(f"embedding {index} holds an element that is not finite")


class EmbeddingProvider(Protocol):
    """A second Protocol rather than an `embed` method on LLMProvider: both chat
    providers are Claude models with no notion of embeddings, and LLM_MODEL
    cannot name a chat model and an embedding model at once. Same layering and
    ProviderError hierarchy — a separate capability, not a second pattern."""

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult: ...


class ProviderError(Exception):
    """Base for normalized provider failures. Shared by both Protocols above, so
    one upstream status maps to one HTTP status in exactly one place."""


class ProviderRateLimited(ProviderError):
    """Upstream returned 429."""


class ProviderTimeout(ProviderError):
    """Upstream connection/timeout failure."""


class ProviderUnavailable(ProviderError):
    """Upstream 5xx / auth / other status failure."""


class ProviderNotConfigured(ProviderError):
    """The selected provider has no credential on this deployment.

    503, not 502: nothing upstream failed, this instance simply cannot serve the
    capability. Mirrors connectors' SourceNotConfigured, and keeps the local-dev
    contract the platform uses everywhere — the service boots and /health stays
    green with no credential; only the calls that need one fail."""


class ProviderUnsupportedModel(ProviderError):
    """The caller named a model this provider cannot serve.

    A client mistake, not a provider fault, so the router maps it to 422 rather
    than 502 — mirroring connectors' SourceUnsupported."""
