from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from contracts.directory import DirectoryUnavailable
from src.api.middleware import AuditLogMiddleware
from src.api.routers import resolve
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

    app.include_router(resolve.router)

    @app.exception_handler(DirectoryUnavailable)
    async def _directory_unavailable(request: Request, exc: DirectoryUnavailable):
        return JSONResponse(status_code=503, content={"detail": "directory temporarily unavailable"})

    return app


app = create_app()
