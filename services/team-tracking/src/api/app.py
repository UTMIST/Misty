from fastapi import FastAPI

from platform_auth import AuditLogMiddleware

from src.config import verify_production_secrets


def create_app() -> FastAPI:
    verify_production_secrets()

    from src.api.routers import (
        api_keys,
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

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(people.router)
    app.include_router(teams.router)
    app.include_router(role_kinds.router)
    app.include_router(memberships.router)
    app.include_router(providers.router)
    app.include_router(identifiers.router)
    app.include_router(api_keys.router)
    return app


app = create_app()
