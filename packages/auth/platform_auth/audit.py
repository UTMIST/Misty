"""Structured audit-log middleware: one JSON line per request to stdout.

The logger name is per-service (passed via add_middleware(..., logger_name=...)).
Reads request.state.auth_key (set by require_api_key) for key_name/is_bootstrap.
"""

import asyncio
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
    with reserved core fields (see ``_RESERVED_AUDIT_KEYS``) are ignored.
    On cancellation, a route can override its extra fields with
    ``request.state.audit_cancel_extra``. Values that are not JSON-native are stringified (via ``default=str``)
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
        status = 500
        try:
            with request_span(request, self._logger.name) as trace:
                try:
                    response = await call_next(request)
                    status = response.status_code
                    return response
                except asyncio.CancelledError:
                    status = 499
                    raise
                finally:
                    if trace is not None:
                        result = (
                            "cancelled"
                            if status == 499
                            else "failed"
                            if status >= 500
                            else "rate_limited"
                            if status == 429
                            else "denied"
                            if status in (401, 403)
                            else "invalid_request"
                            if status >= 400
                            else "success"
                        )
                        trace.get()._emit("end", result)
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
                    "status": status,
                    "duration_ms": duration_ms,
                    "key_name": auth_key.name if auth_key else None,
                    "is_bootstrap": auth_key.is_bootstrap if auth_key else False,
                    "remote": remote,
                }
                extra = getattr(request.state, "audit_extra", None)
                cancelled = (
                    getattr(request.state, "audit_cancel_extra", None) if status == 499 else None
                )
                for fields in (extra, cancelled):
                    if not isinstance(fields, dict):
                        continue
                    for k, v in fields.items():
                        key = str(k)  # keys must be JSON strings; default= only covers values
                        if key not in _RESERVED_AUDIT_KEYS:
                            entry[key] = v
                self._logger.info(json.dumps(entry, separators=(",", ":"), default=str))
            except Exception:
                pass
