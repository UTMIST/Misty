"""Deterministic offline embeddings for the retrieval-quality harness (#178).

#178 requires measurement to run on fixtures rather than paid provider calls, so the
harness never reaches the llm service's /embed. It embeds with the stand-in below,
which satisfies both halves of retrieval: `embed` is the indexing-side `Embedder`
(contracts/embedder.py) that `src/indexing.py` calls, and `embed_query` is the
query-side shape `search_chunks` expects (contracts/retrieval.py).

**This is a measuring instrument, not a model.** It scores vocabulary overlap: a query
and a chunk are similar to the extent they use the same words. Real embeddings place
"reimbursement" near "get paid back"; this does not, and scores them zero. That makes
it useless for judging retrieval quality and well suited to the one question the
harness asks -- when chunking changes, do the right documents still surface? -- because
it is free, instant, and returns the identical vector on every machine and every run.

Never report a number from this as a claim about production retrieval.

It is deliberately not tests/fake_embedder.py, which derives a vector from eight bytes
of a digest: that is right for "was this vector stored?" and useless for ranking, since
near-identical texts land in unrelated directions. Here similarity must mean something.

Method: feature hashing. Each term lands in a fixed bucket carrying a fixed sign,
weighted by sublinear term frequency, and the vector is L2-normalized so cosine is a
plain dot product and long documents gain no advantage. Signed buckets let two colliding
terms cancel on average instead of inventing similarity between unrelated texts. Hashing
with SHA-256 rather than hash() is what makes it reproducible: hash() is salted per
process, so it would silently change scores between runs.
"""

from collections import Counter
from hashlib import sha256
from math import log, sqrt

from contracts.embedder import EmbedBatch, EmbedderRejected
from contracts.retrieval import QueryEmbedding
from src.retrieval import terms

# Fixed by the storage contract: contracts/types.py and the vector(1536) column.
# Not a choice this module gets to make.
DIMENSIONS = 1536

# Stored on every chunk this embedder produces, and declared by every query it embeds.
# The two must match or search_chunks skips the chunk. Deliberately not a real provider
# id: a stored vector should never be mistakable for one a paid model produced. Bump the
# suffix on ANY change that alters the output for the same text -- scores either side of
# a bump are not comparable, and this label is what records that.
MODEL = "fake-lexical-v1"


class LexicalEmbedder:
    """Text in, unit vector out. Deterministic across processes, machines and runs."""

    def __init__(self, model: str = MODEL):
        self.model = model

    def embed(self, inputs: list[str]) -> EmbedBatch:
        """The indexing-side Embedder contract: all-or-nothing, input order kept."""
        return EmbedBatch(
            vectors=tuple(self.embed_text(text) for text in inputs),
            model=self.model,
            dimensions=DIMENSIONS,
        )

    def embed_query(self, query: str) -> QueryEmbedding:
        """The query-side shape retrieval expects (contracts/retrieval.py)."""
        return QueryEmbedding(
            model=self.model, dimensions=DIMENSIONS, vector=self.embed_text(query)
        )

    def embed_text(self, text: str) -> tuple[float, ...]:
        counts = Counter(terms(text))
        if not counts:
            # QueryEmbedding and DocChunk both reject an all-zero vector, and should:
            # text with no indexable terms has no lexical content to measure. Raising
            # the contract's own rejection keeps index_doc's error handling intact.
            raise EmbedderRejected(f"no indexable terms in {text[:60]!r}")
        vector = [0.0] * DIMENSIONS
        for term, count in counts.items():
            digest = sha256(term.encode()).digest()
            bucket = int.from_bytes(digest[:4], "big") % DIMENSIONS
            sign = 1.0 if digest[4] & 1 else -1.0
            # Sublinear: a term repeated twenty times matters more than one said once,
            # but not twenty times more, or boilerplate would dominate the vector.
            vector[bucket] += sign * (1.0 + log(count))
        norm = sqrt(sum(value * value for value in vector))
        return tuple(value / norm for value in vector)
