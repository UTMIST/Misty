from fastapi import Depends, FastAPI, HTTPException

from platform_auth import AuditLogMiddleware

from contracts.storage import StorageAdapter
from src.api.deps import get_storage
from src.config import verify_production_secrets


def create_app() -> FastAPI:
    verify_production_secrets()

    app = FastAPI(
        title="UTMIST documentation-system",
        version="0.1.0",
        description="Catalog of URLs: ingest, browse, and own documents.",
        docs_url="/swagger",
    )
    app.add_middleware(AuditLogMiddleware, logger_name="documentation_system.audit")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def readiness(storage: StorageAdapter = Depends(get_storage)) -> dict[str, str]:
        if not storage.is_ready():
            raise HTTPException(status_code=503, detail="documentation-system database unavailable")
        return {"status": "ok"}

    from src.api.routers import docs, sources

    app.include_router(docs.router)
    app.include_router(sources.router)
    return app


app = create_app()
