from collections.abc import Sequence
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from platform_auth import AuditLogMiddleware

from contracts.storage import StorageAdapter
from src.api.deps import get_storage
from src.config import verify_production_secrets

_STRIPPED_VALIDATION_CTX_KEYS = frozenset({"input", "value", "error"})


def _sanitize_validation_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Strip submitted values from FastAPI 422 details so private request fields are never echoed."""
    sanitized: list[dict[str, Any]] = []
    for err in errors:
        if not isinstance(err, dict):
            continue
        cleaned = {k: v for k, v in err.items() if k != "input"}
        ctx = cleaned.get("ctx")
        if isinstance(ctx, dict):
            safe_ctx = {
                ck: str(cv) if isinstance(cv, BaseException) else cv
                for ck, cv in ctx.items()
                if ck not in _STRIPPED_VALIDATION_CTX_KEYS
            }
            if safe_ctx:
                cleaned["ctx"] = safe_ctx
            else:
                cleaned.pop("ctx", None)
        sanitized.append(cleaned)
    return sanitized


def create_app() -> FastAPI:
    verify_production_secrets()

    app = FastAPI(
        title="UTMIST documentation-system",
        version="0.1.0",
        description="Catalog of URLs: ingest, browse, and own documents.",
        docs_url="/swagger",
    )
    app.add_middleware(AuditLogMiddleware, logger_name="documentation_system.audit")

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(_sanitize_validation_errors(exc.errors()))},
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def readiness(storage: StorageAdapter = Depends(get_storage)) -> dict[str, str]:
        if not storage.is_ready():
            raise HTTPException(status_code=503, detail="documentation-system database unavailable")
        return {"status": "ok"}

    from src.api.routers import (
        api_keys,
        discovery,
        docs,
        email_submissions,
        meeting_records,
        search,
        sources,
    )

    app.include_router(discovery.router)
    app.include_router(docs.router)
    app.include_router(sources.router)
    app.include_router(search.router)
    app.include_router(api_keys.router)
    app.include_router(meeting_records.router)
    app.include_router(email_submissions.router)
    return app


app = create_app()
