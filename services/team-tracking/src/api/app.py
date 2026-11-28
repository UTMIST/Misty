from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from platform_auth import AuditLogMiddleware

from contracts.storage import StorageAdapter
from src.api.deps import get_storage
from src.config import verify_production_secrets


def create_app() -> FastAPI:
    verify_production_secrets()

    from src.api.routers import (
        api_keys,
        channels,
        identifiers,
        memberships,
        people,
        providers,
        role_kinds,
        teams,
    )

    app = FastAPI(
        title="UTMIST team-tracking",
        version="0.1.0",
        description="Source of truth for people, teams, and memberships.",
    )
    app.add_middleware(AuditLogMiddleware, logger_name="team_tracking.audit")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if request.scope.get("endpoint") is people.lookup_primary_email:
            # Pydantic's errors can contain the address in input, extra-field
            # locations and error context. Only fixed fields leave this route.
            return JSONResponse(
                status_code=422,
                content={
                    "detail": [
                        {"loc": ["body"], "msg": "Invalid email lookup", "type": "value_error"}
                    ]
                },
            )
        return await request_validation_exception_handler(request, exc)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def readiness(storage: StorageAdapter = Depends(get_storage)) -> dict[str, str]:
        if not storage.is_ready():
            raise HTTPException(status_code=503, detail="team-tracking database unavailable")
        return {"status": "ok"}

    app.include_router(people.router)
    app.include_router(teams.router)
    app.include_router(role_kinds.router)
    app.include_router(memberships.router)
    app.include_router(providers.router)
    app.include_router(identifiers.router)
    app.include_router(channels.router)
    app.include_router(api_keys.router)
    return app


app = create_app()
