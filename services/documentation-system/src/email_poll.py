"""External, bounded HTTP runner for the forwarded-email polling schedule.

Needs only DOCS_BASE_URL and DOCS_API_KEY (with emails:write); database and
Google OAuth credentials stay in their owning APIs. Logs contain counts and run IDs.
"""

import argparse
from dataclasses import asdict, dataclass
import json
import math
import os
import sys
import time
from uuid import uuid4

import httpx
from pydantic import AnyHttpUrl, SecretStr, TypeAdapter, ValidationError

from contracts.email import EmailPollResult

DEFAULT_BASE_URL = "http://localhost:8001"
POLL_PATH = "/email-submissions/poll"
GUARD_PREFIX = "/email-submissions/poll-runs"
MONITOR_TIMEOUT_SECONDS = 10.0


class EmailPollError(Exception):
    """Sanitized operational error; never carries upstream response bodies or addresses."""


class EmailPollTimeout(EmailPollError):
    """HTTP phase timed out; the API may still be processing the batch."""


class EmailPollBudgetExpired(EmailPollError):
    """Elapsed-time budget reached before draining the inbox."""


@dataclass
class PollReport:
    batches: int = 0
    listed: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected_oversize: int = 0
    ignored_non_inbox: int = 0
    ignored_loop: int = 0
    rejected_unauthenticated: int = 0
    ignored_unknown_sender: int = 0
    directory_deferred: int = 0
    inspect_failed: int = 0
    receipts_attempted: int = 0
    receipts_sent: int = 0
    receipts_suppressed: int = 0
    receipts_failed: int = 0
    expired_cleaned: int = 0
    drained: bool = False
    budget_exhausted: bool = False
    guard_released: bool = False
    monitor_failed: bool = False
    error: str = ""

    @property
    def exit_code(self) -> int:
        if self.error or not self.drained or self.monitor_failed:
            return 2
        return 0

    @property
    def has_warnings(self) -> bool:
        return bool(self.directory_deferred or self.inspect_failed or self.receipts_failed)


def _validate_https_monitor(raw_url: str) -> str:
    try:
        url = TypeAdapter(AnyHttpUrl).validate_python(raw_url)
    except ValidationError:
        raise EmailPollError("EMAIL_POLL_MONITOR_URL must be a valid HTTPS URL") from None
    if url.scheme != "https" or url.username or url.password or url.query or url.fragment:
        raise EmailPollError(
            "EMAIL_POLL_MONITOR_URL must use HTTPS without credentials, query or fragment"
        )
    return str(url).rstrip("/")


def _ping_monitor(base_url: str, suffix: str, *, run_id: str, client: httpx.Client | None) -> bool:
    if not base_url:
        return True
    own_client = client is None
    http = client or httpx.Client(timeout=MONITOR_TIMEOUT_SECONDS, follow_redirects=False)
    try:
        resp = http.post(
            f"{base_url}{suffix}",
            params={"rid": run_id},
            headers={"Connection": "close"},
            timeout=MONITOR_TIMEOUT_SECONDS,
        )
        return resp.status_code == 200 and resp.text.strip() == "OK"
    except httpx.HTTPError:
        return False
    finally:
        if own_client:
            http.close()


class DocsEmailClient:
    def __init__(self, *, base_url: str, api_key: SecretStr, client: httpx.Client) -> None:
        try:
            url = TypeAdapter(AnyHttpUrl).validate_python(base_url)
        except ValidationError:
            raise EmailPollError("DOCS_BASE_URL must be an absolute HTTP(S) URL") from None
        if url.username or url.password or url.query or url.fragment:
            raise EmailPollError(
                "DOCS_BASE_URL must be HTTP(S), without credentials, query or fragment"
            )
        self._base_url = str(url).rstrip("/")
        self._api_key = api_key
        self._client = client

    def _request(self, method: str, path: str, *, timeout: float, params=None):
        try:
            response = self._client.request(
                method,
                f"{self._base_url}{path}",
                headers={"X-API-Key": self._api_key.get_secret_value()},
                params=params,
                timeout=timeout,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise EmailPollTimeout(f"{method} {path} timed out; request was not retried") from None
        except httpx.HTTPError:
            raise EmailPollError(f"{method} {path} failed; request was not retried") from None
        return response

    def check_scope(self, *, timeout: float) -> None:
        resp = self._request("GET", "/api-keys/self", timeout=timeout)
        if resp.status_code != 200:
            raise EmailPollError(f"GET /api-keys/self returned HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            raise EmailPollError("GET /api-keys/self returned invalid JSON") from None
        scopes = body.get("scopes") if isinstance(body, dict) else None
        if not isinstance(scopes, list) or any(not isinstance(s, str) for s in scopes):
            raise EmailPollError("GET /api-keys/self returned an invalid scope list")
        if "emails:write" not in scopes and "admin" not in scopes:
            raise EmailPollError("DOCS_API_KEY requires emails:write")

    def acquire_guard(self, run_id: str, *, timeout: float) -> None:
        resp = self._request("POST", f"{GUARD_PREFIX}/{run_id}", timeout=timeout)
        if resp.status_code == 409:
            raise EmailPollError("another email poll run is already active")
        if resp.status_code != 200:
            raise EmailPollError(f"guard acquire returned HTTP {resp.status_code}")

    def release_guard(self, run_id: str, *, timeout: float) -> bool:
        try:
            resp = self._request("DELETE", f"{GUARD_PREFIX}/{run_id}", timeout=timeout)
            return resp.status_code == 200
        except EmailPollError:
            return False

    def poll_batch(self, *, limit: int, max_pages: int, timeout: float) -> EmailPollResult:
        resp = self._request(
            "POST",
            POLL_PATH,
            params={"limit": limit, "max_pages": max_pages},
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise EmailPollError(f"POST {POLL_PATH} returned HTTP {resp.status_code}")
        try:
            return EmailPollResult.model_validate(resp.json())
        except (ValueError, ValidationError):
            raise EmailPollError(f"POST {POLL_PATH} returned invalid response shape") from None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="doc-email-poll")
    p.add_argument("--base-url", default=None)
    p.add_argument("--batch-size", type=int, default=25)
    p.add_argument("--max-pages", type=int, default=5)
    p.add_argument("--max-batches", type=int, default=20)
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--max-seconds", type=float, default=240.0)
    p.add_argument("--require-monitor", action="store_true")
    return p


def run_poll(
    argv: list[str] | None = None,
    *,
    env: dict[str, str] | None = None,
    http_client: httpx.Client | None = None,
    monitor_client: httpx.Client | None = None,
    clock=time.monotonic,
) -> tuple[int, dict]:
    args = build_parser().parse_args(argv)
    environ = env if env is not None else os.environ
    run_id = str(uuid4())
    report = PollReport()

    if not (1 <= args.batch_size <= 100) or not (1 <= args.max_pages <= 20) or args.max_batches < 1:
        report.error = "invalid batch, page, or max-batches bounds"
        return 2, {"event": "finish", "run_id": run_id, "level": "error", **asdict(report)}
    if (
        not math.isfinite(args.timeout)
        or args.timeout <= 0
        or not math.isfinite(args.max_seconds)
        or args.max_seconds <= 0
    ):
        report.error = "timeout and max-seconds must be positive finite numbers"
        return 2, {"event": "finish", "run_id": run_id, "level": "error", **asdict(report)}

    raw_key = environ.get("DOCS_API_KEY", "").strip()
    if not raw_key:
        report.error = "DOCS_API_KEY is required"
        return 2, {"event": "finish", "run_id": run_id, "level": "error", **asdict(report)}

    raw_monitor = environ.get("EMAIL_POLL_MONITOR_URL", "").strip()
    if args.require_monitor and not raw_monitor:
        report.error = "EMAIL_POLL_MONITOR_URL is required with --require-monitor"
        return 2, {"event": "finish", "run_id": run_id, "level": "error", **asdict(report)}
    try:
        monitor_url = _validate_https_monitor(raw_monitor) if raw_monitor else ""
    except EmailPollError as exc:
        report.error = str(exc)
        return 2, {"event": "finish", "run_id": run_id, "level": "error", **asdict(report)}

    base_url = args.base_url or environ.get("DOCS_BASE_URL", DEFAULT_BASE_URL)
    own_http = http_client is None
    client_http = http_client or httpx.Client()
    acquired = False
    started_ok = True
    deadline = clock() + args.max_seconds

    try:
        api = DocsEmailClient(base_url=base_url, api_key=SecretStr(raw_key), client=client_http)
        api.check_scope(timeout=min(args.timeout, max(1.0, deadline - clock())))
        api.acquire_guard(run_id, timeout=min(args.timeout, max(1.0, deadline - clock())))
        acquired = True

        if monitor_url:
            started_ok = _ping_monitor(monitor_url, "/start", run_id=run_id, client=monitor_client)
            if not started_ok:
                report.monitor_failed = True

        for _ in range(args.max_batches):
            remaining = deadline - clock()
            if remaining <= 0:
                report.budget_exhausted = True
                raise EmailPollBudgetExpired("email poll time budget exhausted")
            batch = api.poll_batch(
                limit=args.batch_size,
                max_pages=args.max_pages,
                timeout=min(args.timeout, remaining),
            )
            report.batches += 1
            for field in (
                "listed",
                "accepted",
                "duplicates",
                "rejected_oversize",
                "ignored_non_inbox",
                "ignored_loop",
                "rejected_unauthenticated",
                "ignored_unknown_sender",
                "directory_deferred",
                "inspect_failed",
                "receipts_attempted",
                "receipts_sent",
                "receipts_suppressed",
                "receipts_failed",
                "expired_cleaned",
            ):
                setattr(report, field, getattr(report, field) + getattr(batch, field))
            if batch.complete:
                report.drained = True
                break
        else:
            report.error = "max-batches reached before inbox drained"
    except EmailPollError as exc:
        report.error = str(exc)
    finally:
        if acquired and (report.drained or report.budget_exhausted):
            report.guard_released = api.release_guard(run_id, timeout=min(args.timeout, 15.0))
        if monitor_url:
            if not acquired:
                _ping_monitor(monitor_url, "/fail", run_id=run_id, client=monitor_client)
            elif report.drained and not report.error and started_ok:
                if not _ping_monitor(monitor_url, "", run_id=run_id, client=monitor_client):
                    report.monitor_failed = True
            else:
                _ping_monitor(monitor_url, "/fail", run_id=run_id, client=monitor_client)
        if own_http:
            client_http.close()

    code = report.exit_code
    level = "error" if code != 0 else ("warning" if report.has_warnings else "info")
    outcome = (
        "failed"
        if code != 0
        else ("completed_with_warnings" if report.has_warnings else "completed")
    )
    summary = {
        "event": "finish",
        "run_id": run_id,
        "level": level,
        "outcome": outcome,
        **asdict(report),
    }
    return code, summary


def main(argv: list[str] | None = None) -> int:
    code, summary = run_poll(argv)
    print(json.dumps(summary, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
