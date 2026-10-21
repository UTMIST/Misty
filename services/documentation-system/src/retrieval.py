"""Shared deterministic ranking rules; authorization is applied by each adapter."""

from collections import Counter
from math import isfinite
import re

from contracts.retrieval import QueryEmbedding, SearchHit, SearchRequest
from contracts.visibility import Actor, Channel


def terms(text: str) -> list[str]:
    # Match before lowercasing so Unicode case mappings cannot invent ASCII terms.
    return [term.lower() for term in re.findall(r"[A-Za-z0-9]+", text)]


def keyword_density(text: str, query: str) -> float:
    counts = Counter(terms(text))
    wanted = set(terms(query))
    if not wanted or not wanted <= counts.keys():
        return 0.0
    return sum(counts[term] for term in wanted) / counts.total()


def validate_search(query, embedding, visibility, k, min_cosine) -> QueryEmbedding:
    SearchRequest(query=query, k=k)
    if not isinstance(visibility, (Actor, Channel)):
        raise ValueError("retrieval requires a person or channel context")
    if not isfinite(min_cosine) or not 0 < min_cosine <= 1:
        raise ValueError("min_cosine must be finite and in (0,1]")
    return QueryEmbedding.model_validate(embedding)


def candidate_limit(k: int) -> int:
    return max(50, 5 * k)


def fuse(vector: list[SearchHit], keyword: list[SearchHit], k: int) -> list[SearchHit]:
    scores = {}
    hits = {}
    for branch in (vector, keyword):
        for rank, hit in enumerate(branch, 1):
            identity = (hit.doc_id, hit.chunk.ordinal)
            hits[identity] = hit
            scores[identity] = scores.get(identity, 0.0) + 1 / (60 + rank)
    ordered = sorted(scores, key=lambda identity: (-scores[identity], *identity))[:k]
    return [hits[identity].model_copy(update={"score": scores[identity]}) for identity in ordered]
