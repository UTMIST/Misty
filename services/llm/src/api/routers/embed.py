import anyio
import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException, Request

from contracts.embed import EmbedRequest, EmbedResponse, EmbedUsage, Embedding
from src.api.auth import require_scope
from src.api.deps import get_embedder
from src.config import get_settings
from src.providers.base import (
    EmbeddingProvider,
    EmbeddingRequest,
    ProviderError,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnsupportedModel,
)

router = APIRouter()

# The SDK call is blocking. A separate limiter caps embedding calls at eight;
# it does not borrow tokens from AnyIO's default thread limiter. Extra /embed
# requests wait on the event loop instead of occupying worker threads, and
# /health stays async so it does not wait for those calls.
_EMBED_LIMITER = anyio.CapacityLimiter(8)


@router.post(
    "/embed",
    response_model=EmbedResponse,
    responses={
        422: {
            "description": (
                "Validation failure: field errors use a `detail` list; the aggregate "
                "character limit uses a `detail` string."
            )
        }
    },
)
async def embed(
    body: EmbedRequest,
    request: Request,
    # Its own scope, not `chat`: the two bill against different models.
    _key=Depends(require_scope("embed")),
    embedder: EmbeddingProvider = Depends(get_embedder),
) -> EmbedResponse:
    settings = get_settings()
    # Aggregate input guard, enforced before any paid call. Per-input caps
    # live in contracts/embed.py; this additionally limits the whole batch.
    # Character limits are not exact token or spending bounds.
    total_chars = sum(len(text) for text in body.inputs)
    if total_chars > settings.embed_max_request_chars:
        raise HTTPException(
            status_code=422,
            detail=(
                f"batch is {total_chars} characters, over the "
                f"{settings.embed_max_request_chars} limit for one request; "
                "split it into smaller batches"
            ),
        )
    embed_request = EmbeddingRequest(inputs=body.inputs, model=body.model)
    request.state.audit_extra = {
        "model": body.model or settings.embed_model,
        "input_count": len(body.inputs),
        "input_chars": total_chars,
    }
    try:
        result = await anyio.to_thread.run_sync(
            embedder.embed, embed_request, limiter=_EMBED_LIMITER
        )
    except ProviderUnsupportedModel as exc:
        # Model validation inside the provider is a client error, not an upstream failure.
        raise HTTPException(status_code=422, detail=str(exc))
    except ProviderNotConfigured as exc:
        # Nothing upstream failed; this deployment has no OpenAI credential.
        raise HTTPException(status_code=503, detail=str(exc))
    except ProviderRateLimited:
        raise HTTPException(status_code=429, detail="embedding provider rate limited")
    except ProviderTimeout:
        raise HTTPException(status_code=504, detail="embedding provider timeout")
    except ProviderError:
        raise HTTPException(status_code=502, detail="embedding provider error")
    request.state.audit_extra.update(
        {"dimensions": result.dimensions, "input_tokens": result.input_tokens}
    )

    return EmbedResponse(
        embeddings=[Embedding(index=i, vector=v) for i, v in enumerate(result.vectors)],
        model=result.model,
        dimensions=result.dimensions,
        usage=EmbedUsage(input_tokens=result.input_tokens),
    )
