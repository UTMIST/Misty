"""Structured audit-log middleware: one JSON line per request to stdout.

The logger name is per-service (passed via add_middleware(..., logger_name=...)).
Reads request.state.auth_key (set by require_api_key) for key_name/is_bootstrap.
"""

import json
import logging
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from platform_auth.telemetry import request_span

_RESERVED_AUDIT_KEYS = frozenset(
    {
        "ts",
        "request_id",
        "method",
        "path",
        "status",
        "duration_ms",
        "key_name",
        "is_bootstrap",
        "remote",
    }
)


class _SysStdoutStream:
    """Resolve sys.stdout at write time so pytest's capsys can capture it."""

    def write(self, msg: str) -> int:
        return sys.stdout.write(msg)

    def flush(self) -> None:
        sys.stdout.flush()


def _configure(logger: logging.Logger) -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler(_SysStdoutStream())  # type: ignore[arg-type]
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class AuditLogMiddleware(BaseHTTPMiddleware):
    """Emits one JSON audit line per request.

    Consumer contract: a route may set ``request.state.audit_extra`` to a
    dict of additional fields to merge into the log entry. Keys that collide
    with reserved core fields (see ``_RESERVED_AUDIT_KEYS``) are ignored, and
    values that are not JSON-native are stringified (via ``default=str``)
    rather than causing the whole line to be dropped.
    """

    def __init__(self, app, logger_name: str = "platform.audit"):
        super().__init__(app)
        self._logger = logging.getLogger(logger_name)

    async def dispatch(self, request: Request, call_next):
        _configure(self._logger)
        request_id = str(uuid4())
        request.state.request_id = request_id
        start = time.monotonic()
        response = None
        try:
            with request_span(request, self._logger.name) as trace:
                try:
                    response = await call_next(request)
                    return response
                finally:
                    if trace is not None:
                        observed = trace.get()
                        code = response.status_code if response is not None else 500
                        result = (
                            "failed"
                            if code >= 500
                            else "rate_limited"
                            if code == 429
                            else "denied"
                            if code in (401, 403)
                            else "invalid_request"
                            if code >= 400
                            else "success"
                        )
                        observed._emit("end", result)
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            try:
                auth_key = getattr(request.state, "auth_key", None)
                remote = (
                    request.headers.get("X-Real-IP")
                    or (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or None)
                    or (request.client.host if request.client else None)
                )
                entry = {
                    "ts": _now_iso(),
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code if response is not None else 500,
                    "duration_ms": duration_ms,
                    "key_name": auth_key.name if auth_key else None,
                    "is_bootstrap": auth_key.is_bootstrap if auth_key else False,
                    "remote": remote,
                }
                extra = getattr(request.state, "audit_extra", None)
                if isinstance(extra, dict):
                    for k, v in extra.items():
                        key = str(k)  # keys must be JSON strings; default= only covers values
                        if key not in _RESERVED_AUDIT_KEYS:
                            entry[key] = v
                self._logger.info(json.dumps(entry, separators=(",", ":"), default=str))
            except Exception:
                pass
