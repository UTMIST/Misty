from fastapi import FastAPI
from platform_auth import AuditLogMiddleware

from src.config import verify_production_secrets


def create_app() -> FastAPI:
    verify_production_secrets()

    from src.api.deps import get_key_store

    get_key_store()  # fail fast on a malformed CONSUMER_KEYS at boot, not first request

    app = FastAPI(
        title="UTMIST connectors",
        version="0.1.0",
        description="Shared internal source-connector API (document URL to text).",
    )
    app.add_middleware(AuditLogMiddleware, logger_name="connectors.audit")

    from src.api.routers import fetch as fetch_router

    app.include_router(fetch_router.router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
