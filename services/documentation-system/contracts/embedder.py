from dataclasses import dataclass
from typing import Protocol

# Configured /embed aliases and the provider model id each resolves to. Stored
# chunks record the resolved id, so indexing, search, and the stale listing
# all translate an alias through this one table (#209).
RESOLVED_EMBED_MODELS = {
    "openai-embed-3-small": "text-embedding-3-small",
    "openai-embed-3-large": "text-embedding-3-large",
}


class EmbedderUnavailable(Exception):
    """The llm service could not embed right now (connection, timeout, 429,
    5xx). Retrying later may succeed; the indexer reports a retryable failure."""


class EmbedderRejected(Exception):
    """Input failed local validation, or the llm service refused the request
    (4xx, a malformed response, or a vector width the doc_chunks column cannot
    hold). Retrying unchanged input will not help."""


@dataclass(frozen=True)
class EmbedBatch:
    # One vector per input, in input order.
    vectors: tuple[tuple[float, ...], ...]
    # The resolved provider-side model id from /embed, not the configured alias,
    # so stored chunks record what actually produced them.
    model: str
    dimensions: int


class Embedder(Protocol):
    # The resolved model id every batch from this embedder carries, known
    # before any provider call so indexing can tell which stored set is its own.
    model: str

    def embed(self, inputs: list[str]) -> EmbedBatch:
        """Embed every input, splitting into as many /embed requests as the
        llm service's limits require. All-or-nothing: raises EmbedderUnavailable
        or EmbedderRejected rather than returning a partial result. Validate
        all inputs before the first provider request."""
        ...
