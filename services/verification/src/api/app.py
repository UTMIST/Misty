from fastapi import FastAPI

from platform_auth import AuditLogMiddleware

from src.config import verify_production_secrets
from src.api.deps import get_key_store


def create_app() -> FastAPI:
    verify_production_secrets()
    get_key_store()  # Validate scoped credentials at boot, before serving requests.

    app = FastAPI(
        title="UTMIST verification",
        version="0.1.0",
        description="Proves control of an email for a given subject via one-time codes.",
    )
    app.add_middleware(AuditLogMiddleware, logger_name="verification.audit")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    from src.api.routers import verification

    app.include_router(verification.router)
    return app


app = create_app()
