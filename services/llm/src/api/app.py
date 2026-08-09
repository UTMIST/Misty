from fastapi import FastAPI

from platform_auth import AuditLogMiddleware

from src.config import verify_production_secrets


def create_app() -> FastAPI:
    verify_production_secrets()

    from src.api.deps import get_key_store

    get_key_store()  # fail fast on a malformed CONSUMER_KEYS at boot, not first request

    app = FastAPI(
        title="UTMIST llm",
        version="0.1.0",
        description="Shared internal LLM API (Claude via Amazon Bedrock).",
    )
    app.add_middleware(AuditLogMiddleware, logger_name="llm.audit")

    from src.api.routers import chat as chat_router

    app.include_router(chat_router.router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
