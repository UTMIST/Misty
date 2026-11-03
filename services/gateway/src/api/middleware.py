"""Thin shim re-exporting the shared audit middleware."""

from platform_auth import AuditLogMiddleware  # noqa: F401

from starlette.responses import JSONResponse


class LoginBoundaryMiddleware:
    """Bound login bodies before JSON parsing and prevent caching, including errors."""

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
            await send(message)

        if scope["method"] == "POST":
            chunks = []
            size = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                size += len(message.get("body", b""))
                if size > 4096:
                    response = JSONResponse(
                        status_code=413, content={"detail": "request too large"}
                    )
                    return await response(scope, receive, private_send)
                chunks.append(message)
                if not message.get("more_body", False):
                    break
            pending = iter(chunks)

            async def bounded_receive():
                return next(pending, None) or await receive()

            return await self.app(scope, bounded_receive, private_send)
        return await self.app(scope, receive, private_send)
