"""A deterministic, offline stand-in for the llm service's /embed."""

import hashlib

from contracts.embedder import EmbedBatch, EmbedderUnavailable

FAKE_MODEL = "text-embedding-3-small"


def fake_vector(text: str) -> tuple[float, ...]:
    # Derived from the text so equal inputs embed equally and tests can tell
    # which input produced a stored vector.
    digest = hashlib.sha256(text.encode()).digest()
    head = tuple((b - 128) / 128 for b in digest[:8])
    return head + (0.0,) * (1536 - len(head))


class FakeEmbedder:
    def __init__(self, *, fail_on_call: int | None = None, model: str = FAKE_MODEL) -> None:
        # Every embed() call's inputs, so tests can assert "no provider call".
        self.calls: list[list[str]] = []
        self.fail_on_call = fail_on_call
        self.model = model

    def embed(self, inputs: list[str]) -> EmbedBatch:
        self.calls.append(list(inputs))
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise EmbedderUnavailable("llm service returned 503")
        return EmbedBatch(
            vectors=tuple(fake_vector(text) for text in inputs),
            model=self.model,
            dimensions=1536,
        )
