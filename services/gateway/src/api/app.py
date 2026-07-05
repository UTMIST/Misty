from fastapi import FastAPI

from src.api.middleware import AuditLogMiddleware
from src.config import verify_production_secrets


def create_app() -> FastAPI:
    verify_production_secrets()
    app = FastAPI(
        title="UTMIST gateway",
        version="0.1.0",
        description="External API gateway.",
        docs_url="/docs",
    )
    app.add_middleware(AuditLogMiddleware, logger_name="gateway.audit")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
