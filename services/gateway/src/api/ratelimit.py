"""Per-key fixed-window rate limit. In-memory (process-local): correct because the
gateway runs a single replica. A shared store (Redis) is needed only if scaled >1."""

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limit: int = 60, window_s: int = 60):
        super().__init__(app)
        self._limit = limit
        self._window = window_s
        self._hits: dict[str, tuple[int, float]] = {}  # key -> (count, window_start)

    async def dispatch(self, request: Request, call_next):
        key = request.headers.get("X-API-Key")
        if key:
            now = time.monotonic()
            count, start = self._hits.get(key, (0, now))
            if now - start >= self._window:
                count, start = 0, now
            count += 1
            self._hits[key] = (count, start)
            if count > self._limit:
                return JSONResponse(status_code=429, content={"detail": "rate limit exceeded"})
        return await call_next(request)
