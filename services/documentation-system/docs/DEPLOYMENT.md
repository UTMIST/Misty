# documentation-system — operational reference

Runtime operations for the documentation-system service: env-var contract,
API key management (`doc-keys` CLI), fetcher configuration, and the
team-tracking dependency.

**Infrastructure** — how the service actually runs in staging + production —
lives in the platform-wide runbook:

- **[`docs/RAILWAY-DEPLOYMENT.md`](../../../docs/RAILWAY-DEPLOYMENT.md)** — Railway + Neon setup and the branching/auto-deploy flow.
- **[`docs/DEPLOYMENT-HISTORY.md`](../../../docs/DEPLOYMENT-HISTORY.md)** — how we got there and the bugs we hit.

For local dev, the [README quickstart](../README.md) is enough — this file is
about the service in a real (staging / production) environment.

## What this service depends on

- **Postgres 16 with pgvector.** Managed via Neon in staging/production (see
  [Postgres](#postgres)); local development and CI use
  `pgvector/pgvector:0.8.2-pg16`. Installing the Python `pgvector` dependency
  alone does not install the database extension.
- **A reachable team-tracking directory** — the source of truth for owners.
  See the directory-dependency section below; the service *runs* without it
  but ownership validation degrades.
- **A reachable connectors service** — fetches content for the Google
  sources. See "The connectors dependency" below: it is a soft dependency
  both at request time and at *boot* — a connectors outage or a missing
  `CONNECTORS_API_KEY` degrades Google-source fetches but never blocks
  startup (see `verify_production_secrets()` below).
- **A reachable llm service** — embeds stored content so it can be searched.
  Soft as well: see "The llm (embedding) dependency" below. Without it docs
  are catalogued and their content stored, just not indexed.

## Environment variables

All configuration is env-driven (`src/config.py`, loaded from the process environment or a
`.env` file):

| Env var | Setting | Default | Purpose |
|---------|---------|---------|---------|
| `DATABASE_URL` | `database_url` | `postgresql+psycopg://docs:dev_password@localhost:5434/docs` | Postgres connection (SQLAlchemy + psycopg driver) |
| `API_KEY` | `api_key` | `dev-api-key-change-me` | Bootstrap admin key. **Change or unset in prod.** |
| `DIRECTORY_BASE_URL` | `directory_base_url` | `http://localhost:8000` | Base URL of the team-tracking directory |
| `DIRECTORY_API_KEY` | `directory_api_key` | `dev-api-key-change-me` | API key this service uses to call the directory |
| `CONNECTORS_BASE_URL` | `connectors_base_url` | `http://localhost:8005` | Base URL of the connectors service (fetches Google source content) |
| `CONNECTORS_API_KEY` | `connectors_api_key` | `dev-api-key-change-me` | API key this service uses to call connectors. **Should be overridden outside `local`**, but it's a soft dependency — `verify_production_secrets()` only logs a startup warning on the dev default, it does not refuse to boot (see [`src/config.py`](../src/config.py)). |
| `LLM_BASE_URL` | `llm_base_url` | `http://localhost:8002` | Platform LLM `/embed` endpoint host. |
| `LLM_API_KEY` | `llm_api_key` | empty | Secret `embed`-scoped LLM key; missing key keeps catalog operations available but search returns 503. |
| `SEARCH_EMBED_MODEL` | `search_embed_model` | `openai-embed-3-small` | Query model alias; also supports `openai-embed-3-large`, both at 1536 dimensions. |
| `SEARCH_MIN_COSINE` | `search_min_cosine` | `0.3` | Absolute vector floor in `(0,1]`; complete keyword matches qualify independently. |
| `EMBED_MODEL` | `embed_model` | unset | Optional indexing model alias; unset uses `SEARCH_EMBED_MODEL`. An override must match the search model before its chunks can be retrieved. Changing models requires re-embedding (#209). |
| `EMBED_MAX_REQUEST_CHARS` | `embed_max_request_chars` | `400000` | Characters per indexing `/embed` request. Keep at or below the llm service's own limit. |
| `SOURCE_ACCESS_REFRESH_AFTER_HOURS` | `source_access_refresh_after_hours` | `24` | A Google doc becomes due for ACL/content refresh after this interval. |
| `SOURCE_ACCESS_MAX_AGE_HOURS` | `source_access_max_age_hours` | `48` | Source-derived grants expire and stop authorizing reads after this interval without a complete successful sync. Must exceed the refresh interval. |

The directory key needs read-only scopes `people:read`, `teams:read`, `memberships:read`,
`identifiers:read`, and `channels:read`. The provisioning script issues these; no directory write
scope is required.

Staging + production values live in **Railway** — set per environment via the
runbook's env-var contract. Keep the defaults ONLY for local dev; anything
running against a real Neon branch must set a strong `API_KEY` and its own
scoped `DIRECTORY_API_KEY`, and should set its own scoped `CONNECTORS_API_KEY`
(issued by the provisioning script / connectors' `connectors-keys` CLI — see
the runbook) to enable Google-source content.

> **A staging or production deploy that has not set `API_KEY` or
> `DIRECTORY_API_KEY` will refuse to boot.** `verify_production_secrets()`
> treats those two as hard requirements once `docs_env` is `staging`/`production`.
> `CONNECTORS_API_KEY` is different: a default there only logs a startup
> warning — the service still boots, it just can't fetch Google-source
> content (see "The connectors dependency" below).

## Authorized retrieval

Provision the documentation-system directory key again to add `channels:read`;
keep its existing read scopes for person access and source reconciliation.
Consumers need `docs:read` and literal `act-as-user` for person/DM searches,
or literal `act-as-channel` for channel searches. Neither literal scope is
implied by admin. Issue the minimum appropriate key, for example:

```bash
uv --project services/documentation-system run doc-keys issue --name member-search --scopes docs:read act-as-user
uv --project services/documentation-system run doc-keys issue --name helper-search --scopes docs:read act-as-user act-as-channel
uv --project services/llm run llm-keys --name documentation-system --scopes embed
```

Run from the repository root. Append the last key to LLM's `CONSUMER_KEYS` and
set it as this service's `LLM_API_KEY`; set `LLM_BASE_URL` to that environment's
private LLM URL. All actual provider credentials stay in the LLM service.
`LLM_API_KEY` is deliberately a soft boot dependency: a missing value logs a
warning outside local mode and search returns 503 before any outbound call.
Catalog operations remain available during rollout. A bad nonempty key yields
a safe embedding dependency error; no keyword-only fallback is enabled.

Seed/index chunks with the resolved model matching `SEARCH_EMBED_MODEL`:
`openai-embed-3-small` -> `text-embedding-3-small`, or
`openai-embed-3-large` -> `text-embedding-3-large`. Both use 1536 dimensions.
Switching the query model hides incompatible old chunks from both branches
until reindexing; it never compares them or silently falls back. The absolute
relevance floor starts at 0.3: against that harness's fixture the
cosine distributions of correct and incorrect documents overlap across their whole
range, so no threshold separates them and moving this value trades recall against
precision. That measurement uses a lexical stand-in embedder, so re-measure against
real embeddings before changing it. See docs/EVALUATION.md.

No migration or unfiltered-search feature flag is needed. Use the shared
[contract fixture](API.md#authorized-retrieval-post-search) for consumer mocks;
the [#277 integration verification](INGESTION-RETRIEVAL-AUDIT.md) also ingests its
documents through the real indexing lifecycle before executing those exchanges.
That runbook records reproducible offline/Postgres checks and the remaining
controlled staging/provider acceptance. Real bot/DM/channel integration (#278)
remains a separate release gate.

## Postgres

- **Local:** `docker compose up -d postgres` inside `services/documentation-system/`
  spins up the dev DB (host port `5434`, chosen so it doesn't collide with
  team-tracking's `5433`). Connection string uses the `psycopg` (v3) driver:
  `postgresql+psycopg://<user>:<pass>@<host>:<port>/<db>`.
- **Staging / production:** Neon manages this. Each environment points at its
  own branch of the `documentation-system` Neon project. See the runbook.

Migration `008` runs `CREATE EXTENSION IF NOT EXISTS vector` in this service's
database. Before rollout, check the target Neon branch's available extensions:

```sql
SELECT name, default_version, installed_version
FROM pg_available_extensions WHERE name = 'vector';
```

The migration role must be able to enable that extension, or an administrator must
enable it before migration. The migration role does not need to own a preinstalled
extension. Local databases already using Postgres 16 can retain their Compose volume
when switching to the pgvector image; recreate the container with
`docker compose up -d postgres` before migrating.
No new application environment variable or embedding credential is needed for storage.

## Migrations (Alembic)

Schema is managed by Alembic (`migrations/`). Alembic reads the same
`DATABASE_URL` the app uses — `migrations/env.py` pulls it from
`src.config.get_settings()`, one source of truth.

Migration history:

- **`001_initial_schema`** — creates `sources`, `docs`, `doc_tags`, `api_keys`
  and their indexes.
- **`002_seed_sources`** — seeds the initial built-in sources; migration `009`
  removes the retired source, leaving seven.
- **`003_doc_grants`** — adds the `doc_grants` table behind per-doc visibility,
  with the grantee-shape CHECK and both uniqueness constraints (including the
  partial index that catches duplicate `org` grants, which the plain unique
  constraint can't because `grantee_id` is NULL there).
- **`004_docs_url_unique_active`** — enforces the dedup invariant at the DB level
  with a partial unique index on `url_normalized WHERE active`.
- **`005_doc_content`** — stores full extracted text, its content hash, and capture time.
- **`006_enable_google_content_fetch`** — enables connector-backed Google fetches.
- **`007_source_document_access`** — records source-grant provenance/expiry and each
  document's last complete source-access sync.
- **`008_doc_chunks`** — enables pgvector, creates 1536-dimensional chunk storage,
  and adds generated keyword metadata with a GIN index. Vector scans remain exact;
  see the [storage contract and index decision](ARCHITECTURE.md#doc_chunks).

- **`009_remove_retired_source`** — retires the source registry entry while keeping
  its documents as web links; see [registry cleanup](#registry-cleanup-migration-009).
- **`010_chunk_title_hash`** — adds nullable title provenance to chunks. Apply before
  running the updated application. Existing vectors remain readable, but their unknown
  title provenance makes them eligible for `POST /docs/index` with its default body.
  Drain that endpoint once per environment to refresh existing chunks; this spends
  embedding budget. The migration itself makes no provider calls. Downgrading to `009`
  removes only the title fingerprint and preserves vectors, text, and citation offsets.
- **`011_source_refresh_run`** — adds the singleton refresh-runner guard. Apply before
  starting the new runner; stop runners before downgrading. See the
  [refresh recovery procedure](#recovery-credential-rotation-and-disablerollback).

Downgrading `008` removes the chunk table/index, preserving catalog records and full
content. The database-level `vector` extension remains installed, even if `008`
enabled it: it may belong to another role or support unrelated tables. Extension
removal is an administrator's separate decision after checking all dependencies.
Re-upgrading restores an empty chunk table; rebuild it by draining `POST /docs/index`
(see "The llm (embedding) dependency"). This migration does not call `/embed`.

> **`004` is a data migration, not just a schema one.** It first collapses any
> pre-existing duplicate active rows for the same `url_normalized` (keeping the
> earliest by `created_at`, id as tiebreak) and only *then* creates the index —
> creating the index first would fail on dirty data. Expect it to modify rows,
> and take a Neon branch snapshot before running it on an environment whose data
> you care about.

**On Railway,** migrations run automatically via `railway.json`'s
`preDeployCommand: "alembic upgrade head"` before every deploy — idempotent,
transactional, no manual step. **Locally,** run `uv run alembic upgrade head`
after cloning or when new migrations land.

## Running the server locally

```bash
uv sync --extra dev
uv run uvicorn src.api.app:app --host 0.0.0.0 --port 8001
```

The service listens on **8001** locally by convention (team-tracking uses
`8000`). In Railway, `PORT` is set to `8000` explicitly and the start command
in `railway.json` picks that up.

Health/observability: the audit middleware emits one JSON log line per
request to stdout. On Railway, view via `railway service logs --service
documentation-system --environment <env>` or the Railway dashboard.

Railway probes `GET /health/ready` before promoting a deployment. This route
checks this service's own Postgres connection and returns `503` on failure;
it does not probe team-tracking or connectors. `GET /health` remains a
DB-independent liveness check. Neither route needs an API key; response
bodies are documented in [API.md](API.md).

## Issuing API keys (`doc-keys` CLI)

Consumers authenticate with scoped keys issued via the `doc-keys` CLI, which talks
**directly to the database** (`DATABASE_URL` must be set/reachable from wherever you run
it). See [`docs/API.md`](API.md) for how consumers present the key.

```bash
# Issue the Discord bot's key: read+write for /doc add, plus literal act-as-user
# so /doc list and /doc search run as the asking member.
# The plaintext key is printed to STDOUT exactly once — capture it now.
uv run doc-keys issue --name discord-bot --scopes docs:read docs:write act-as-user

# Issue a read-only key for a dashboard.
uv run doc-keys issue --name dashboard --scopes docs:read

# Issue a key for an operator or job that retries/re-runs indexing (POST /docs/index).
uv run doc-keys issue --name index-admin --scopes docs:index

# List keys (metadata only — never plaintext).
uv run doc-keys list --active-only

# Revoke a compromised key by its id.
uv run doc-keys revoke <api_key_id>
```

## Repairing docs missing content (`doc-backfill` CLI)

A source fetch that fails during ingest is only a warning: the doc is catalogued, but no
content is stored. Re-ingesting the same URL does not repair it either — the dedup path
merges tags and returns before the fetch runs. Records predating content storage are in
the same state.

`doc-backfill` finds those docs and refetches them. Unlike `doc-keys` it talks to this
service **over HTTP**, not to the database: the repair is already an endpoint
(`POST /docs/{id}/refetch`), and going through it keeps the connectors/Google credentials
inside the service instead of requiring them wherever the script runs.

```bash
# Discover only — lists what would be refetched, makes no fetch.
DOCS_API_KEY=doc_<prefix>_<secret> uv --project services/documentation-system \
  run doc-backfill --dry-run

# Repair everything contentless.
DOCS_API_KEY=doc_<prefix>_<secret> uv --project services/documentation-system \
  run doc-backfill

# Narrow to one source kind.
DOCS_API_KEY=... uv --project services/documentation-system run doc-backfill --source-id gdocs
```

### Authorization

Issue the key with **three** scopes (or `admin`):

```bash
uv --project services/documentation-system run doc-keys \
  issue --name content-backfill --scopes docs:read docs:read:all docs:write
```

| Scope | Needed for |
|-------|-----------|
| `docs:read` | `GET /sources` — which sources have fetching enabled |
| `docs:read:all` | `GET /docs?has_content=false` must see every doc, not one actor's |
| `docs:write` | `POST /docs/{id}/refetch` (not needed for `--dry-run`) |

`docs:read:all` does **not** imply `docs:read` — scope checks are an exact match plus the
`admin` wildcard. A key holding only `docs:read:all` gets a 403 on `GET /sources`.

The run reads its own scopes from `GET /api-keys/self` and refuses before listing
anything if one is missing. That check is not belt-and-braces: a key with `docs:read`
but not `docs:read:all`, sent without `X-On-Behalf-Of` (this tool never sends one),
resolves to `DENY` rather than a 403 — `GET /docs` answers **200 `[]`**, which is
indistinguishable from an empty catalog. Without the check, a mis-scoped key would
print `nothing to do` and exit 0 with the gap untouched.

### Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `DOCS_API_KEY` | *(required)* | Scoped key, read from the environment only |
| `DOCS_BASE_URL` | `http://localhost:8001` | Service base URL; `--base-url` overrides it |

Both are read by the CLI alone — they are not in `Settings` and the service never reads
them, so they are absent from `verify_production_secrets()`. The key is deliberately not
a command-line flag: argv is visible to anything that can run `ps`.

### Reading the output

Each doc gets its own line, then a summary:

```
repaired 4f3a1b2c-...  https://docs.google.com/document/d/abc
empty    6c5d4e3f-...  https://github.com/owner/repo  refetch succeeded but no content was extracted
failed   8b2c7d14-...  https://utmist.ca/handbook  502: connectors unreachable
skipped  1a2b3c4d-...  https://youtube.com/watch?v=page  source 'youtube' has content fetching disabled
4 contentless doc(s): 1 repaired, 1 empty, 1 failed, 1 skipped
```

- **repaired** — content is now stored. Confirmed, not assumed: after the refetches the
  run re-lists `has_content=false` once and demotes anything still in it.
- **empty** — the refetch *worked* and produced no text to store. `POST /docs/{id}/refetch`
  returns 200 whenever the fetch ran, and only writes content `if result.content`, so a
  title-only fetcher (`github`, by design) or a page that extracts to nothing leaves the
  doc contentless behind a 200. Reported apart from **failed** because nothing is broken
  — but it still exits non-zero, because the doc is still missing content.
- **failed** — the source still fails. The doc stays contentless and is named with its
  reason; one failure never aborts the rest of the run.
- **skipped** — the source has `content_fetch_enabled=false` (`youtube`), so it
  could never have content. Bucketed apart from failures so a permanent, expected gap
  does not read as an error every run. The enabled set is read from `GET /sources`, so
  registering a fetcher for a new source needs no change to the CLI.

Exit codes: **0** every contentless doc now has content, **1** at least one does not
(**failed** or **empty**), **2** fatal (missing key, mis-scoped key, service unreachable,
or a verification listing that could not be read). A mis-scoped key is a hard error
rather than an empty run, so it can never be mistaken for "nothing to repair"; likewise
exit 0 means the corpus is complete, not merely that every request returned 200.

---

Notes:

- Scopes are `docs:read`, `docs:read:all`, `docs:write`, `docs:index`, and `admin`
  (wildcard). Grant the minimum needed. `docs:index` is separate from `docs:write`
  because a forced re-index spends embedding-provider budget on every doc.
- Human-readable metadata (name, prefix, scopes, id) is printed to **stderr**; only the
  plaintext key goes to **stdout**, so `... issue ... > key.txt` captures just the key.
- `--actor` (global flag, default `cli`) is stamped on `created_by` / `updated_by`.
- Name keys after the consumer — that name is the attested actor in the audit log.

## Configuring / enabling fetchers

Fetchers produce the best-effort content snapshot at ingest and on `POST /docs/{id}/refetch`.
Which sources get fetched is determined by two things that must agree:

1. The source's `content_fetch_enabled` flag (in the `sources` table).
2. A `Fetcher` registered for that `source_id` in `default_registry()`
   (`src/fetch/registry.py`).

`web` fetches in-process. `github` and the four Google sources (`gdrive`, `gdocs`, `gsheets`,
`gslides`) fetch via the [connectors](../../connectors/) service over HTTP
(`ConnectorsFetcher`, registered in `src/fetch/registry.py`); migration `006` flipped their
`content_fetch_enabled` to `true` to match. `youtube` remains fetch-disabled:
`content_fetch_enabled` is `false` and no fetcher is registered, so ingest records the doc
without a snapshot. Enabling a new source is a **code change**, not just config — implement
the `Fetcher`, register it, and flip `content_fetch_enabled` (see
[`docs/CONTRIBUTING.md`](CONTRIBUTING.md)).

Fetcher-specific env vars: `CONNECTORS_BASE_URL` / `CONNECTORS_API_KEY` (see above) configure
the connectors-backed fetchers (`github` and the Google sources). **Deploy order is recommended, not required:** documentation-system
boots fine with no connectors configuration at all — `verify_production_secrets()` only warns
on a default `CONNECTORS_API_KEY`, it never blocks startup. Deploying connectors first and
setting these two vars on documentation-system before deploying it just avoids a window where
GitHub and Google-source fetches fail at request time (degraded, not fatal — see the connectors dependency
section below) because connectors isn't up yet or isn't yet configured.

## The team-tracking directory dependency

The documentation-system validates owners against the team-tracking directory over HTTP.
It is a **runtime dependency** but a **soft** one:

- **When the directory is reachable:** owner ids are validated (`GET /teams/{id}`,
  `GET /people/{id}`) and labels are cached on the doc. An unknown id is rejected with
  **HTTP 400**.
- **When the directory is down** (connection failure or 5xx): the service **degrades**.
  Ingest and update still succeed — the owner id is stored, the label is left null, and a
  warning is returned. Labels are backfilled automatically on the next read or update once
  the directory is reachable again.

Operationally this means a directory outage will **not** take the catalog down; it only
delays owner-label resolution and temporarily disables the "unknown owner id" rejection.
Ensure `DIRECTORY_BASE_URL` and `DIRECTORY_API_KEY` are correct so validation works in
steady state.

## The connectors dependency

The documentation-system fetches content for `github` and the Google sources (`gdrive`,
`gdocs`, `gsheets`, `gslides`) by calling the [connectors](../../connectors/) service over HTTP. It is
a runtime dependency with **two different failure modes** depending on when it's down:

- **At boot, outside `local`:** soft. `verify_production_secrets()` logs a warning, not a
  boot refusal, if `CONNECTORS_API_KEY` is still the built-in dev default — the service
  still starts and catalogues docs, it just can't fetch Google-source content until a real
  key is set. See "Environment variables" above.
- **At request time:** soft for ingest, hard for refetch — deliberately different, since
  ingest degrading a whole batch is worse than a single explicit refetch failing loudly.
  On `POST /docs`, if connectors is unreachable or errors, `ConnectorsFetcher` raises
  `FetchError`, which `ingest_doc` catches the same way it catches a `web` fetch
  failure: the doc is still catalogued, just without a content snapshot, and a warning is
  returned — a connectors outage never fails `POST /docs`. On `POST /docs/{id}/refetch`,
  the same `FetchError` is instead surfaced to the caller as an HTTP 502 (see
  `src/api/routers/docs.py`): refetch is an explicit user-initiated action, so it fails
  loudly rather than silently leaving stale content in place.

## The llm (embedding) dependency

Stored content is indexed for retrieval by calling the [llm](../../llm/) service's
`POST /embed` (see "Indexing (#175)" in [ARCHITECTURE.md](ARCHITECTURE.md)). It is a soft
dependency everywhere:

- **At boot:** an empty or dev-default `LLM_API_KEY` outside `local` logs a warning; the service starts.
- **At request time:** indexing runs inline after ingest, refetch, scheduled refresh, and
  `PATCH active`. If the llm service is down, rate-limited, or rejects the request, the
  triggering request still succeeds and nothing is written to the index: ingest returns an
  `indexing failed (...)` warning, refresh-due counts `index_failed`, and refetch/`PATCH`
  log it. The doc's previous chunks (if any) stay intact.
- **Retry:** call `POST /docs/index` with a `docs:index` key. With body `{}` it pages through
  every doc whose chunks are missing, stale, or orphaned; repeat with `after: next_after`
  until `next_after` is null. Nothing schedules this — run it after an llm outage or from
  the same scheduler as refresh-due if failures are routine. Re-embedding after a model
  change uses `{"stale_only": false, "force": true}`.

During an indexing outage, retrieval can still return the previous complete chunk
set under **current** authorization. Its citations retain the old content hash;
the newest stored content is not searchable until indexing recovers. An indexing
failure cannot restore a revoked source grant. Confirmed source disappearance
removes stored text and chunks together, while a transient fetch failure preserves
them. Use the [integration runbook](INGESTION-RETRIEVAL-AUDIT.md) to verify recovery
and record controlled staging evidence before release acceptance.

Embedding cost is per changed body or title; identical body/title hashes avoid repeat
calls after title provenance is recorded. Ingest, title edits, and refetch get slower by the `/embed` round trips (about one request per 96 chunks).

**Deploy order:** recommended (not required) is connectors first, then set
`CONNECTORS_BASE_URL` / `CONNECTORS_API_KEY` on documentation-system, then deploy
documentation-system. documentation-system deploys independently either way — see
"Configuring / enabling fetchers" above for what degrades (not breaks) if the order
is reversed.

## Source-access refresh schedule

Run `doc-source-refresh` hourly (`0 * * * *`) from Railway or the approved external
scheduler. It calls `POST /docs/source-access/refresh-due?limit=10` with a dedicated
`docs:write` key, repeating until `attempted < limit`. The endpoint processes each
document independently and reports `{attempted, refreshed, failed, index_failed}`.
Only sources with
`content_fetch_enabled=true` are selected; disabled sources receive no automatic content
or ACL refresh, so their existing source grants eventually expire.

Hourly checks retain the 24-hour eligibility threshold: they do not download each document
hourly. A document refreshed a few minutes after a job starts becomes due just after the
next day's matching job; an hourly check picks it up within another hour rather than
waiting a second day and risking the 48-hour expiry.

The defaults refresh after 24 hours and expire source-derived grants after 48 hours. A
connector/directory failure retains the last complete grant set until its existing expiry;
after that, source grants no longer authorize reads. Manual grants remain independent;
ownership is metadata only and never authorizes access. Alert on a non-zero `failed`
count or on the scheduler not completing for 24 hours.

Only Drive `user` permissions matching a primary/verified email and Drive `group`
permissions matching an active, membership-synced Team Tracking mapping become grants.
Domain/link-wide shares, external or unknown users, unmapped groups, and pre-migration
legacy groups are intentionally ignored. Add legacy group mappings through Team Tracking's
managed-group migration; never infer access from an ordinary team membership.

Provision a dedicated key using the intended environment's key-management runbook:

```bash
# Run from this service's directory with that environment's DATABASE_URL.
uv --project . run python -m src.cli issue --name source-access-refresh-staging --scopes docs:write
```

Store the issued `doc_` token privately as the runner's `DOCS_API_KEY`. It needs no
`docs:read:all`, impersonation scope, Google credential, or database connection.
The runner checks its own scope through `GET /api-keys/self` before any POST.
Set `DOCS_BASE_URL` to the intended documentation-system API, then run from the
repository root with those variables in the process environment:

```bash
uv --directory services/documentation-system --project . run python -m src.source_refresh
```

The module invocation pins this service's `src` package in the workspace. The
installed `doc-source-refresh` command is also available inside its isolated image.
Neither invocation loads `.env`.

| Option | Default | Meaning |
|---|---|---|
| `--base-url` | `DOCS_BASE_URL` or `http://localhost:8001` | HTTP API target |
| `--batch-size` | `10` | Documents per request, from 1 through 100 |
| `--max-batches` | `100` | Stop with an incomplete-run error if no short batch is observed |
| `--timeout` | `600` | Timeout in seconds for each HTTP network phase |
| `--max-seconds` | `3000` | Elapsed-time budget for starting requests; each request's timeout is capped by the remaining budget |
| `--require-monitor` | off locally; on in staging cron | Fail before API calls unless `SOURCE_REFRESH_MONITOR_URL` is configured |

The time budget is not a server cancellation or a hard process deadline. A timed-out
POST may still finish in the API; the runner never immediately retries it. Failed
document attempts are recorded by the API and become due again after the configured
refresh interval. Per-document failures do not stop draining other batches.

Output contains aggregate counts, nonsecret run UUIDs and sanitized errors. Exit `0`
means a short batch was observed and no refresh/index failures occurred; `1` means
the queue was drained with failures; `2` means configuration/auth/HTTP/response
failure, an exhausted budget, or a conflicting runner. An unacknowledged monitor
ping also exits `2`; `drained` still records whether the API work completed.
Previously completed batch counts remain in the
summary after an HTTP failure. `index_failed` requires the normal `POST /docs/index`
recovery workflow and its separately scoped key; the refresh runner does not re-index
repeatedly. Alert on either nonzero exit and on no completed run for 24 hours.

### Staging deployment and review gate

Publish and review the implementation in a PR targeting `staging` before deployment
([#292](https://github.com/UTMIST/Misty/issues/292)). After it merges and the API is
healthy, create exactly one **dedicated staging service**, `doc-source-refresh`, in
Misty. Use repository `UTMIST/Misty`, branch `staging`, repository-root build context,
and config path `/services/documentation-system/railway.source-refresh.json`.
Record and compare the deployed commit SHA of the API and runner; a matching branch
name alone is insufficient. Enable wait-for-CI and use the API's watch paths.
The API must apply migration `011` before starting the new runner; the cron still
has no database credentials and does not run migrations.

The config reuses the API Dockerfile, clears migrations and HTTP healthchecks,
sets one replica and restart policy `NEVER`, and runs
`python -m src.source_refresh --require-monitor`. Only the `staging` override has
an hourly UTC schedule. The base configuration is disabled; the explicit `pr`
override also removes the inherited schedule and runs a harmless command so a
preview cannot issue even one refresh or heartbeat. Exclude the service from
PR copies as well. Do not attach a volume, public domain, Google credentials,
`DATABASE_URL`, or the API's bootstrap key. Do not repoint the HTTP service at this
config. Creating this file does not provision or activate a job.

Set `DOCS_BASE_URL` to the staging API's private domain **and listening port**,
for example `http://${{documentation-system.RAILWAY_PRIVATE_DOMAIN}}:${{documentation-system.PORT}}`.
Set the dedicated `DOCS_API_KEY` and the monitoring variable described below.
Never copy production credentials into this service. In its first deployment,
observe a manual run and then a real scheduler-triggered run, saving the exit,
aggregate summary, deployment/run IDs, timestamps and matching revision.

[Railway skips a cron invocation while its previous execution is active](https://docs.railway.com/cron-jobs#service-execution-requirements).
That does not serialize a second cron service, a manual HTTP caller, or a rollout.
The runner also acquires a shared database guard through
`POST /docs/source-access/refresh-runs/{run_id}` before its first heartbeat.
Only one runner can enter, including across containers or API restarts. A duplicate
gets `409`, exits `2`, and sends `/fail` without sending `/start` or success.
Keep one service and one region. Use the CLI for manual execution; a raw HTTP
batch caller must follow the [same guard protocol](API.md#source-refresh-runner-guard).
Never launch an older runner that lacks the guard or use a second runner to clear
a stuck first one. One heartbeat check must correspond to exactly one catalog database.

### Failure and missed-run alerts

Use two independent signals, routed to the same operations destination:

1. In **Misty → Settings → Webhooks**, add the operations Discord/Slack incoming
   webhook (or an authenticated receiver). Subscribe to `Deployment.failed`,
   `Deployment.crashed`, and `Deployment.oom_killed`; exclude preview environments.
   Railway webhooks are project-wide, so direct Discord delivery includes other
   services/environments. For staging-runner-only alerts, a receiver must filter
   the staging environment and runner service IDs. Test delivery and prove that
   exits `1` and `2` generate the expected event during acceptance; a successful
   deployment is not proof of a successful cron run. See
   [Railway webhooks](https://docs.railway.com/observability/webhooks).
2. Create a Healthchecks-compatible check named `Misty staging source refresh`,
   scheduled `0 * * * *` in UTC with a **55-minute grace period**, and connect its
   notification integration. Store its base HTTPS ping URL as the secret
   `SOURCE_REFRESH_MONITOR_URL`. The runner sends empty `POST` pings to `/start`,
   the base URL for a clean drain, or `/fail` for an incomplete/failed run, with a
   per-run UUID. UUIDs correlate durations; [Healthchecks only times the most
   recently started run](https://healthchecks.io/docs/measuring_script_run_time/#alerting-logic-when-using-run-ids),
   so the database guard supplies overlap protection. It sends no catalog key,
   counts, document content or source URL to the monitor. The check detects a
   missing finish, a missed invocation, or a
   process killed before it can report; its alert fires before 24 hours without
   a clean completed run. See the [ping API](https://healthchecks.io/docs/http_api/).

A start without completion for 55 minutes is a stuck-run incident. A second runner
is rejected and reports failure immediately, so it cannot hide that incident with
a later start or success. The 3000-second budget is not a hard deadline:
HTTP phase timeouts can extend it. Investigate before stopping a run; terminating
the client does not cancel the API request. Compare run/deployment history and
remove duplicate scheduling before resuming. Test missed-run alerts on a disposable
check using a shorter interval; do not withhold real refreshes for a day to test it.

Monitor requests use a separate HTTP client, a 10-second phase timeout and no
redirects or retries. A failed start ping does not block refreshing; it prevents a
success ping and makes the process exit `2`. A failed final ping likewise exits `2`
without replaying API work. HTTP 200 with `OK (not found)` or a rate-limit message is
not accepted as delivery. If the monitor itself is unavailable, Railway's failure
hook is the independent fallback. Configure and test both before release acceptance.
CLI-only configuration is validated by the runner; it does not load the API's
`Settings` or `verify_production_secrets()`.

The responsible operator and alert destination must be entered in the acceptance
record below. Owning a GitHub issue or CODEOWNERS entry is not an on-call assignment.
No destination is installed by this repository change.

### Recovery, credential rotation and disable/rollback

The guard remains held through the final acknowledged heartbeat. A completed drain
(including counted document/index failures) releases it; a crash, incomplete drain,
API error or uncertain heartbeat leaves it held for inspection. It has no automatic
expiry because a timed-out request can still be doing work in the API. If admission
or release times out, inspect ownership: the request may have committed.

To recover a stranded guard, stop the old runner and confirm all its API requests
and monitor deliveries have finished or failed. Find its UUID in `event=acquire`
or `event=start` logs; if admission was ambiguous, the API database's
`source_refresh_run` row records the current `run_id`, `started_at` and attested
`created_by`. Then use an authorized `docs:write` key to call
`DELETE /docs/source-access/refresh-runs/{run_id}` for that exact owner. Expect
`200`; a `409` means ownership differs or is already absent, so inspect again.
Do not delete an arbitrary current owner, expire it based on age, or suppress the
failure alert. Resume one runner and observe its complete run and recovery signal.

Migration `011` is reversible (`upgrade head` → `downgrade -1` → `upgrade head`),
but dropping its table removes coordination. Disable and drain every runner before
an API schema rollback; ordinary cron disablement does not require a DB downgrade.

| Symptom | Operator action |
|---|---|
| Admission `409` / overlapping runner | Inspect the existing owner and duplicate schedules. The rejected runner has sent `/fail` without replacing the original heartbeat start. Do not release a live owner; use the recovery procedure above only after quiescence. |
| `exit_code=2`, API connection/HTTP failure | Restore the API/network/key. Retain confirmed counts. For a timed-out POST, inspect the API's sanitized audit log and wait until the request is known to have finished before starting another run; never immediately replay it. |
| `drained=true`, `failed>0` | Inspect sanitized API/connector/directory status. Failed documents retain their previous grants until expiry and wait for the next eligibility window; successful/later documents still advance. After restoring the dependency, let the next eligible refresh run or use the documented operator refetch flow. |
| `index_failed>0` | Restore the llm/index dependency, then use a separate `docs:index` key with `POST /docs/index`, paging `after: next_after` until null. Verify no failed results and a subsequent empty stale-only pass. Refresh alone will not retry a now-fresh document's index. |
| `drained=false`, budget exhausted | Inspect backlog and durations; reduce batch size or increase bounded limits only with enough margin before the next hour. Do not add replicas or another scheduler. |
| `monitor_failed=true` | Check the check's URL, existence and integration; inspect Railway run status before retrying anything. A monitoring failure does not undo completed refreshes. |
| Missing finish/missed-run alert | Inspect active runs and deploy history, duplicate services, schedule/config and private networking. Resolve an outstanding POST before resuming; record how long source grants lacked successful refresh. |

Rotate the catalog key by issuing a new `docs:write` key in staging, storing it
privately, then updating only the cron's `DOCS_API_KEY` while no run is active.
Observe the new key's scope check and a complete run before revoking the old key
by its recorded ID (`python -m src.cli revoke <old-key-id>` from the service directory
with the staging DB configuration). Verify the old key is rejected; retain only IDs,
actor names and scopes in the audit. Rotate a monitor URL separately and test its
check without printing it.

To disable, remove the cron schedule and stop the active deployment after resolving
any in-flight API call; pause the heartbeat check only as an acknowledged maintenance
window. Config-as-code overrides dashboard settings on future deployments: remove
`environments.staging.deploy.cronSchedule` (set it to `null`) and use the disabled
start command in the reviewed rollback, or disconnect the cron's source until that
change ships. Revoke its dedicated key if immediate prevention of new work is needed.
Do not delete catalog data or roll back API migrations. Re-enable only one scheduler,
restore alerts, and repeat manual/scheduled acceptance with matching API/runner SHAs.
Production activation requires a separate explicit rollout decision and config change.

### Acceptance record for #292

The implementation is imported from the existing `qiuethan/google-identity-setup`
workspace, with monitoring, strict index-count validation and staging/preview guards
added during review. Local fake-provider checks are **not** staging acceptance.
Record sanitized evidence in the issue or this table before the release go/no-go:

| Evidence | Status / required record |
|---|---|
| Review / publication | [PR #294](https://github.com/UTMIST/Misty/pull/294) targets staging; approval and merged staging SHA remain pending. |
| Staging resource | No cron provisioned during implementation; record service/environment IDs and identical API/runner deployment SHAs. |
| Alert ownership | Pending named responsible operator and backup. |
| Alert destination | Existing Discord webhook `9af3f11e-b6b9-4ffa-beb5-29b1ac18791d` verified with `Deployment.failed`, `Deployment.crashed`, and `Deployment.oom_killed`; previews excluded, existing volume/monitor alerts preserved. Healthchecks integration `Misty staging refresh - Discord` is assigned to the refresh check and sends JSON POSTs to the same destination on down/up events, with mentions disabled; the existing email integration is retained. Channel name and delivery tests remain pending; never paste secret URLs. |
| Heartbeat configuration | On 2026-10-21, configured `Misty staging source refresh` (slug `misty-staging-source-refresh`) in the operations Healthchecks account: `0 * * * *`, UTC, 55-minute grace. Settings and integration assignment verified by read-back. No pings received yet; wiring `SOURCE_REFRESH_MONITOR_URL` into the staging runner remains pending. Treat the ping UUID as a secret too. |
| Manual + scheduled run | Pending both run IDs, UTC start/finish, exit code and aggregate counts, including `drained`. |
| Multi-batch + partial failure | Offline runner/API test exercises three documents across batches with one refresh failure and one index failure; record controlled staging evidence separately. |
| Content/access + recovery | Offline test checks updated content/grants, preservation on transient failure, separate indexing recovery and next-eligible refresh. Staging proof must use approved synthetic documents and access assertions; record outcomes without content/URLs. |
| API unavailable / ambiguous timeout | Offline transport tests verify exit `2`, preserved progress and no POST retry; record a controlled staging outage/recovery without disrupting shared services. |
| Overlap / stranded guard | Offline real-API tests prove a second runner cannot send start/success or release the owner; Postgres tests cover concurrent admission through separate connections. Record controlled staging overlap, exit/alert and explicit recovery evidence. |
| Alert delivery + silence | Pending observed nonzero-exit webhook, stuck/missed-run alert and recovery notification at the chosen destination. |
| Production | Not authorized by #292. |

For controlled staging fault cases, use an isolated test API/dependency deployment
or approved synthetic fixtures; never break the shared API, connectors or llm service
just to trigger an alert. Do not close #292 until manual/scheduled runs and actual
alert delivery are recorded. Pair this evidence with #291 for the release gate.

## Registry cleanup (migration `009`)

Reassigns documents from the retired source to `web` before removing its registry row.
Downgrade restores only the registry entry; it does not undo the data cleanup.
Use a database backup to recover the previous assignments.

## Automatic discovery schedule

The dedicated, disabled-by-default `doc-discover` runner and
`railway.discovery.json` provide initial and recurring sharing discovery over HTTP.
This is separate from the existing-document refresh schedule above. Follow the
[discovery runbook](DISCOVERY.md) for its two scoped keys, persistent checkpoint,
15-minute schedule, enable/status/disable commands, recovery and staging acceptance.
Production activation requires a separate rollout decision.

## Meeting record persistence

Apply migrations through **014** (`uv run alembic upgrade head`) before the new API serves
traffic. Migration 012 adds `meeting_records` and nullable `docs.purged_at`; the prior head
is 011. Verify upgrade → downgrade -1 → upgrade against a disposable database,
never against live meeting data. Downgrading 012 drops meeting retry payloads and IDs;
export them before any operational rollback.
Migration 013 preserves existing records and marks saved meetings for initial index
repair. Its downgrade removes only the index retry flag.
Migration 014 adds confirmed Google-deletion receipts. Its downgrade removes only
the receipt field; rollback after deletion can require manual investigation.

Settings: `MEETING_DOC_CREATE_LEASE_S=600` (minimum 60),
`MEETING_RECORD_MAX_ATTEMPTS=8`, `MEETING_RECORD_RETENTION_DAYS=365`.
The existing `CONNECTORS_API_KEY` additionally needs `meeting-write` and
`meeting-fetch`. The directory key needs the existing identifier, channel-team
and synced Google Group reads. Issue `meeting` a `meetings:write` key and give
the bot `meetings:read,meetings:write` for #223.

The existing staging Railway cron now runs
`python -m src.source_refresh --require-monitor --meeting-records`; add
`meetings:write` to its existing `docs:write` key **before deploying** the runner.
It validates both scopes before work. The meeting phase uses the same time and
batch budgets and no direct DB or Google credentials. Production scheduling
remains explicitly disabled in `railway.source-refresh.json` until rollout.

Recovery: inspect `GET /meeting-records?persistence_status=failed` or
`persistence_status=inaccessible`; use privileged
`POST /meeting-records/{id}/retry` after fixing permissions/configuration.
Do not clear a lease to retry a still-running Google call. Do not replace a
canonical ID or resubmit changed text under the same UUID. Reconciliation
continues hourly for terminal records; investigate `reconcile_failed` and
`removal_pending` by meeting ID. A Google 404 cannot confirm permanent deletion,
so `delete_unconfirmed` remains pending for admin investigation. After seven
days in failed state, the pending transcript is cleared; the Discord PDF is the
fallback if no Google Doc was adopted. Expiry and explicit removal purge the
catalog even if Google cleanup still needs retries.

Saved records with `index_pending=true` retry indexing on the next due run, with
a five-minute retry delay, without another Google write.

`google_deleted_at` records a successful canonical deletion and prevents repeated
deletion during remaining cleanup. If the process or database fails before that receipt commits,
the existing ambiguous-404 investigation procedure still applies.

Removal/unsafe-ACL notices and the existing PDF attachment are the admin's
responsibility until the link-delivery cleanup in #223 ships. Never log record
payloads or credentials. See the [platform rollout](../../../docs/RAILWAY-DEPLOYMENT.md#meeting-record-persistence).
