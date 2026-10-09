"""doc-backfill — find catalogued docs with no stored content and repair them.

Talks to documentation-system over **HTTP**.
The platform rule is that nothing runs inside a source of truth — no
batch jobs reaching into the database (see docs/ARCHITECTURE.md) — and the
repair is already an endpoint: POST /docs/{id}/refetch, which carries the
subtle guards about not wiping stored text on an empty fetch. Going over HTTP
also keeps the connectors/Google credentials inside the service instead of
requiring them wherever this script runs; it needs only an API key.

Usage:

    DOCS_API_KEY=doc_ab12cd34_... uv run doc-backfill [--base-url URL]
                                                      [--source-id web]
                                                      [--dry-run]

The key is read from the environment, never from argv: command-line arguments
are visible to anything that can run `ps`.

Required scopes on the key: `docs:read` (GET /sources), `docs:read:all`
(GET /docs must see every doc, not just one actor's), and `docs:write`
(refetch). `admin` satisfies all three. Note `docs:read:all` does NOT imply
`docs:read` — AuthedKey.has_scope is an exact match plus the `admin` wildcard.
"""

import argparse
import os
import sys
from dataclasses import dataclass, field

import httpx

DEFAULT_BASE_URL = "http://localhost:8001"

# Per-doc outcomes. SKIPPED is deliberately distinct from FAILED: a doc whose
# source has content fetching disabled was never going to have content, and
# reporting it as a failure trains operators to ignore the output.
REPAIRED = "repaired"
FAILED = "failed"
SKIPPED = "skipped"
PLANNED = "planned"  # --dry-run only


class BackfillError(Exception):
    """A fatal problem that stops the whole run (bad key, service unreachable,
    malformed listing). Per-doc problems are recorded as FAILED instead."""


@dataclass(frozen=True)
class DocOutcome:
    doc_id: str
    url: str
    outcome: str
    detail: str = ""

    def line(self) -> str:
        suffix = f"  {self.detail}" if self.detail else ""
        return f"{self.outcome:<8} {self.doc_id}  {self.url}{suffix}"


@dataclass
class Report:
    results: list[DocOutcome] = field(default_factory=list)

    def of(self, outcome: str) -> list[DocOutcome]:
        return [r for r in self.results if r.outcome == outcome]

    @property
    def failed(self) -> list[DocOutcome]:
        return self.of(FAILED)

    def summary(self) -> str:
        parts = [
            f"{len(self.of(name))} {name}"
            for name in (REPAIRED, FAILED, SKIPPED, PLANNED)
            if self.of(name)
        ]
        counted = ", ".join(parts) if parts else "nothing to do"
        return f"{len(self.results)} contentless doc(s): {counted}"


class DocsApiClient:
    """Thin client over the three endpoints a backfill needs. Takes an injected
    httpx.Client so tests can run offline."""

    def __init__(self, *, base_url: str, api_key: str, client: httpx.Client) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-API-Key": api_key}
        self._client = client

    def _get_json(self, path: str, params: dict | None = None) -> list[dict]:
        try:
            resp = self._client.get(
                f"{self._base_url}{path}", headers=self._headers, params=params or {}
            )
        except httpx.HTTPError as e:
            raise BackfillError(f"GET {path} failed: {e}") from e
        if resp.status_code != 200:
            raise BackfillError(f"GET {path} returned {resp.status_code}: {_detail(resp)}")
        body = resp.json()
        if not isinstance(body, list):
            raise BackfillError(f"GET {path} returned {type(body).__name__}, expected a list")
        return body

    def fetch_enabled_source_ids(self) -> set[str]:
        """Source ids the catalog will actually attempt a fetch for. Read from
        the service rather than hardcoding the disabled ones, so enabling a new
        source (a Notion fetcher, say) needs no change here."""
        return {
            s["id"]
            for s in self._get_json("/sources", {"active_only": "true"})
            if s.get("content_fetch_enabled")
        }

    def list_contentless(self, *, source_id: str | None = None) -> list[dict]:
        params: dict[str, str] = {"has_content": "false", "active_only": "true"}
        if source_id is not None:
            params["source_id"] = source_id
        return self._get_json("/docs", params)

    def refetch(self, doc_id: str) -> str:
        """Re-run the content fetch for one doc. Returns "" on success, or a
        short reason on failure — a per-doc problem, never fatal to the run."""
        try:
            resp = self._client.post(
                f"{self._base_url}/docs/{doc_id}/refetch", headers=self._headers
            )
        except httpx.HTTPError as e:
            return f"unreachable: {e}"
        if resp.status_code != 200:
            return f"{resp.status_code}: {_detail(resp)}"
        return ""


def _detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail", ""))[:200]
    except Exception:
        return resp.text[:200]


def run_backfill(
    client: DocsApiClient, *, source_id: str | None = None, dry_run: bool = False
) -> Report:
    """Repair every contentless doc the key can see. Each doc gets its own
    outcome: a failure is recorded and the run continues."""
    enabled = client.fetch_enabled_source_ids()
    report = Report()
    for doc in client.list_contentless(source_id=source_id):
        doc_id, url = str(doc.get("id", "")), str(doc.get("url", ""))
        source = doc.get("source_id")
        if source not in enabled:
            report.results.append(
                DocOutcome(
                    doc_id,
                    url,
                    SKIPPED,
                    f"source {source!r} has content fetching disabled",
                )
            )
            continue
        if dry_run:
            report.results.append(DocOutcome(doc_id, url, PLANNED))
            continue
        reason = client.refetch(doc_id)
        if reason:
            report.results.append(DocOutcome(doc_id, url, FAILED, reason))
        else:
            report.results.append(DocOutcome(doc_id, url, REPAIRED))
    return report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="doc-backfill",
        description=(
            "Find catalogued docs with no stored content and refetch it. "
            "Reads the API key from DOCS_API_KEY."
        ),
    )
    p.add_argument(
        "--base-url",
        default=os.environ.get("DOCS_BASE_URL", DEFAULT_BASE_URL),
        help=f"documentation-system base URL (env DOCS_BASE_URL, default {DEFAULT_BASE_URL})",
    )
    p.add_argument("--source-id", default=None, help="only repair docs of this source kind")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be refetched without making any fetch",
    )
    p.add_argument("--timeout", type=float, default=60.0, help="per-request timeout in seconds")
    return p


def main(argv: list[str] | None = None) -> int:
    """Exit 0 when nothing failed, 1 when any doc failed, 2 on a fatal error.

    A non-zero exit on per-doc failure is the point: a silent success would
    leave an operator believing the corpus is complete."""
    args = build_parser().parse_args(argv)
    api_key = os.environ.get("DOCS_API_KEY")
    if not api_key:
        print("DOCS_API_KEY is not set (see docs/DEPLOYMENT.md)", file=sys.stderr)
        return 2
    try:
        with httpx.Client(timeout=args.timeout) as http:
            client = DocsApiClient(base_url=args.base_url, api_key=api_key, client=http)
            report = run_backfill(client, source_id=args.source_id, dry_run=args.dry_run)
    except BackfillError as e:
        print(str(e), file=sys.stderr)
        return 2
    for result in report.results:
        print(result.line())
    print(report.summary())
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
