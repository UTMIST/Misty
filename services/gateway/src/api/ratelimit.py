"""Per-key fixed-window rate limit. In-memory (process-local): correct because the
gateway runs a single replica. A shared store (Redis) is needed only if scaled >1."""

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class RateLimitMiddleware(BaseHTTPMiddleware):
    _SWEEP_THRESHOLD = 1024

    def __init__(self, app, limit: int = 60, window_s: int = 60):
        super().__init__(app)
        self._limit = limit
        self._window = window_s
        self._hits: dict[str, tuple[int, float]] = {}  # key -> (count, window_start)

    def _sweep(self, now: float) -> None:
        """Opportunistically evict entries whose window has fully expired.
        Only runs once the dict grows large, so it stays O(1) amortized."""
        if len(self._hits) < self._SWEEP_THRESHOLD:
            return
        expired = [k for k, (_, start) in self._hits.items() if now - start >= self._window]
        for k in expired:
            del self._hits[k]

    async def dispatch(self, request: Request, call_next):
        key = request.headers.get("X-API-Key")
        if key:
            now = time.monotonic()
            self._sweep(now)
            count, start = self._hits.get(key, (0, now))
            if now - start >= self._window:
                count, start = 0, now
            count += 1
            self._hits[key] = (count, start)
            self._sweep(now)
            if count > self._limit:
                return JSONResponse(status_code=429, content={"detail": "rate limit exceeded"})
        return await call_next(request)
