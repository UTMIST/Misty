import httpx

from contracts.embedder import EmbedBatch, EmbedderRejected, EmbedderUnavailable

# Mirrors services/llm/contracts/embed.py (batch and per-input limits) and the llm
# service's EMBED_MAX_REQUEST_CHARS default. Copied, not imported: services
# never import each other. If the llm side lowers either limit, /embed 422s
# and the indexer reports a non-retryable failure until these follow.
MAX_BATCH_INPUTS = 96
MAX_INPUT_CHARS = 32_000
# The width of doc_chunks.embedding (migration 008). A model with another
# width needs a migration, not a silent truncation.
EMBEDDING_DIMENSIONS = 1536

_TIMEOUT = httpx.Timeout(60.0, connect=5.0)


class HttpEmbedder:
    """Embeds text through the llm service's POST /embed. Splits inputs into
    requests that respect the llm service's batch and character limits and
    returns one all-or-nothing EmbedBatch."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        model: str | None = None,
        max_request_chars: int = 400_000,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._max_request_chars = max_request_chars
        # One reused client so connections pool across requests; tests inject
        # a MockTransport client (same pattern as HttpDirectoryClient).
        self._client = client or httpx.Client(timeout=_TIMEOUT)

    def embed(self, inputs: list[str]) -> EmbedBatch:
        # Validate the whole document first: a bad input in a later batch must
        # not waste paid embeddings from earlier batches that we cannot save.
        self._validate_inputs(inputs)
        vectors: list[tuple[float, ...]] = []
        model: str | None = None
        for batch in self._batches(inputs):
            batch_model, batch_vectors = self._embed_batch(batch)
            # Every chunk of one doc must share one model (validate_doc_chunks);
            # a deploy flipping the llm model mid-doc is a retryable race.
            if model is not None and batch_model != model:
                raise EmbedderUnavailable(
                    f"embedding model changed mid-document ({model} -> {batch_model})"
                )
            model = batch_model
            vectors.extend(batch_vectors)
        if model is None:
            raise EmbedderRejected("no inputs to embed")
        return EmbedBatch(vectors=tuple(vectors), model=model, dimensions=EMBEDDING_DIMENSIONS)

    def _validate_inputs(self, inputs: list[str]) -> None:
        if not inputs:
            raise EmbedderRejected("no inputs to embed")
        for i, text in enumerate(inputs):
            if not isinstance(text, str) or not text.strip():
                raise EmbedderRejected(f"input {i} must be a nonblank string")
            if len(text) > MAX_INPUT_CHARS:
                raise EmbedderRejected(f"input {i} exceeds the {MAX_INPUT_CHARS}-character limit")
            if len(text) > self._max_request_chars:
                raise EmbedderRejected(f"input {i} exceeds EMBED_MAX_REQUEST_CHARS")
            try:
                text.encode("utf-8")
            except UnicodeEncodeError:
                # Input text must never appear in the error or log.
                raise EmbedderRejected(f"input {i} must be valid UTF-8") from None

    def _batches(self, inputs: list[str]) -> list[list[str]]:
        batches: list[list[str]] = []
        current: list[str] = []
        chars = 0
        for text in inputs:
            if current and (
                len(current) == MAX_BATCH_INPUTS or chars + len(text) > self._max_request_chars
            ):
                batches.append(current)
                current, chars = [], 0
            current.append(text)
            chars += len(text)
        if current:
            batches.append(current)
        return batches

    def _embed_batch(self, batch: list[str]) -> tuple[str, list[tuple[float, ...]]]:
        body: dict = {"inputs": batch}
        if self._model is not None:
            body["model"] = self._model
        try:
            resp = self._client.post(
                f"{self._base_url}/embed", json=body, headers={"X-API-Key": self._api_key}
            )
        except httpx.HTTPError as e:
            raise EmbedderUnavailable(f"llm service unreachable: {type(e).__name__}") from e
        if resp.status_code == 429 or resp.status_code >= 500:
            raise EmbedderUnavailable(f"llm service returned {resp.status_code}")
        if resp.status_code != 200:
            raise EmbedderRejected(f"llm service returned {resp.status_code}")
        try:
            data = resp.json()
            model = data["model"]
            dimensions = data["dimensions"]
            embeddings = sorted(data["embeddings"], key=lambda e: e["index"])
            indexes = [e["index"] for e in embeddings]
            vectors = [tuple(float(x) for x in e["vector"]) for e in embeddings]
        except (ValueError, KeyError, TypeError) as e:
            raise EmbedderRejected(f"malformed /embed response: {type(e).__name__}") from e
        if not isinstance(model, str) or not model.strip():
            raise EmbedderRejected("malformed /embed response: missing model")
        if dimensions != EMBEDDING_DIMENSIONS or any(
            len(v) != EMBEDDING_DIMENSIONS for v in vectors
        ):
            raise EmbedderRejected(
                f"/embed returned {dimensions}-dimension vectors; "
                f"doc_chunks holds {EMBEDDING_DIMENSIONS}"
            )
        if indexes != list(range(len(batch))):
            raise EmbedderRejected("/embed returned a different number of vectors than inputs")
        return model, vectors
