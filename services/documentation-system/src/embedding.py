"""Authenticated HTTP client for the platform LLM service's /embed endpoint."""

import json

import httpx

from contracts.retrieval import EmbeddingError, QueryEmbedding

RESOLVED_MODELS = {
    "openai-embed-3-small": "text-embedding-3-small",
    "openai-embed-3-large": "text-embedding-3-large",
}


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


class HttpEmbeddingClient:
    def __init__(self, base_url, api_key, model, client: httpx.Client | None = None):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._client = client or httpx.Client(timeout=httpx.Timeout(65.0))

    def embed_query(self, query: str) -> QueryEmbedding:
        if not self._api_key:
            raise EmbeddingError(503)
        try:
            response = self._client.post(
                f"{self._base_url}/embed",
                headers={"X-API-Key": self._api_key},
                json={"inputs": [query], "model": self._model},
            )
        except httpx.TimeoutException:
            raise EmbeddingError(504) from None
        except httpx.HTTPError:
            raise EmbeddingError() from None
        if response.status_code in (429, 503):
            raise EmbeddingError(503)
        if response.status_code == 504:
            raise EmbeddingError(504)
        if response.status_code != 200:
            raise EmbeddingError()
        try:
            body = json.loads(response.content, object_pairs_hook=_unique_fields)
            if body["model"] != RESOLVED_MODELS[self._model]:
                raise ValueError("incompatible model")
            if type(body["dimensions"]) is not int or body["dimensions"] != 1536:
                raise ValueError("incompatible dimensions")
            items = body["embeddings"]
            if len(items) != 1 or type(items[0]["index"]) is not int or items[0]["index"] != 0:
                raise ValueError("invalid embedding index")
            return QueryEmbedding(
                model=body["model"], dimensions=body["dimensions"], vector=items[0]["vector"]
            )
        except (KeyError, TypeError, ValueError):
            raise EmbeddingError() from None
