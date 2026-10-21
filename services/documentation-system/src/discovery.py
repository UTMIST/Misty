"""Checkpointed HTTP-only Drive discovery. No database or Google credentials.

The checkpoint belongs to one environment/connector identity and one scheduler.
Atomic replacement plus an OS lock make interruption/replay safe on a volume.
"""

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sys
import time
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.exceptions import SettingsError


class DiscoveryError(Exception):
    """Sanitized error safe to print. Never carries an upstream response body."""


class RetryableDiscoveryError(DiscoveryError):
    """Transport/server failure; an import may already have committed remotely."""


MAX_IMPORT_ATTEMPTS = 3


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    discovery_enabled: bool = False
    discovery_state_path: Path = Path("discovery-state.json")
    # Change this whenever the connector identity changes; protects checkpoints.
    discovery_identity: str = Field(default="default", min_length=1)
    docs_base_url: str = "http://localhost:8001"
    docs_api_key: SecretStr = SecretStr("")
    connectors_base_url: str = "http://localhost:8005"
    connectors_api_key: SecretStr = SecretStr("")
    discovery_page_size: int = Field(default=100, ge=1, le=100)
    discovery_max_pages: int = Field(default=20, ge=1, le=1000)
    discovery_max_items: int = Field(default=100, ge=1, le=10000)
    discovery_max_folders: int = Field(default=10000, ge=1, le=100000)
    discovery_max_seconds: float = Field(default=900, gt=0, allow_inf_nan=False)
    discovery_timeout_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)
    discovery_excluded_ids: set[str] = Field(default_factory=set)


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Item(WireModel):
    file_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$", max_length=200)
    kind: Literal["file", "folder", "unsupported", "shortcut", "shared_drive"]
    source_id: Literal["gdocs", "gsheets", "gslides", "gdrive"] | None = None

    @model_validator(mode="after")
    def file_has_source(self):
        if (self.kind == "file") != (self.source_id is not None):
            raise ValueError("source required only for supported files")
        return self


class Page(WireModel):
    items: list[Item] = Field(max_length=100)
    next_page_token: str | None = Field(default=None, min_length=1, max_length=4096)
    incomplete: bool = False


class ImportResult(WireModel):
    status: Literal["indexed", "registered", "index_failed", "existing", "retired", "disabled"]
    doc_id: str | None = None
    warnings: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def valid_document(self):
        if self.status == "disabled":
            if self.doc_id is not None:
                raise ValueError("disabled source cannot identify an import")
        elif self.doc_id is None:
            raise ValueError("import result requires a document id")
        else:
            UUID(self.doc_id)
        return self


class Listing(WireModel):
    folder_id: str | None = None
    page_token: str | None = None


class State(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    identity: str
    frontier: list[Listing] = Field(default_factory=list)
    pending: list[Item] = Field(default_factory=list)
    # Consecutive retryable failures of pending[0], retained across invocations.
    pending_attempts: int = Field(default=0, ge=0)
    seen_folders: set[str] = Field(default_factory=set)
    counts: dict[str, int] = Field(default_factory=dict)
    started_at: str | None = None
    completed_at: str | None = None
    last_counts: dict[str, int] = Field(default_factory=dict)

    def summary(self):
        return {
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "queued_listings": len(self.frontier),
            "pending_items": len(self.pending),
            "counts": self.counts,
            "last_counts": self.last_counts,
        }


def _now():
    return datetime.now(timezone.utc).isoformat()


def _url(value: str) -> str:
    try:
        url = httpx.URL(value)
    except (httpx.InvalidURL, ValueError):
        raise DiscoveryError("invalid service URL") from None
    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
    ):
        raise DiscoveryError("service URLs require HTTP(S), no credentials, query or fragment")
    return str(url).rstrip("/")


class DiscoveryClient:
    def __init__(self, settings: Settings, http: httpx.Client):
        self.docs_url = _url(settings.docs_base_url)
        self.connector_url = _url(settings.connectors_base_url)
        self.settings = settings
        self.http = http
        if not all(
            k.get_secret_value().strip()
            for k in (settings.docs_api_key, settings.connectors_api_key)
        ):
            raise DiscoveryError("DOCS_API_KEY and CONNECTORS_API_KEY are required")

    def request(self, method, path, *, timeout, body=None, connector=False):
        key = self.settings.connectors_api_key if connector else self.settings.docs_api_key
        base = self.connector_url if connector else self.docs_url
        try:
            response = self.http.request(
                method,
                base + path,
                json=body,
                timeout=timeout,
                headers={"X-API-Key": key.get_secret_value()},
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise RetryableDiscoveryError("HTTP request failed; no immediate retry") from None
        # Authentication/configuration errors stop; isolated listing/import errors
        # advance the checkpoint and are revisited on the next full scan.
        if response.status_code != 200:
            if connector and response.status_code in {403, 404}:
                try:
                    detail = response.json().get("detail")
                except (ValueError, AttributeError):
                    detail = None
                if isinstance(detail, str) and detail in {
                    "source denied listing",
                    "source folder not found",
                }:
                    return None
            if response.status_code in {400, 422} or connector and response.status_code == 502:
                return None
            if response.status_code in {408, 429} or response.status_code >= 500:
                raise RetryableDiscoveryError(f"HTTP request returned {response.status_code}")
            raise DiscoveryError(f"HTTP request returned {response.status_code}")
        try:
            result = response.json()
        except ValueError:
            raise DiscoveryError("HTTP response was not JSON") from None
        if not isinstance(result, dict):
            raise DiscoveryError("HTTP response was not an object")
        return result

    def check_scope(self, timeout):
        body = self.request("GET", "/api-keys/self", timeout=timeout)
        scopes = body.get("scopes") if isinstance(body, dict) else None
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            raise DiscoveryError("catalog returned invalid scopes")
        if "docs:write" not in scopes and "admin" not in scopes:
            raise DiscoveryError("DOCS_API_KEY requires docs:write")

    def list_page(self, listing, timeout):
        body = self.request(
            "POST",
            "/discover/google",
            connector=True,
            timeout=timeout,
            body={**listing.model_dump(), "page_size": self.settings.discovery_page_size},
        )
        if body is None:
            return None
        try:
            page = Page.model_validate(body)
            if len(page.items) > self.settings.discovery_page_size:
                raise ValueError()
            if page.next_page_token and page.next_page_token == listing.page_token:
                raise ValueError()
            return page
        except (ValidationError, ValueError):
            raise DiscoveryError("connector returned invalid discovery page") from None

    def import_file(self, item, timeout):
        body = self.request(
            "POST",
            "/docs/discovery",
            timeout=timeout,
            body={"file_id": item.file_id, "source_id": item.source_id},
        )
        if body is None:
            return None
        try:
            return ImportResult.model_validate(body)
        except ValidationError:
            raise DiscoveryError("catalog returned invalid discovery result") from None


def save_state(path: Path, state: State):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as handle:
        os.chmod(temporary, 0o600)
        handle.write(state.model_dump_json())
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


@contextmanager
def checkpoint(settings: Settings):
    path = settings.discovery_state_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DiscoveryError("another discovery run holds the checkpoint lock") from None
        try:
            state = (
                State.model_validate_json(path.read_text())
                if path.exists()
                else State(identity=settings.discovery_identity)
            )
        except (ValueError, ValidationError):
            raise DiscoveryError("invalid checkpoint; inspect before resetting") from None
        if state.identity != settings.discovery_identity:
            raise DiscoveryError("checkpoint identity changed; use a separate state path")
        yield state, lambda: save_state(path, state)


def run_discovery(client, settings: Settings, state: State, save) -> int:
    """Bound new requests, checkpoint each result, never refresh existing docs.

    Exit 0: full scan without failures. 1: full scan with item/listing gaps.
    2: interrupted/budget-limited; next invocation continues this checkpoint.
    A completed scan starts again on the next scheduled invocation.
    """
    deadline = time.monotonic() + settings.discovery_max_seconds

    def timeout():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DiscoveryError("time budget reached; checkpoint retained")
        return min(settings.discovery_timeout_seconds, remaining)

    client.check_scope(timeout())
    if not state.frontier and not state.pending:
        state.frontier = [Listing()]
        state.pending_attempts = 0
        state.seen_folders = set()
        state.counts = {}
        state.started_at = _now()
        save()
    counts = Counter(state.counts)
    pages = items = 0

    def persist():
        state.counts = dict(counts)
        save()

    while state.frontier or state.pending:
        timeout()
        if state.pending:
            if items >= settings.discovery_max_items:
                return 2
            item = state.pending[0]
            if item.file_id in settings.discovery_excluded_ids:
                counts["excluded"] += 1
            elif item.kind == "folder":
                if item.file_id not in state.seen_folders:
                    if len(state.seen_folders) >= settings.discovery_max_folders:
                        raise DiscoveryError("folder bound reached; checkpoint retained")
                    state.seen_folders.add(item.file_id)
                    state.frontier.append(Listing(folder_id=item.file_id))
                    counts["folders"] += 1
            elif item.kind != "file":
                counts[item.kind] += 1
            else:
                try:
                    result = client.import_file(item, timeout())
                except RetryableDiscoveryError:
                    state.pending_attempts += 1
                    if state.pending_attempts < MAX_IMPORT_ATTEMPTS:
                        counts["import_retries"] += 1
                        persist()
                        # Do not immediately replay an ambiguous POST. The next
                        # invocation rechecks catalog identity before paid work.
                        raise
                    result = None
                counts[result.status if result else "import_failed"] += 1
                if result:
                    counts["warnings"] += result.warnings
            state.pending.pop(0)
            state.pending_attempts = 0
            items += 1
            persist()
        else:
            if pages >= settings.discovery_max_pages:
                return 2
            listing = state.frontier[0]
            page = client.list_page(listing, timeout())
            pages += 1
            counts["pages"] += 1
            if page is None:
                counts["listing_failed"] += 1
                state.frontier.pop(0)
            else:
                counts["incomplete_pages"] += page.incomplete
                state.pending = list(page.items)
                if page.next_page_token:
                    listing.page_token = page.next_page_token
                else:
                    state.frontier.pop(0)
            persist()
    state.completed_at = _now()
    state.last_counts = dict(counts)
    persist()
    return (
        1
        if any(
            counts[k]
            for k in (
                "registered",
                "index_failed",
                "import_failed",
                "listing_failed",
                "incomplete_pages",
            )
        )
        else 0
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="print sanitized checkpoint counts")
    args = parser.parse_args(argv)
    try:
        settings = Settings()
        if not settings.discovery_enabled and not args.status:
            print("discovery disabled")
            return 0
        with checkpoint(settings) as (state, save):
            if args.status:
                print(json.dumps({"enabled": settings.discovery_enabled, **state.summary()}))
                return 0
            with httpx.Client() as http:
                code = run_discovery(DiscoveryClient(settings, http), settings, state, save)
            print(json.dumps({"exit_code": code, **state.summary()}))
            return code
    except (ValidationError, SettingsError, OSError):
        print("invalid discovery configuration or checkpoint storage", file=sys.stderr)
        return 2
    except DiscoveryError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
