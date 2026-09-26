"""Embeddings via the OpenAI API.

OpenAI embeds a whole batch in one request.
The `openai` SDK is imported lazily on the first embedding request.
"""

import math
import threading

from src.providers.base import (
    EmbeddingRequest,
    EmbeddingResult,
    ProviderError,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    ProviderUnsupportedModel,
    validate_vector,
)

_MODEL_IDS = {
    "openai-embed-3-small": "text-embedding-3-small",
    "openai-embed-3-large": "text-embedding-3-large",
}

# Both models are asked for these widths explicitly rather than taking their
# defaults. 3-large is natively 3072, which exceeds pgvector's 2,000-dimension
# HNSW/IVFFlat ceiling, so it is truncated to 1536 via the `dimensions`
# parameter. OpenAI vectors are unit-norm at any width, so cosine and L2 rank
# identically downstream.
_MODEL_DIMENSIONS = {
    "openai-embed-3-small": 1536,
    "openai-embed-3-large": 1536,
}


class OpenAIEmbeddingProvider:
    def __init__(self, *, api_key: str, default_model: str, timeout_s: float, client=None):
        if default_model not in _MODEL_IDS:
            raise ValueError(
                f"unsupported default embedding model {default_model!r}; "
                f"expected one of {sorted(_MODEL_IDS)}"
            )
        self._api_key = api_key
        self._default_model = default_model
        self._timeout_s = timeout_s
        self._client_lock = threading.Lock()
        # Built on first use: this constructor runs inside the get_embedder
        # dependency, outside the router's except-ladder.
        self._client = client

    def _get_client(self):
        """Built on first use. Call only from inside embed()'s try block."""
        if self._client is None:
            if not self._api_key:
                # Checked here rather than in __init__: the constructor runs at
                # boot, and a missing local credential must not stop the service
                # starting. verify_production_secrets() makes it a boot failure
                # outside `local`, where it genuinely is one.
                raise ProviderNotConfigured("OPENAI_API_KEY is not set")
            with self._client_lock:
                if self._client is None:
                    from openai import OpenAI

                    # max_retries=0: the SDK retries twice by default, extending
                    # upstream work beyond a single attempt.
                    self._client = OpenAI(
                        api_key=self._api_key, timeout=self._timeout_s, max_retries=0
                    )
        return self._client

    def _model_id(self, model: str) -> str:
        try:
            return _MODEL_IDS[model]
        except KeyError:
            raise ProviderUnsupportedModel(f"unsupported embedding model: {model!r}")

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        model = request.model or self._default_model
        model_id = self._model_id(model)
        dimensions = _MODEL_DIMENSIONS[model]

        # Imported here, not at module scope: wiring imports this module
        # unconditionally. It needs its OWN try: put it inside the one below and
        # an ImportError leaves RateLimitError unbound, so evaluating the except
        # clause raises UnboundLocalError — a 500, not a 502.
        try:
            from openai import (
                APIConnectionError,
                APIStatusError,
                APITimeoutError,
                RateLimitError,
            )
        except ImportError as exc:
            raise ProviderUnavailable("the openai package is not installed") from exc

        try:
            client = self._get_client()
            response = client.embeddings.create(
                model=model_id, input=request.inputs, dimensions=dimensions
            )
        except RateLimitError as exc:
            raise ProviderRateLimited("OpenAI embedding rate limited") from exc
        except (APITimeoutError, APIConnectionError) as exc:
            raise ProviderTimeout("OpenAI embedding timeout") from exc
        except APIStatusError as exc:
            raise ProviderUnavailable("OpenAI embedding request failed") from exc
        except ProviderError:
            # Already typed and already correct (e.g. ProviderNotConfigured from
            # _get_client). Must precede the catch-all or it gets reclassified.
            raise
        except Exception as exc:  # auth/config faults must not escape as a 500
            raise ProviderUnavailable(f"openai embedding failed: {type(exc).__name__}") from exc

        # Sort by index rather than trusting response order, then check the index
        # set is exactly 0..n-1. A count-only check accepts indices like [0, 0, 2],
        # where every width matches and the router relabels positionally — input 1
        # would be stored holding input 0's vector, under a 200.
        data = getattr(response, "data", None)
        if not isinstance(data, list):
            # sorted(None) raises a raw TypeError past the router's ladder.
            raise ProviderUnavailable(
                f"unexpected OpenAI response shape: data is {type(data).__name__}"
            )
        response_model = getattr(response, "model", None)
        if not isinstance(response_model, str) or response_model != model_id:
            raise ProviderUnavailable("OpenAI returned unexpected embedding model")
        if any(type(getattr(item, "index", None)) is not int for item in data):
            raise ProviderUnavailable("OpenAI returned invalid embedding indices")
        items = sorted(data, key=lambda d: d.index)
        indices = [item.index for item in items]
        if indices != list(range(len(request.inputs))):
            # Subsumes a plain count check: a wrong length cannot equal range(n).
            raise ProviderUnavailable("OpenAI returned invalid embedding indices")
        vectors = [getattr(item, "embedding", None) for item in items]
        for i, vector in enumerate(vectors):
            validate_vector(vector, i, dimensions)

        # Usage reporting must never turn a 200 into a 500, so a malformed count
        # is dropped rather than cast blindly.
        usage = getattr(response, "usage", None)
        raw_tokens = getattr(usage, "prompt_tokens", 0)
        try:
            input_tokens = int(raw_tokens or 0)
            if isinstance(raw_tokens, bool) or input_tokens < 0 or not math.isfinite(input_tokens):
                input_tokens = 0
        except (TypeError, ValueError, OverflowError):
            input_tokens = 0

        return EmbeddingResult(
            vectors=vectors,
            model=model_id,
            dimensions=dimensions,
            input_tokens=input_tokens,
        )
