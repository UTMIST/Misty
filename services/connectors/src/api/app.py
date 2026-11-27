from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from platform_auth import AuditLogMiddleware

from src.config import verify_production_secrets

_STRIPPED_VALIDATION_CTX_KEYS = frozenset({"input", "value", "error"})


def _sanitize_validation_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Strip submitted values from FastAPI 422 details so private content is never echoed."""
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

    from src.api.deps import get_key_store

    get_key_store()  # fail fast on a malformed CONSUMER_KEYS at boot, not first request

    app = FastAPI(
        title="UTMIST connectors",
        version="0.1.0",
        description="Shared internal source-connector API (document URL to text).",
    )
    app.add_middleware(AuditLogMiddleware, logger_name="connectors.audit")

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(_sanitize_validation_errors(exc.errors()))},
        )

    from src.api.routers import discovery, fetch as fetch_router, gmail, meeting

    app.include_router(fetch_router.router)
    app.include_router(discovery.router)
    app.include_router(meeting.router)
    app.include_router(gmail.router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
