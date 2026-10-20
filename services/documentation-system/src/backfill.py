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
The run checks its own scopes against GET /api-keys/self before trusting any
listing, because a key without `docs:read:all` is not rejected: `read_context`
resolves it to DENY and the catalog lists as `200 []` — an authorization
answer that would otherwise be read as "nothing to repair".
"""

import argparse
import os
import sys
from dataclasses import dataclass, field, replace

import httpx

DEFAULT_BASE_URL = "http://localhost:8001"

# Per-doc outcomes. SKIPPED is deliberately distinct from FAILED: a doc whose
# source has content fetching disabled was never going to have content, and
# reporting it as a failure trains operators to ignore the output. EMPTY is
# likewise distinct: the refetch itself worked, the source just yielded no
# text to store.
REPAIRED = "repaired"
EMPTY = "empty"
FAILED = "failed"
SKIPPED = "skipped"
PLANNED = "planned"  # --dry-run only

NO_CONTENT_DETAIL = "refetch succeeded but no content was extracted"

ADMIN_SCOPE = "admin"
READ_SCOPE = "docs:read"  # GET /sources
READ_ALL_SCOPE = "docs:read:all"  # GET /docs catalog-wide
WRITE_SCOPE = "docs:write"  # POST /docs/{id}/refetch


class BackfillError(Exception):
    """A fatal problem that stops the whole run (bad key, mis-scoped key,
    service unreachable, malformed listing). Per-doc problems are recorded as
    FAILED."""


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

    @property
    def unrepaired(self) -> list[DocOutcome]:
        """Docs the run was supposed to repair and didn't — whether the refetch
        errored or returned nothing to store. Both exit non-zero: an operator
        who reads exit 0 as "the corpus is complete" must not be wrong."""
        return self.of(FAILED) + self.of(EMPTY)

    def summary(self) -> str:
        parts = [
            f"{len(self.of(name))} {name}"
            for name in (REPAIRED, EMPTY, FAILED, SKIPPED, PLANNED)
            if self.of(name)
        ]
        counted = ", ".join(parts) if parts else "nothing to do"
        return f"{len(self.results)} contentless doc(s): {counted}"


class DocsApiClient:
    """Thin client over the endpoints a backfill needs. Takes an injected
    httpx.Client so tests can run offline."""

    def __init__(self, *, base_url: str, api_key: str, client: httpx.Client) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-API-Key": api_key}
        self._client = client

    def _get(self, path: str, params: dict | None = None):
        try:
            resp = self._client.get(
                f"{self._base_url}{path}", headers=self._headers, params=params or {}
            )
        except httpx.HTTPError as e:
            raise BackfillError(f"GET {path} failed: {e}") from e
        if resp.status_code != 200:
            raise BackfillError(f"GET {path} returned {resp.status_code}: {_detail(resp)}")
        return resp.json()

    def _get_json(self, path: str, params: dict | None = None) -> list[dict]:
        body = self._get(path, params)
        if not isinstance(body, list):
            raise BackfillError(f"GET {path} returned {type(body).__name__}, expected a list")
        return body

    def self_scopes(self) -> set[str]:
        """The scopes on the key this run is using, from GET /api-keys/self —
        the only endpoint that answers an authorization question with an
        authorization answer rather than with filtered data."""
        body = self._get("/api-keys/self")
        scopes = body.get("scopes") if isinstance(body, dict) else None
        if not isinstance(scopes, list):
            raise BackfillError("GET /api-keys/self returned no scope list")
        return {str(s) for s in scopes}

    def fetch_enabled_source_ids(self) -> set[str]:
        """Source ids the catalog will actually attempt a fetch for. Read from
        the service rather than hardcoding the disabled ones, so enabling a new
        source (a YouTube fetcher, say) needs no change here."""
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
        """Re-run the content fetch for one doc. Returns "" when the request
        succeeded, or a short reason on failure — a per-doc problem, never
        fatal to the run.

        A 200 means the refetch ran, NOT that content was stored: the endpoint
        only writes a doc_content row `if result.content`, so a title-only
        fetcher (github) or an empty extraction leaves the doc contentless and
        still reports success. `_verify_repairs` settles that, not this."""
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


def _missing_scopes(scopes: set[str], *, dry_run: bool) -> list[str]:
    if ADMIN_SCOPE in scopes:
        return []
    needed = [READ_SCOPE, READ_ALL_SCOPE] + ([] if dry_run else [WRITE_SCOPE])
    return [s for s in needed if s not in scopes]


def _check_scopes(client: DocsApiClient, *, dry_run: bool) -> None:
    """Refuse to run on a key that cannot see the whole catalog.

    Without `docs:read:all` and with no X-On-Behalf-Of header (this tool never
    sends one), `read_context` resolves to DENY and GET /docs answers 200 with
    an empty list rather than 403. Discovery would then "succeed" and the run
    would report nothing to repair while the gap it exists to close sits
    untouched. Checked before any listing, so a mis-scoped key is a hard error
    rather than an empty run."""
    missing = _missing_scopes(client.self_scopes(), dry_run=dry_run)
    if not missing:
        return
    raise BackfillError(
        f"the API key is missing scope(s): {', '.join(missing)} — issue one with "
        "doc-keys issue --scopes docs:read docs:read:all docs:write"
    )


def _verify_repairs(client: DocsApiClient, report: Report, *, source_id: str | None) -> None:
    """Demote any "repaired" doc that is still contentless.

    A 200 from POST /docs/{id}/refetch means the fetch ran, not that content
    was stored — the endpoint only writes `if result.content`. GithubFetcher is
    title-only by design, so those docs would otherwise report `repaired` and
    exit 0 on every run, forever. One listing settles the whole run: the
    contentless set is exactly the question."""
    repaired = {r.doc_id for r in report.results if r.outcome == REPAIRED}
    if not repaired:
        return
    still_contentless = repaired & {
        str(doc.get("id", "")) for doc in client.list_contentless(source_id=source_id)
    }
    if not still_contentless:
        return
    report.results = [
        replace(r, outcome=EMPTY, detail=NO_CONTENT_DETAIL)
        if r.outcome == REPAIRED and r.doc_id in still_contentless
        else r
        for r in report.results
    ]


def run_backfill(
    client: DocsApiClient, *, source_id: str | None = None, dry_run: bool = False
) -> Report:
    """Repair every contentless doc the key can see. Each doc gets its own
    outcome: a failure is recorded and the run continues."""
    _check_scopes(client, dry_run=dry_run)
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
    if not dry_run:
        _verify_repairs(client, report, source_id=source_id)
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
    """Exit 0 when everything contentless was repaired, 1 when any doc was not
    (it failed, or the fetch yielded nothing to store), 2 on a fatal error.

    A non-zero exit on an unrepaired doc is the point: a silent success would
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
    return 1 if report.unrepaired else 0


if __name__ == "__main__":
    sys.exit(main())
