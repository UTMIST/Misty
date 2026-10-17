from dataclasses import dataclass
from typing import Protocol


class EmbedderUnavailable(Exception):
    """The llm service could not embed right now (connection, timeout, 429,
    5xx). Retrying later may succeed; the indexer reports a retryable failure."""


class EmbedderRejected(Exception):
    """The llm service refused the request (4xx, a malformed response, or a
    vector width the doc_chunks column cannot hold). Retrying unchanged input
    will not help."""


@dataclass(frozen=True)
class EmbedBatch:
    # One vector per input, in input order.
    vectors: tuple[tuple[float, ...], ...]
    # The resolved provider-side model id from /embed, not the configured alias,
    # so stored chunks record what actually produced them.
    model: str
    dimensions: int


class Embedder(Protocol):
    def embed(self, inputs: list[str]) -> EmbedBatch:
        """Embed every input, splitting into as many /embed requests as the
        llm service's limits require. All-or-nothing: raises EmbedderUnavailable
        or EmbedderRejected rather than returning a partial result."""
        ...
