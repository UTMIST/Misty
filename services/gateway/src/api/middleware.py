"""Login request boundaries and the shared audit middleware."""

from asyncio import timeout

from platform_auth import AuditLogMiddleware  # noqa: F401

from starlette.responses import JSONResponse

LOGIN_BODY_TIMEOUT_SECONDS = 5


class LoginBoundaryMiddleware:
    """Prevent caching and close rejected HTTP/1 login connections."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/v1/auth/"):
            return await self.app(scope, receive, send)

        async def private_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (key, value)
                    for key, value in message["headers"]
                    if key.lower() != b"cache-control"
                ] + [(b"cache-control", b"no-store")]
                if message["status"] in {408, 413, 429} and scope["http_version"] in {"1.0", "1.1"}:
                    # Uvicorn's idle timeout can be cancelled by another body byte
                    # after an early response. Explicitly release rejected uploads.
                    message["headers"] = [
                        (key, value)
                        for key, value in message["headers"]
                        if key.lower() != b"connection"
                    ] + [(b"connection", b"close")]
            await send(message)

        return await self.app(scope, receive, private_send)


class LoginBodyLimitMiddleware:
    """Bound body size and total receive time after the pre-authentication IP limit."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or not scope["path"].startswith("/v1/auth/")
            or scope["method"] != "POST"
        ):
            return await self.app(scope, receive, send)

        body = bytearray()
        try:
            # One deadline for the entire body; small periodic chunks cannot extend it.
            async with timeout(LOGIN_BODY_TIMEOUT_SECONDS):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > 4096:
                        response = JSONResponse(
                            status_code=413, content={"detail": "request too large"}
                        )
                        break
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        response = None
                        break
        except TimeoutError:
            response = JSONResponse(status_code=408, content={"detail": "request body timed out"})
        if response is not None:
            return await response(scope, receive, send)

        pending = True

        async def bounded_receive():
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        return await self.app(scope, bounded_receive, send)
