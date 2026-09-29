from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from platform_auth import AuditLogMiddleware

from src.config import verify_production_secrets


def create_app() -> FastAPI:
    verify_production_secrets()

    from src.api.deps import get_key_store

    get_key_store()  # fail fast on a malformed CONSUMER_KEYS at boot, not first request

    app = FastAPI(
        title="UTMIST llm",
        version="0.1.0",
        description="Shared internal LLM API (Claude via Amazon Bedrock).",
    )
    app.add_middleware(AuditLogMiddleware, logger_name="llm.audit")

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors()
        for error in errors:
            if error["type"] == "union_tag_invalid":
                error["msg"] = "Unsupported content block type"
            if error["type"] == "extra_forbidden":
                error["loc"] = error["loc"][:-1]
        return JSONResponse(
            status_code=422,
            content={
                "detail": [{key: error[key] for key in ("type", "loc", "msg")} for error in errors]
            },
        )

    from src.api.routers import chat as chat_router

    app.include_router(chat_router.router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
