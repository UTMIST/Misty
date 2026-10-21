# Discovery implementation review

Review scope: issue [#291](https://github.com/UTMIST/Misty/issues/291), local implementation
and offline verification. This is a self-review, not independent approval or staging
acceptance. Production activation remains outside this change.

## Findings and disposition

No blocking code findings remain from this review. The review checked the HTTP/Protocol
boundaries, identity deduplication, source-derived visibility, pagination, checkpoint
replay, scope/validation ordering, failure isolation and operational documentation.

Corrections made during implementation/review:

- Alias lookup covers old manual Google URLs; new ingests use one canonical identity
  key. Both storage adapters exercise the same cases against real Postgres and memory.
- Discovery skips retired records, including a registration/retirement race observed
  between the route's initial lookup and ingest's lookup.
- A concurrent registration is inspected before being called `existing`; missing
  content/indexing cannot masquerade as a completed index.
- Successful JSON `null`, malformed page/import results, missing document ids and
  malformed settings stop safely instead of silently advancing a checkpoint.
- Listing pages are copied into checkpoint state rather than consumed from the provider
  result object; repeat-scan/retry regressions exercise reused fake pages.
- Missing, repeated or over-budget ACL pages fail closed before extraction. Discovery
  metadata never becomes a grant.

## Verification

Executed locally with offline fake Drive, directory, embedding and HTTP providers:

- Complete connectors pytest suite.
- Complete documentation-system pytest suite with `RUN_PG_TESTS=1` and
  `RUN_PG_MIGRATION_TESTS=1` against a disposable `pgvector/pgvector:0.8.2-pg16`
  container, after `python -m alembic upgrade head`.
- New HTTP runner → catalog ingest/index integration with direct and nested shares,
  later descendants, repeat scans and person-scoped chunk visibility; actual `/search`
  authorization checked separately with an offline query embedder.
- Pinned `uv run --no-sync ruff check` and `ruff format --check` for both affected
  Python services, plus `git diff --check`.
- Built documentation-system/auth wheels, installed into a fresh virtual environment
  outside the checkout, imported the API/contracts/runner and ran `doc-discover --help`.

The test launcher prepended this worktree's service directory and asserted the imported
`src` and `contracts` file paths before collection. Console-script Alembic resolved
another workspace member initially; `python -m alembic` from the target service corrected
that source-path collision. The connector suite emits the existing Starlette/httpx
TestClient deprecation warning. No real Google, AWS or LLM requests were used.

## Remaining release gates and limits

Live Drive topology, actual scheduled latency, checkpoint-volume behavior on Railway,
alert routing, and authorized Discord search/answer acceptance are **not verified**.
Complete the [staging checklist](DISCOVERY.md#controlled-staging-acceptance--pending),
including #292 refresh and #278 answers, before claiming the issue's release gate met.
No service, key, cron, alert, or production activation was provisioned by this change.

Shared Drives and shortcuts are explicitly skipped. Legacy alias lookup may scan
catalog URL expressions; measure growth before adding a persisted/indexed identity
column. Existing duplicate catalog rows are preserved, with deterministic lookup;
this change does not merge grants or data. The checkpoint requires exactly one
scheduler/volume per environment and connector identity. HTTP timeout does not cancel
server work; retries are deferred to the next invocation and deduplicated.

## File rationale

The implementation deliberately spans connectors, documentation-system, and root
operating guidance. Connectors owns Google enumeration; the catalog owns identity,
ACL reconciliation and indexing; root docs must describe the new deployment and
agent-facing contract. No unrelated service implementation is changed.

Paths below are relative to the repository root. Every changed/new file has a purpose:

| File | Why it changes |
|---|---|
| `AGENTS.md` | Distinguish strict discovery DTOs and document discovery/refresh ownership. |
| `docs/RAILWAY-DEPLOYMENT.md` | Point platform operators to the optional cron, keys, volume and rollout gate. |
| `services/connectors/contracts/discovery.py` | Define strict, bounded metadata request/response wire models. |
| `services/connectors/src/sources/base.py` | Declare the injectable discovery Protocol. |
| `services/connectors/src/sources/google.py` | Enumerate direct shares/children using existing MIME coverage; bound ACL reads. |
| `services/connectors/src/api/deps.py` | Wire discovery to the configured Google identity. |
| `services/connectors/src/api/routers/discovery.py` | Enforce discovery scope, map normalized errors and return metadata only. |
| `services/connectors/src/api/app.py` | Register the new route. |
| `services/connectors/tests/test_discovery.py` | Verify enumeration, pagination, formats, failures, scopes and ACL completeness offline. |
| `services/connectors/README.md` | Link discovery and correct the complete-ACL sharing prerequisite. |
| `services/connectors/docs/API.md` | Document enumeration, strict input, response categories and ACL bounds. |
| `services/connectors/docs/ARCHITECTURE.md` | Explain metadata discovery's provider boundary and stateless ownership. |
| `services/connectors/docs/DEPLOYMENT.md` | Document the consumer scope and complete-ACL sharing prerequisite. |
| `services/documentation-system/contracts/storage.py` | Specify deterministic Google identity lookup for both adapters. |
| `services/documentation-system/src/google_identity.py` | Centralize supported alias extraction for Python/Postgres. |
| `services/documentation-system/src/url_norm.py` | Give new Google aliases one active-URL dedup key. |
| `services/documentation-system/src/storage/in_memory.py` | Implement identity lookup in the fast adapter. |
| `services/documentation-system/src/storage/postgres.py` | Implement equivalent legacy/manual identity lookup in production storage. |
| `services/documentation-system/src/ingest.py` | Reuse identity lookup and permit discovery to skip existing/retired provider work. |
| `services/documentation-system/src/api/routers/discovery.py` | Import through existing ingest/index paths with truthful status and no claimed grants. |
| `services/documentation-system/src/api/app.py` | Register discovery before the document-id catch-all. |
| `services/documentation-system/src/discovery.py` | Provide bounded, resumable HTTP traversal and safe operator status. |
| `services/documentation-system/pyproject.toml` | Install the CLI and include contracts needed by the installed API. |
| `services/documentation-system/railway.discovery.json` | Define the optional dedicated cron without migrations/restarts/healthcheck. |
| `services/documentation-system/.env.example` | Expose runner-only settings with disabled local defaults. |
| `services/documentation-system/.gitignore` | Keep local checkpoint ids/tokens and lock/temp files out of Git. |
| `services/documentation-system/tests/google_identity_cases.py` | Share alias/retirement/non-alias behavior between adapters. |
| `services/documentation-system/tests/test_in_memory_adapter.py` | Apply identity cases to memory storage. |
| `services/documentation-system/tests/test_postgres_adapter.py` | Apply identical cases to real Postgres. |
| `services/documentation-system/tests/test_api_discovery.py` | Verify authenticated ingestion, visibility, races, recovery and HTTP integration. |
| `services/documentation-system/tests/test_discovery.py` | Verify checkpoints, budgets, retries, recurrence, HTTP validation and sanitized output. |
| `services/documentation-system/README.md` | Describe member flow, endpoint and runner configuration. |
| `services/documentation-system/docs/API.md` | Document import validation, statuses and recovery ownership. |
| `services/documentation-system/docs/ARCHITECTURE.md` | Explain identity, persistence and discovery/refresh tradeoffs. |
| `services/documentation-system/docs/DEPLOYMENT.md` | Link the dedicated discovery deployment and staging gate. |
| `services/documentation-system/docs/DISCOVERY.md` | Supply member/operator instructions, coverage, lifecycle, recovery and acceptance checklist. |
| `services/documentation-system/docs/DISCOVERY-REVIEW.md` | Record this review, actual verification, limitations and per-file rationale. |
