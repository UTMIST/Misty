import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from contracts.directory import DirectoryUnavailable
from src.api.middleware import AuditLogMiddleware
from src.api.ratelimit import ClientRateLimitMiddleware
from src.api.routers import resolve
from src.config import get_settings, verify_production_secrets

logger = logging.getLogger("gateway")


def create_app() -> FastAPI:
    verify_production_secrets()
    app = FastAPI(
        title="UTMIST gateway",
        version="0.1.0",
        description="External API gateway.",
        docs_url="/docs",
    )
    # Order matters, and add_middleware prepends: the last one added is the
    # outermost. Audit must be outermost so it still records the requests the
    # flood guard short-circuits — a 429 storm is exactly what you want in the
    # log. The per-key quota is not here; it runs as a router dependency, after
    # auth has resolved the key (see src/api/ratelimit.py).
    app.add_middleware(
        ClientRateLimitMiddleware,
        trust_proxy=get_settings().trust_proxy_headers,
    )
    app.add_middleware(AuditLogMiddleware, logger_name="gateway.audit")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(resolve.router)

    @app.exception_handler(DirectoryUnavailable)
    async def _directory_unavailable(request: Request, exc: DirectoryUnavailable):
        logger.warning("directory unavailable: %s", exc)
        return JSONResponse(
            status_code=503, content={"detail": "directory temporarily unavailable"}
        )

    return app


app = create_app()
