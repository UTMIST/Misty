from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from platform_auth import AuditLogMiddleware

from src.config import verify_production_secrets


def create_app() -> FastAPI:
    verify_production_secrets()

    from src.api.deps import get_embedder, get_key_store

    get_key_store()  # fail fast on a malformed CONSUMER_KEYS at boot, not first request
    # Same reason: a bad EMBED_MODEL raises from construction
    # inside a dependency, which would be an unhandled 500 on a deploy whose
    # /health stayed green. Safe here — the OpenAI client is built lazily.
    get_embedder()

    app = FastAPI(
        title="UTMIST llm",
        version="0.1.0",
        description="Shared internal LLM API (Bedrock chat and OpenAI embeddings).",
    )
    app.add_middleware(AuditLogMiddleware, logger_name="llm.audit")

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {key: error[key] for key in ("type", "loc", "msg")} for error in exc.errors()
                ]
            },
        )

    from src.api.routers import chat as chat_router
    from src.api.routers import embed as embed_router

    app.include_router(chat_router.router)
    app.include_router(embed_router.router)

    # async so the healthcheck runs on the event loop and can never queue
    # behind /embed work in the sync worker pool.
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
