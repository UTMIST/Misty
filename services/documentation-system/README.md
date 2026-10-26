# documentation-system

Source-of-truth HTTP API for UTMIST's document catalog: a registry of URLs (docs, sheets, slides, repos, videos) with owners, tags, and lightweight content snapshots.

## What this service does

UTMIST accumulates links faster than anyone can track them — onboarding docs on Google Drive, budget sheets, GitHub repos, YouTube recordings. Nobody owns a canonical index of "what exists and who's responsible for it," so links rot, duplicate, or vanish when the one person who bookmarked them graduates.

The documentation-system service is that canonical index. It exposes a REST API over two core concepts:

- **Docs** — a URL plus metadata: title, description, owning team/person, tags, an optional best-effort content snapshot, and an active flag (soft delete).
- **Sources** — a registry of URL "kinds" (`web`, `github`, `gdrive`, `gdocs`, `gsheets`, `gslides`, `youtube`) that says whether a source requires auth to fetch, has an API, and whether content fetching is even enabled for it.

Content fetching is **egress-guarded (SSRF protection).** Ingesting a URL only ever fetches public destinations: the fetcher enforces an http/https scheme allowlist, resolves the target host, and refuses any private/loopback/link-local/reserved/carrier-grade-NAT or cloud-metadata (`169.254.169.254`) address — any hostname is accepted, but only if it resolves to a permitted public IP — before the request and again on every redirect hop. The connection is pinned to the validated IP (Host header + TLS SNI preserved) so DNS-rebinding can't slip an internal target past the check. A blocked or malformed URL is surfaced as an ordinary fetch failure (the doc is still catalogued with the URL as its title), never a 500.

Ingesting a URL is idempotent: re-submitting a known URL merges new tags onto the existing doc rather than creating a duplicate. De-duplication is enforced at the database level — a partial unique index on `url_normalized WHERE active` (migration 004) guarantees at most one active row per normalized URL, so concurrent adds of the same URL converge on a single canonical doc instead of racing to create duplicates, and re-adding a URL that was previously soft-removed reuses/re-catalogues it rather than piling up duplicate active entries. Ownership (`owning_team_id` / `owning_person_id`) is validated against UTMIST's team-tracking service when reachable, and degrades gracefully (id stored, label resolved later) when it isn't — the catalog never blocks on a downstream directory outage.

## Dependency on the team-tracking directory

The documentation-system is the catalog; the [team-tracking](../team-tracking/) service is the **source of truth** for people and teams. When you ingest or update a doc with an `owning_team_id` or `owning_person_id`, this service calls team-tracking over HTTP (`GET /teams/{id}`, `GET /people/{id}`) to validate the id and cache its human-readable label.

Two things follow from this:

- **team-tracking is a runtime dependency.** For ownership validation you need a reachable directory at `DIRECTORY_BASE_URL` (default `http://localhost:8000`) and a directory API key (`DIRECTORY_API_KEY`).
- **degrade-on-directory-down.** If the directory is unreachable (connection failure or 5xx), ingest does not fail. The owner id is stored, the label is left null with a warning, and a later read or update backfills the label once the directory is reachable again. A directory that *is* reachable but returns 404 for the id is a genuine "unknown owner" error (HTTP 400), not a degrade.

See `docs/ARCHITECTURE.md` for the full data flow.

## Dependency on connectors

The Google sources (`gdocs`, `gsheets`, `gslides`, `gdrive`) fetch content and complete
Drive ACL evidence from [connectors](../connectors/) over HTTP (`POST /fetch`) rather than
holding Google credentials themselves. documentation-system resolves user emails only
through verified directory identities and group emails only through Team Tracking mappings
whose Google membership sync is healthy. Broad and unmapped principals produce no grant.
Configure the dependency with `CONNECTORS_BASE_URL` and `CONNECTORS_API_KEY`.

Connectors reachability at request time is soft for ingest but hard for refetch: a connectors outage during `POST /docs` is caught and turned into a per-doc warning (the doc is still catalogued, just without a content snapshot) — see the `FetchError` handling in `src/ingest.py`. During `POST /docs/{id}/refetch`, the same `FetchError` is instead surfaced as an HTTP 502 — see `src/api/routers/docs.py` — because refetch is an explicit user-initiated action rather than a batch ingest. It is also a soft dependency at *boot* in staging/production: `verify_production_secrets()` (`src/config.py`) logs a startup warning, not a boot refusal, if `CONNECTORS_API_KEY` is still the built-in dev default — this service still starts and catalogues docs, it just can't fetch Google-source content. See `docs/DEPLOYMENT.md` for the recommended (not required) deploy order (connectors before documentation-system, so Google fetches work from the start).

## Dependency on the llm service

Stored content is indexed for retrieval by embedding it through the [llm](../llm/) service's
`POST /embed`. Configure it with `LLM_BASE_URL` and `LLM_API_KEY` (an llm key with the
`embed` scope). Indexing runs inline after ingest, refetch, scheduled refresh, and
`PATCH active` or `PATCH title`; it is a soft dependency, so an llm outage never fails those requests. The
doc stays catalogued with its content, ingest returns an `indexing failed` warning, and
`POST /docs/index` (scope `docs:index`) retries. See "Indexing (#175)" in
`docs/ARCHITECTURE.md`.

## Quick start

Prerequisites: Docker, Python 3.11+, [uv](https://github.com/astral-sh/uv).

```bash
# 0. From the repo root, enter the service directory (all commands below run here)
cd services/documentation-system

# 1. Copy environment config and start Postgres
cp .env.example .env
docker compose up -d postgres

# 2. Install dependencies (including dev tools)
uv sync --extra dev

# 3. Apply database migrations
uv run alembic upgrade head

# 4. Start the API server
uv run uvicorn src.api.app:app --reload --port 8001
```

> The repo is a single [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/) (root `pyproject.toml` with `[tool.uv.workspace] members = ["services/*", "packages/*"]`, one root `uv.lock`). documentation-system depends on the shared `platform-auth` package (`[tool.uv.sources] platform-auth = { workspace = true }`) but the commands above are unchanged — `uv sync`, `uv run pytest`, `uv run alembic` still work exactly as shown when run from this directory.

The API is now running at `http://localhost:8001`. Interactive Swagger UI is at `http://localhost:8001/swagger` (note: relocated from `/docs`, since `/docs` is the docs-resource router). Machine-readable OpenAPI schema is at `GET /openapi.json`.

The default dev API key is `dev-api-key-change-me` (set in `.env`). Pass it as `X-API-Key` on every request:

```bash
curl -sS http://localhost:8001/sources \
  -H "X-API-Key: dev-api-key-change-me" | python3 -m json.tool
```

**For production, use per-consumer scoped keys instead of the shared env key.** Issue them via the CLI:

```bash
# Issue a read/write key for an ingestion bot
uv run doc-keys issue --name slack-bot --scopes docs:read docs:write
# Prints: doc_<prefix>_<secret>  (shown ONCE — capture it now)

# List existing keys (metadata only, never plaintext)
uv run doc-keys list --active-only

# Revoke a compromised key
uv run doc-keys revoke <api_key_id>
```

Scopes: `docs:{read,write}`, `admin` (wildcard). See the auth model section below for how attested actors and audit fields work.

A doc whose source fetch failed at ingest is catalogued without content (the failure is only a warning). Find and repair those with the `doc-backfill` CLI, which talks to this service over HTTP — so it needs a scoped key, not `DATABASE_URL`:

```bash
# What is missing, without fetching anything
DOCS_API_KEY=doc_<prefix>_<secret> uv run doc-backfill --dry-run

# Repair it; exits non-zero if any doc still fails
DOCS_API_KEY=doc_<prefix>_<secret> uv run doc-backfill
```

Sources with `content_fetch_enabled=false` (`youtube`) can never have content, so they are reported as skipped rather than failed. A refetch that succeeds but extracts nothing (a title-only `github` doc, an empty page) is reported as `empty`, not `repaired` — a 200 from the refetch endpoint means the fetch ran, not that content was stored, so the run re-lists `has_content=false` to confirm. The tool checks its own scopes (`GET /api-keys/self`) before trusting any listing: without `docs:read:all` the catalog lists as empty rather than 403, which would otherwise read as "nothing to repair". See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md#repairing-docs-missing-content-doc-backfill-cli).

For recurring Google content and access refreshes, run `doc-source-refresh` hourly
from an external scheduler. It drains the existing refresh endpoint in bounded
batches and checks its key before starting. The checked-in staging command adds
`--meeting-records` and requires both `docs:write` and `meetings:write`; a manual
docs-only run omits that flag and needs `docs:write`. A drained run with handled
refresh, index, or meeting-item failures exits `0` and logs a warning with the
failure counts. Incomplete runs and runner/monitor failures still exit nonzero.
It discovers no new documents. See the
[refresh schedule](docs/DEPLOYMENT.md#source-access-refresh-schedule) for limits,
exit codes, and the separate Railway cron config. Migration `011` adds the shared
runner guard: overlapping runners fail before sending a start heartbeat. A crashed
or incomplete run requires the documented operator recovery before another run.

| CLI environment variable | Default | Purpose |
|---|---|---|
| `DOCS_BASE_URL` | `http://localhost:8001` | Target documentation-system API for both HTTP CLIs |
| `DOCS_API_KEY` | required | Caller credential; staging refresh needs `docs:write meetings:write`, manual docs-only refresh needs `docs:write`, backfill needs the scopes above |
| `SOURCE_REFRESH_MONITOR_URL` | unset | Secret Healthchecks-compatible HTTPS ping URL for refresh start/success/failure; required by the staging cron |

These CLI variables are read from the process environment; the CLIs do not load `.env`.

**Ports:** this service runs on **8001** (team-tracking uses 8000) and its Postgres container is exposed on **5434** — both chosen to avoid colliding with team-tracking's defaults when running both services locally.

## Folder tour

```
documentation-system/
├── contracts/               Domain models (Pydantic) + boundary Protocols
│   ├── types.py             Source, Doc, DocIngest/DocUpdate, IngestResult, ApiKey
│   ├── storage.py           StorageAdapter Protocol — docs, sources, API keys
│   ├── fetcher.py           Fetcher Protocol — fetch(url) -> FetchResult
│   ├── retrieval.py         Search DTOs and EmbeddingClient Protocol
│   └── directory.py         DirectoryClient Protocol — team/person label lookups
│
├── src/
│   ├── api/                 FastAPI application
│   │   ├── app.py           App factory (create_app); docs_url="/swagger"
│   │   ├── auth.py          Thin shim over the shared `platform_auth` package: require_api_key / require_scope / get_actor
│   │   ├── deps.py          get_storage / get_fetchers / get_directory / get_embeddings
│   │   ├── hashing.py       Thin shim over `platform_auth`: Argon2 API key hashing + prefix parsing
│   │   └── routers/         docs.py, sources.py, search.py
│   │
│   ├── storage/              Concrete StorageAdapter implementations
│   │   ├── schema.py         SQLAlchemy Core table definitions
│   │   ├── chunks.py         Shared chunk-set validation before persistence
│   │   ├── in_memory.py      InMemoryStorageAdapter — used in tests
│   │   └── postgres.py       PostgresStorageAdapter — used in production
│   │
│   ├── fetch/                 Concrete Fetcher implementations + registry
│   │   ├── web.py             Generic web page fetcher (SSRF egress guard: scheme allowlist, private/metadata-IP block, IP pinning)
│   │   ├── connectors.py      ConnectorsFetcher — github + Google sources via the connectors service
│   │   └── registry.py        FetcherRegistry: source_id -> Fetcher
│   │
│   ├── directory/
│   │   └── http_client.py     HttpDirectoryClient — calls team-tracking over HTTP
│   │
│   ├── embed/
│   │   └── http_client.py     HttpEmbedder — batches chunk text to the llm service's /embed
│   │
│   ├── ingest.py               ingest_doc(): normalize, dedup, source, fetch, own, persist
│   ├── indexing.py             index_doc(): chunk, embed, and replace a doc's chunk set
│   ├── eval/                   Retrieval quality harness — dev tooling, not a service path
│   │   ├── embedding.py        Deterministic offline lexical embedder; a measuring stand-in, not a model
│   │   ├── corpus.py           Load the fixed corpus and seed it through the real index_doc
│   │   └── harness.py          Ask the question set, report hit@k with its configuration; `calibrate` mode
│   ├── retrieval.py            Shared keyword normalization, validation, and rank fusion
│   ├── embedding.py            Authenticated LLM /embed client
│   ├── url_norm.py             URL normalization + source derivation from patterns
│   ├── config.py               Settings (DATABASE_URL, API_KEY, DIRECTORY_*, LLM_*)
│   ├── cli.py                  doc-keys CLI (issue/list/revoke)
│   └── backfill.py             doc-backfill CLI — repairs docs missing content
│
├── migrations/                 Alembic migrations
│   ├── env.py
│   └── versions/
│       ├── 001_initial_schema.py   Creates docs, doc_tags, sources, api_keys tables
│       ├── 002_seed_sources.py     Seeds the 8 built-in sources
│       ├── 003_doc_grants.py       Adds the doc_grants table (visibility / on-behalf-of)
│       ├── 004_docs_url_unique_active.py   Partial unique index on active normalized URLs
│       ├── 005_doc_content.py       Stores full extracted content + hash
│       ├── 006_enable_google_content_fetch.py  Enables Google connector fetches
│       ├── 007_source_document_access.py  Source grant provenance, expiry, sync time
│       ├── 008_doc_chunks.py      pgvector embeddings and generated keyword metadata
│       ├── 009_remove_retired_source.py  Retires a source while preserving its documents
│       ├── 010_chunk_title_hash.py  Embedding title provenance and stale-index detection
│       ├── 011_source_refresh_run.py  Persistent singleton runner guard
│       ├── 012–016                  Meeting persistence and recovery
│       └── 017_chunk_sets_per_model.py  Complete chunk sets per embedding model
│
├── tests/                      Fast suite plus gated Postgres integration tests
│   ├── conftest.py              build_seed_sources() — shared seed matching migration 002
│   ├── test_api_docs.py
│   ├── test_api_sources.py
│   ├── test_auth.py
│   ├── test_cli.py
│   ├── test_config.py
│   ├── test_directory_client.py
│   ├── test_fetchers.py
│   ├── test_in_memory_adapter.py
│   ├── chunk_storage_cases.py    Shared chunk-storage assertions for both adapters
│   ├── fake_embedder.py          Offline /embed stand-in (autouse `fake_embedder` fixture)
│   ├── test_indexing.py          index_doc lifecycle: unchanged, replace, clear, failure, races
│   ├── test_api_indexing.py      Triggers, source deletion, and POST /docs/index
│   ├── test_eval_harness.py      embedder, seeding, scoring, leaks, fixture integrity, CLI
│   ├── test_embed_client.py      HttpEmbedder batching and error mapping
│   ├── test_migration_007.py     Source-access migration round trip
│   ├── test_migration_008.py     pgvector migration, constraints, and round trip
│   ├── test_migration_009.py     Retired-source cleanup and registry rollback
│   ├── test_migration_010.py     Title provenance migration and preservation of existing chunks
│   ├── test_migration_011.py     Runner guard migration and catalog preservation
│   ├── test_migration_017.py     Per-model sets and populated staging preservation
│   ├── test_ingest.py
│   ├── test_postgres_adapter.py   Integration tests (requires running Postgres; gated)
│   ├── test_protocols.py
│   ├── test_openapi.py
│   ├── test_types.py
│   └── test_url_norm.py
│
├── docker-compose.yml           Local pgvector/Postgres 16 (host port 5434 — staging/prod use Neon)
├── Dockerfile                   Production image (built + smoke-tested by CI; used by Railway).
│                                 Build context is the repo root (so packages/ is reachable);
│                                 installs via `uv sync --frozen --no-dev --package documentation-system`.
└── alembic.ini
```

**Dependency direction:** `contracts/` has no imports from `src/`. `src/api/` depends only on `contracts/` and `src/config`, `src/ingest`, `src/fetch`, `src/directory`. `src/storage/`, `src/fetch/`, and `src/directory/` each implement a `contracts/` Protocol and know nothing about FastAPI. `src/api/deps.py` is the only place concrete adapters get wired to their Protocols.

## The three boundaries

The service is built around three swappable Protocols, so no concrete dependency (a specific database, a specific fetch strategy, a specific directory service) leaks into the ingest/API layers:

- **`StorageAdapter`** (`contracts/storage.py`) — persistence for docs, sources, and API keys. Two implementations: `InMemoryStorageAdapter` (tests) and `PostgresStorageAdapter` (production). Swapping to Postgres in Task 15 required zero changes to `src/ingest.py` or the routers.
- **`Fetcher`** (`contracts/fetcher.py`) — `fetch(url) -> FetchResult`. `FetcherRegistry` maps a `source_id` to a concrete fetcher (`web.py` in-process; `connectors.py` for `github` and the Google sources). Fetch-disabled sources skip fetching; `requires_auth` alone does not disable it. Enabled sources without a registered fetcher produce a warning. The web fetcher applies SSRF egress protection (http/https scheme allowlist, public-address validation that blocks private/reserved/link-local/CGN/metadata IPs, per-hop redirect revalidation, pinned connections) and rejects a blocked or malformed URL as a `BlockedURLError` (a `FetchError` subclass). Fetch failures raise `FetchError`, which `ingest_doc` catches and turns into a warning — a doc is still created with the URL as its title.
- **`DirectoryClient`** (`contracts/directory.py`) — resolves owner labels, verified email identities, active memberships, and only membership-synced Google Group mappings. Owner-label outages degrade; an outage during ACL reconciliation aborts the whole reconciliation so a partial result can never revoke or broaden access.

## Auth model

Ownership is attribution metadata only. Person-scoped access requires a valid matching
grant; see the [visibility policy](docs/API.md#visibility-and-x-on-behalf-of).

Auth is **Level 2** (scoped API keys), matching team-tracking:

- Resource endpoints require `X-API-Key`; health, schema and interactive API documentation routes are public. Keys are either the shared bootstrap env key (`API_KEY`, scope: `admin`) or a per-consumer key issued via the `doc-keys` CLI, stored as an Argon2 hash with the consumer's scope set.
- There is **no `X-Actor` header** — the actor stamped on `created_by`/`updated_by`/audit log entries is always the authenticated key's own name (`AuthedKey.name`). A caller cannot claim to be someone else; this is the "attested actor" model.
- Endpoint scopes distinguish catalog reads/writes, indexing and meeting operations. `admin` satisfies endpoint scope checks but does not replace literal person/channel assertion scopes; see the [scope reference](docs/API.md#scopes).
- `AuditLogMiddleware` (imported from `platform_auth` directly, wired in `src/api/app.py`) logs every request with the resolved actor.
- Manage keys with the CLI:
  ```bash
  uv run doc-keys issue --name my-consumer --scopes docs:read docs:write
  uv run doc-keys list --active-only
  uv run doc-keys revoke <api_key_id>
  ```

## API at a glance

The resource endpoints below require `X-API-Key` with the appropriate scope.

| Method | Path | Scope | Description |
|--------|------|-------|-------------|
| POST | `/search` | `docs:read` + literal `act-as-user` or `act-as-channel` | Authorized hybrid chunk search; [contract and portable fixture](docs/API.md#authorized-retrieval-post-search) |
| POST | `/docs` | `docs:write` | Ingest a URL (idempotent; 200 if already catalogued, 201 if new) |
| GET | `/docs` | `docs:read` | List docs (`?owning_team_id=&owning_person_id=&source_id=&tag=&has_content=&active_only=true`) |
| GET | `/docs/{id}` | `docs:read` | Get one doc (backfills owner labels if previously unresolved) |
| PATCH | `/docs/{id}` | `docs:write` | Update a doc (title, description, owner ids, active flag) |
| POST | `/docs/{id}/tags` | `docs:write` | Add a tag |
| DELETE | `/docs/{id}/tags/{tag}` | `docs:write` | Remove a tag |
| POST | `/docs/{id}/refetch` | `docs:write` | Re-run content fetch for an existing doc |
| POST | `/docs/discovery` | `docs:write` | Import a newly discovered Google file identity |
| POST | `/docs/source-access/refresh-due` | `docs:write` | Refresh a bounded batch whose Google ACL is due |
| POST | `/docs/index` | `docs:index` | (Re-)index stored content; retry path for indexing failures |
| GET | `/sources` | `docs:read` | List sources (`?active_only=false`) |
| GET | `/sources/{id}` | `docs:read` | Get one source |
| GET | `/api-keys/self` | *(any valid key)* | The calling key's own name and scopes |

Machine-readable OpenAPI schema: `GET /openapi.json`. Interactive Swagger UI: `GET /swagger`.

### Retrieval configuration

| Variable | Default | Purpose |
|---|---|---|
| `LLM_BASE_URL` | `http://localhost:8002` | Platform LLM service for chunk and query embeddings |
| `LLM_API_KEY` | empty | Secret consumer key with `embed`; missing key makes search return 503 |
| `SEARCH_EMBED_MODEL` | `openai-embed-3-small` | Neutral embedding alias; `openai-embed-3-large` is also supported at 1536 dimensions |
| `EMBED_MODEL` | unset | Re-embed target; while it names another model, indexing also builds that model's sets beside the served ones. See [re-embedding](docs/DEPLOYMENT.md#re-embedding-after-a-model-change-209) |
| `EMBED_MAX_REQUEST_CHARS` | `400000` | Indexing request character budget; keep at or below the LLM service's limit |
| `SEARCH_MIN_COSINE` | `0.3` | Absolute vector relevance floor, greater than 0 and at most 1; complete keyword matches qualify independently |

The directory key additionally needs `channels:read` for channel search. See
[deployment](docs/DEPLOYMENT.md#authorized-retrieval) for provisioning, model
compatibility, and the catalog-preserving boot behavior.

## Testing

The test suite has two modes:

**Fast (in-memory, no Docker required):**
```bash
uv run pytest --ignore=tests/test_postgres_adapter.py -q
```

This uses `InMemoryStorageAdapter` injected via FastAPI's `dependency_overrides`, plus fakes for `Fetcher` and `DirectoryClient`. `tests/conftest.py` provides `build_seed_sources()`, a shared source seed matching migration 002, used across the in-memory adapter, ingest, API, and OpenAPI tests.

Without `RUN_PG_TESTS=1`, the Postgres adapter and migration tests are skipped.

For ingestion → indexing → authorized retrieval acceptance (#277), run
`uv run pytest tests/test_ingestion_retrieval.py -q`. The same cases run against
Postgres in `tests/test_postgres_adapter.py::TestIngestionRetrieval`. They use the
real HTTP clients with deterministic external responses, and never seed chunks.
See the [integration runbook](docs/INGESTION-RETRIEVAL-AUDIT.md) for commands,
failure/freshness behavior, and controlled staging acceptance.

**Full (includes Postgres integration):**
```bash
docker compose up -d postgres
export DATABASE_URL=postgresql+psycopg://docs:dev_password@localhost:5434/docs
uv run alembic upgrade head
RUN_PG_TESTS=1 RUN_PG_MIGRATION_TESTS=1 uv run pytest -q
```

`tests/test_postgres_adapter.py` runs the same behavioral assertions against a live Postgres instance. Chunk assertions are shared with the in-memory suite through `tests/chunk_storage_cases.py`; migration tests also verify pgvector constraints and reversibility. Use the supplied pgvector-enabled container.

Export `DATABASE_URL` explicitly so standalone Alembic and pytest target the same
database. Standalone Alembic reads `.env`; pytest's autouse fixtures disable `.env`
loading and clear cached settings before each test. Collection-time application
imports can still read `.env` before those fixtures run. Exported environment
variables take precedence in both cases.

Use a disposable database: the adapter suite truncates catalog tables, and
`RUN_PG_TESTS=1` alone enables older destructive migration tests (including
`test_migration_004.py` and `test_migration_005.py`).
`RUN_PG_MIGRATION_TESTS=1` additionally enables later suites with downgrades that
drop chunk storage and creation/removal of a temporary database and role. The
second flag is not a safety barrier for retained data. Enable both flags only
against a disposable cluster with superuser credentials, as CI does.

Lint before committing:

```bash
uv run ruff check .
uv run ruff format --check .
```

## Further documentation

| Doc | Audience | Contents |
|-----|----------|----------|
| [`docs/API.md`](docs/API.md) | Consumers (bots, dashboards, HTTP clients) | Every endpoint, auth, scopes, ingest idempotency, degrade behavior, error semantics |
| [`docs/EVALUATION.md`](docs/EVALUATION.md) | Contributors tuning retrieval | The harness: seeding the corpus, running it, reading `hit@k`, calibrating the relevance floor, and what the lexical stand-in embedder does and does not measure |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Contributors | The three Protocols, adapters, ingest orchestrator, URL normalization, fetcher registry, directory client, data model |
| [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md) | Contributors | Task walkthroughs: add an endpoint, add a fetcher, add an adapter method, write a migration, run the tests |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Operators | Running in prod: Postgres, env vars, migrations, issuing keys, enabling fetchers, the directory dependency |

## Status

v1 covers the docs + sources registry, DB-enforced dedup, source-ACL reconciliation with
revocation and fail-closed expiry, best-effort content fetching with SSRF protection,
team-tracking-backed ownership, tags, soft delete, scoped API keys, and both storage adapters.

Stored content is indexed for retrieval: chunked, embedded through the llm service, and
persisted with pgvector, source offsets, model/dimension metadata, and keyword-search
metadata. Ingest, refetch, refresh, and retirement keep the index current, and
`POST /docs/index` retries failures (#175). See the
[storage contract](docs/ARCHITECTURE.md#doc_chunks) and
[database prerequisites](docs/DEPLOYMENT.md#postgres).

**Deferred (per design §3/§11 non-goals):**

- Controlled staging/provider acceptance for #277 (automated ingestion/retrieval
  verification is implemented; see the [audit](docs/INGESTION-RETRIEVAL-AUDIT.md)),
  and bot/DM/channel integration (#278).
- An in-process scheduler or queue — Railway (or another external scheduler) calls the bounded refresh endpoint hourly; see the [refresh schedule](docs/DEPLOYMENT.md#source-access-refresh-schedule).
- Additional fetchers — YouTube remains a registered source without a fetcher implementation.
- Bulk import — ingest is one URL per request; no CSV/bulk endpoint.
- Pagination — list endpoints return all matching rows, adequate for current catalog size.
- Admin UI — no dashboard; catalog browsing goes through the API or a future thin client.

## Automatic sharing discovery

When an operator enables discovery for your environment, share a supported Google
file or My Drive folder with Misty's connector email; nested supported files are
included. No `/doc add` or folder-root registration is needed. Use the
[member instructions](docs/DISCOVERY.md#members-sharing-with-misty) for sharing
prerequisites and expected search behavior.

The HTTP-only `doc-discover` runner has separate settings from the API. It reads
**process environment only**, including the existing `DOCS_BASE_URL`,
`DOCS_API_KEY`, `CONNECTORS_BASE_URL`, and `CONNECTORS_API_KEY`. Its two keys default
to empty and must be configured when enabled. No database/Google credential is
needed by the runner. Startup validates settings and credentials before requests;
the API's production boot check does not read these runner-only settings.

| Runner setting | Default | Purpose |
|---|---|---|
| `DISCOVERY_ENABLED` | `false` | Explicit operator enable/disable |
| `DISCOVERY_STATE_PATH` | `discovery-state.json` | Durable checkpoint; mount a volume in staging |
| `DISCOVERY_IDENTITY` | `default` | Environment/connector identity label; changing it requires a new checkpoint |
| `DISCOVERY_PAGE_SIZE` | `100` | Drive results per page, 1–100 |
| `DISCOVERY_MAX_PAGES` | `20` | Listing requests per invocation, 1–1000 |
| `DISCOVERY_MAX_ITEMS` | `100` | Items handled per invocation, 1–10000 |
| `DISCOVERY_MAX_FOLDERS` | `10000` | Unique folders per full scan, 1–100000 |
| `DISCOVERY_MAX_SECONDS` | `900` | Budget for starting requests; finite and positive |
| `DISCOVERY_TIMEOUT_SECONDS` | `120` | HTTP phase timeout; finite and positive |
| `DISCOVERY_EXCLUDED_IDS` | `[]` | JSON array of file/folder ids excluded from discovery |
| `DISCOVERY_MONITOR_URL` | empty | Secret HTTPS heartbeat URL for invocation failures, missing runs and stuck runs |
| `DISCOVERY_COMPLETION_MONITOR_URL` | empty | Separate secret HTTPS check, reset only by a clean full scan |

The staging command uses `--require-monitor`, which requires both checks before
API work. Each URL belongs to discovery only; do not reuse the refresh check.

See the [operator runbook](docs/DISCOVERY.md) for scheduling, bounds, recovery,
limitations and the staging acceptance gate. Production activation is separate.

## Meeting record configuration

Completed meetings are accepted durably at `PUT /meeting-records/{meeting_id}`.
The catalog owns Google persistence, structured generation metadata and retry
state; see the [API contract](docs/API.md#meeting-records).

| Var | Default | Purpose |
|---|---|---|
| `MEETING_DOC_CREATE_LEASE_S` | `600` | Creation/reconciliation lease, minimum 60 seconds |
| `MEETING_RECORD_MAX_ATTEMPTS` | `8` | Automatic persistence attempts before explicit admin recovery |
| `MEETING_RECORD_RETENTION_DAYS` | `365` | Lifetime before Google/catalog removal |

The existing connector key additionally needs `meeting-write,meeting-fetch`.
See [deployment and recovery](docs/DEPLOYMENT.md#meeting-record-persistence).
