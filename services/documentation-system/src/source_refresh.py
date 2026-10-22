"""External, bounded HTTP runner for the source-access refresh schedule.

Like doc-backfill, this client needs only DOCS_BASE_URL and DOCS_API_KEY;
database and provider credentials stay in the API. Output contains counts and run IDs.
"""

import argparse
import math
import os
import sys
import time
from dataclasses import dataclass
from uuid import uuid4

import httpx
from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, TypeAdapter, ValidationError

DEFAULT_BASE_URL = "http://localhost:8001"
REFRESH_PATH = "/docs/source-access/refresh-due"


class RefreshError(Exception):
    """A sanitized operational failure; never include response bodies or URLs."""


class Batch(BaseModel):
    attempted: int = Field(strict=True, ge=0)
    refreshed: int = Field(strict=True, ge=0)
    failed: int = Field(strict=True, ge=0)
    index_failed: int = Field(strict=True, ge=0)


@dataclass
class Report:
    batches: int = 0
    attempted: int = 0
    refreshed: int = 0
    failed: int = 0
    index_failed: int = 0
    drained: bool = False
    error: str = ""

    @property
    def exit_code(self) -> int:
        if self.error or not self.drained:
            return 2
        return 1 if self.failed or self.index_failed else 0

    def summary(self) -> str:
        return (
            f"batches={self.batches} attempted={self.attempted} refreshed={self.refreshed} "
            f"failed={self.failed} index_failed={self.index_failed} "
            f"drained={str(self.drained).lower()}"
        )


class DocsApiClient:
    def __init__(self, *, base_url: str, api_key: SecretStr, client: httpx.Client) -> None:
        try:
            url = TypeAdapter(AnyHttpUrl).validate_python(base_url)
        except ValidationError:
            raise RefreshError("DOCS_BASE_URL must be an absolute HTTP(S) URL") from None
        if url.username or url.password or url.query or url.fragment:
            raise RefreshError(
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
        except httpx.HTTPError:
            # A timeout does not prove the server stopped. Do not replay the POST.
            raise RefreshError(f"{method} {path} failed; request was not retried") from None
        if response.status_code != 200:
            raise RefreshError(f"{method} {path} returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise RefreshError(f"{method} {path} returned invalid JSON") from None

    def check_scope(self, *, timeout: float) -> None:
        body = self._request("GET", "/api-keys/self", timeout=timeout)
        scopes = body.get("scopes") if isinstance(body, dict) else None
        if not isinstance(scopes, list) or any(not isinstance(s, str) for s in scopes):
            raise RefreshError("GET /api-keys/self returned an invalid scope list")
        if "docs:write" not in scopes and "admin" not in scopes:
            raise RefreshError("DOCS_API_KEY requires docs:write")

    def refresh_batch(self, *, limit: int, timeout: float) -> Batch:
        body = self._request("POST", REFRESH_PATH, params={"limit": limit}, timeout=timeout)
        try:
            batch = Batch.model_validate(body)
        except ValidationError:
            raise RefreshError("refresh endpoint returned invalid counts") from None
        if (
            batch.attempted > limit
            or batch.refreshed + batch.failed != batch.attempted
            or batch.index_failed > batch.refreshed
        ):
            raise RefreshError("refresh endpoint returned inconsistent counts")
        return batch

    def acquire_run(self, run_id: str) -> None:
        self._run_guard_request("POST", run_id)

    def release_run(self, run_id: str) -> None:
        self._run_guard_request("DELETE", run_id)

    def _run_guard_request(self, method: str, run_id: str) -> None:
        body = self._request(method, f"/docs/source-access/refresh-runs/{run_id}", timeout=10)
        if not isinstance(body, dict) or body.get("run_id") != run_id:
            raise RefreshError("run guard returned an invalid acknowledgement")


class RunMonitor:
    """Healthchecks-compatible pings with no catalog credentials or document data."""

    def __init__(self, url: SecretStr, *, client: httpx.Client) -> None:
        try:
            parsed = TypeAdapter(AnyHttpUrl).validate_python(url.get_secret_value())
        except ValidationError:
            raise RefreshError("SOURCE_REFRESH_MONITOR_URL must be an HTTPS ping URL") from None
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise RefreshError(
                "SOURCE_REFRESH_MONITOR_URL must use HTTPS without credentials, query or fragment"
            )
        self._url = SecretStr(str(parsed).rstrip("/"))
        self._client = client
        self.run_id = str(uuid4())

    def ping(self, event: str) -> bool:
        suffix = {"start": "/start", "success": "", "fail": "/fail"}[event]
        try:
            response = self._client.post(
                self._url.get_secret_value() + suffix,
                params={"rid": self.run_id},
                timeout=10,
                follow_redirects=False,
            )
            # Healthchecks returns HTTP 200 even for unknown/rate-limited checks.
            return response.status_code == 200 and response.text.strip() == "OK"
        except httpx.HTTPError:
            return False


def _validate_limits(batch_size: int, max_batches: int, timeout: float, max_seconds: float):
    if not 1 <= batch_size <= 100:
        raise RefreshError("batch size must be between 1 and 100")
    if max_batches < 1:
        raise RefreshError("max batches must be positive")
    if any(not math.isfinite(value) or value <= 0 for value in (timeout, max_seconds)):
        raise RefreshError("timeout and max seconds must be finite and positive")


def run_refresh(
    client: DocsApiClient,
    *,
    batch_size: int = 10,
    max_batches: int = 100,
    timeout: float = 600,
    max_seconds: float = 3000,
) -> Report:
    """Drain due batches, retaining failures and stopping on ambiguous HTTP results.

    The API records failed attempts, so per-document failures do not prevent the
    next batch from advancing. Index failures are counted separately, not retried
    here. The time budget gates new requests; HTTPX timeouts bound network phases.
    """
    _validate_limits(batch_size, max_batches, timeout, max_seconds)
    report = Report()
    deadline = time.monotonic() + max_seconds

    def request_timeout() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RefreshError("time budget exhausted before the queue was drained")
        return min(timeout, remaining)

    try:
        client.check_scope(timeout=request_timeout())
        for _ in range(max_batches):
            batch = client.refresh_batch(limit=batch_size, timeout=request_timeout())
            report.batches += 1
            report.attempted += batch.attempted
            report.refreshed += batch.refreshed
            report.failed += batch.failed
            report.index_failed += batch.index_failed
            if batch.attempted < batch_size:
                report.drained = True
                return report
        report.error = "batch budget exhausted before the queue was drained"
    except RefreshError as exc:
        report.error = str(exc)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="doc-source-refresh",
        description="Drain due source-access refreshes over HTTP; reads DOCS_API_KEY from env.",
    )
    parser.add_argument("--base-url", default=os.environ.get("DOCS_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--max-batches", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=600, help="HTTP phase timeout in seconds")
    parser.add_argument(
        "--max-seconds", type=float, default=3000, help="budget for starting requests"
    )
    parser.add_argument(
        "--require-monitor", action="store_true", help="require SOURCE_REFRESH_MONITOR_URL"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.monotonic()
    report = Report()
    monitor_failed = False
    guard_acquired = False
    try:
        # Separate clients keep catalog headers and cookies away from the monitor.
        with httpx.Client() as http, httpx.Client() as monitor_http:
            monitor_url = SecretStr(os.environ.get("SOURCE_REFRESH_MONITOR_URL", ""))
            monitor = (
                RunMonitor(monitor_url, client=monitor_http)
                if monitor_url.get_secret_value()
                else None
            )
            run_id = monitor.run_id if monitor else str(uuid4())
            try:
                _validate_limits(args.batch_size, args.max_batches, args.timeout, args.max_seconds)
                api_key = SecretStr(os.environ.get("DOCS_API_KEY", ""))
                if not api_key.get_secret_value().strip():
                    raise RefreshError("DOCS_API_KEY is not set (see docs/DEPLOYMENT.md)")
                if not api_key.get_secret_value().isascii() or any(
                    not 33 <= ord(char) <= 126 for char in api_key.get_secret_value()
                ):
                    raise RefreshError(
                        "DOCS_API_KEY must contain only printable ASCII without spaces"
                    )
                if args.require_monitor and monitor is None:
                    raise RefreshError("SOURCE_REFRESH_MONITOR_URL is required for scheduled runs")
                client = DocsApiClient(base_url=args.base_url, api_key=api_key, client=http)
                client.check_scope(timeout=min(args.timeout, args.max_seconds))
                # Log the ID before admission: even a lost acquire response can
                # leave an owner that an operator must identify for recovery.
                print(f"source_refresh event=acquire run_id={run_id}", flush=True)
                client.acquire_run(run_id)
                guard_acquired = True
                print(f"source_refresh event=start run_id={run_id}", flush=True)
                if monitor:
                    monitor_failed = not monitor.ping("start")
                report = run_refresh(
                    client,
                    batch_size=args.batch_size,
                    max_batches=args.max_batches,
                    timeout=args.timeout,
                    max_seconds=args.max_seconds,
                )
            except RefreshError as exc:
                report.error = str(exc)
            if monitor:
                event = "success" if report.exit_code == 0 and not monitor_failed else "fail"
                monitor_failed = not monitor.ping(event) or monitor_failed
            # Keep ownership through the final ping so a delayed success cannot
            # mask a new run. Uncertain API/monitor outcomes require inspection;
            # never expire or release them automatically, including on crashes.
            if guard_acquired and not report.error and not monitor_failed:
                try:
                    client.release_run(run_id)
                except RefreshError as exc:
                    report.error = str(exc)
                    if monitor:
                        monitor_failed = not monitor.ping("fail")
    except RefreshError as exc:
        report.error = str(exc)
    if monitor_failed:
        report.error = report.error or "monitor delivery failed"
    print(
        f"source_refresh event=finish {report.summary()} exit_code={report.exit_code} "
        f"monitor_failed={str(monitor_failed).lower()} "
        f"duration_seconds={time.monotonic() - started:.3f}",
        flush=True,
    )
    if report.error:
        print(report.error, file=sys.stderr)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
