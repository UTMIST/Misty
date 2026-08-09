# documentation-system

Source-of-truth HTTP API for UTMIST's document catalog: a registry of URLs (docs, sheets, slides, repos, videos) with owners, tags, and lightweight content snapshots.

## What this service does

UTMIST accumulates links faster than anyone can track them — onboarding docs on Google Drive, budget sheets, GitHub repos, Notion pages, YouTube recordings. Nobody owns a canonical index of "what exists and who's responsible for it," so links rot, duplicate, or vanish when the one person who bookmarked them graduates.

The documentation-system service is that canonical index. It exposes a REST API over two core concepts:

- **Docs** — a URL plus metadata: title, description, owning team/person, tags, an optional best-effort content snapshot, and an active flag (soft delete).
- **Sources** — a registry of URL "kinds" (`web`, `github`, `gdrive`, `gdocs`, `gsheets`, `gslides`, `notion`, `youtube`) that says whether a source requires auth to fetch, has an API, and whether content fetching is even enabled for it.

Content fetching is **egress-guarded (SSRF protection).** Ingesting a URL only ever fetches public destinations: the fetcher enforces an http/https scheme allowlist, resolves the target host, and refuses any private/loopback/link-local/reserved/carrier-grade-NAT or cloud-metadata (`169.254.169.254`) address — any hostname is accepted, but only if it resolves to a permitted public IP — before the request and again on every redirect hop. The connection is pinned to the validated IP (Host header + TLS SNI preserved) so DNS-rebinding can't slip an internal target past the check. A blocked or malformed URL is surfaced as an ordinary fetch failure (the doc is still catalogued with the URL as its title), never a 500.

Ingesting a URL is idempotent: re-submitting a known URL merges new tags onto the existing doc rather than creating a duplicate. De-duplication is enforced at the database level — a partial unique index on `url_normalized WHERE active` (migration 004) guarantees at most one active row per normalized URL, so concurrent adds of the same URL converge on a single canonical doc instead of racing to create duplicates, and re-adding a URL that was previously soft-removed reuses/re-catalogues it rather than piling up duplicate active entries. Ownership (`owning_team_id` / `owning_person_id`) is validated against UTMIST's team-tracking service when reachable, and degrades gracefully (id stored, label resolved later) when it isn't — the catalog never blocks on a downstream directory outage.

## Dependency on the team-tracking directory

The documentation-system is the catalog; the [team-tracking](../team-tracking/) service is the **source of truth** for people and teams. When you ingest or update a doc with an `owning_team_id` or `owning_person_id`, this service calls team-tracking over HTTP (`GET /teams/{id}`, `GET /people/{id}`) to validate the id and cache its human-readable label.

Two things follow from this:

- **team-tracking is a runtime dependency.** For ownership validation you need a reachable directory at `DIRECTORY_BASE_URL` (default `http://localhost:8000`) and a directory API key (`DIRECTORY_API_KEY`).
- **degrade-on-directory-down.** If the directory is unreachable (connection failure or 5xx), ingest does not fail. The owner id is stored, the label is left null with a warning, and a later read or update backfills the label once the directory is reachable again. A directory that *is* reachable but returns 404 for the id is a genuine "unknown owner" error (HTTP 400), not a degrade.

See `docs/ARCHITECTURE.md` for the full data flow.

## Dependency on connectors

The Google sources (`gdocs`, `gsheets`, `gslides`, `gdrive`) fetch their content by calling the [connectors](../connectors/) service over HTTP (`POST /fetch`) rather than holding Google credentials themselves — see `src/fetch/connectors.py` and `src/fetch/registry.py`. Configure it with `CONNECTORS_BASE_URL` (default `http://localhost:8005`) and `CONNECTORS_API_KEY`.

Connectors reachability at request time is soft for ingest but hard for refetch: a connectors outage during `POST /docs` is caught and turned into a per-doc warning (the doc is still catalogued, just without a content snapshot) — see the `FetchError` handling in `src/ingest.py`. During `POST /docs/{id}/refetch`, the same `FetchError` is instead surfaced as an HTTP 502 — see `src/api/routers/docs.py` — because refetch is an explicit user-initiated action rather than a batch ingest. It is also a soft dependency at *boot* in staging/production: `verify_production_secrets()` (`src/config.py`) logs a startup warning, not a boot refusal, if `CONNECTORS_API_KEY` is still the built-in dev default — this service still starts and catalogues docs, it just can't fetch Google-source content. See `docs/DEPLOYMENT.md` for the recommended (not required) deploy order (connectors before documentation-system, so Google fetches work from the start).

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

**Ports:** this service runs on **8001** (team-tracking uses 8000) and its Postgres container is exposed on **5434** — both chosen to avoid colliding with team-tracking's defaults when running both services locally.

## Folder tour

```
documentation-system/
├── contracts/               Domain models (Pydantic) + the three boundary Protocols
│   ├── types.py             Source, Doc, DocIngest/DocUpdate, IngestResult, ApiKey
│   ├── storage.py           StorageAdapter Protocol — docs, sources, API keys
│   ├── fetcher.py           Fetcher Protocol — fetch(url) -> FetchResult
│   └── directory.py         DirectoryClient Protocol — team/person label lookups
│
├── src/
│   ├── api/                 FastAPI application
│   │   ├── app.py           App factory (create_app); docs_url="/swagger"
│   │   ├── auth.py          Thin shim over the shared `platform_auth` package: require_api_key / require_scope / get_actor
│   │   ├── deps.py          get_storage / get_fetchers / get_directory
│   │   ├── hashing.py       Thin shim over `platform_auth`: Argon2 API key hashing + prefix parsing
│   │   └── routers/         docs.py, sources.py
│   │
│   ├── storage/              Concrete StorageAdapter implementations
│   │   ├── schema.py         SQLAlchemy Core table definitions
│   │   ├── in_memory.py      InMemoryStorageAdapter — used in tests
│   │   └── postgres.py       PostgresStorageAdapter — used in production
│   │
│   ├── fetch/                 Concrete Fetcher implementations + registry
│   │   ├── web.py             Generic web page fetcher (SSRF egress guard: scheme allowlist, private/metadata-IP block, IP pinning)
│   │   ├── github.py          GitHub-aware fetcher
│   │   └── registry.py        FetcherRegistry: source_id -> Fetcher
│   │
│   ├── directory/
│   │   └── http_client.py     HttpDirectoryClient — calls team-tracking over HTTP
│   │
│   ├── ingest.py               ingest_doc(): normalize, dedup, source, fetch, own, persist
│   ├── url_norm.py             URL normalization + source derivation from patterns
│   ├── config.py               Settings (DATABASE_URL, API_KEY, DIRECTORY_*)
│   └── cli.py                  doc-keys CLI (issue/list/revoke)
│
├── migrations/                 Alembic migrations
│   ├── env.py
│   └── versions/
│       ├── 001_initial_schema.py   Creates docs, doc_tags, sources, api_keys tables
│       ├── 002_seed_sources.py     Seeds the 8 built-in sources
│       ├── 003_doc_grants.py       Adds the doc_grants table (visibility / on-behalf-of)
│       └── 004_docs_url_unique_active.py   Partial unique index on url_normalized WHERE active (DB-level dedup)
│
├── tests/                      Test suite (59 fast + 7 Postgres, gated)
│   ├── conftest.py              build_seed_sources() — shared seed matching migration 002
│   ├── test_api_docs.py
│   ├── test_api_sources.py
│   ├── test_auth.py
│   ├── test_cli.py
│   ├── test_config.py
│   ├── test_directory_client.py
│   ├── test_fetchers.py
│   ├── test_in_memory_adapter.py
│   ├── test_ingest.py
│   ├── test_postgres_adapter.py   Integration tests (requires running Postgres; gated)
│   ├── test_protocols.py
│   ├── test_openapi.py
│   ├── test_types.py
│   └── test_url_norm.py
│
├── docker-compose.yml           Local dev Postgres (host port 5434 — staging/prod use Neon)
├── Dockerfile                   Production image (built + smoke-tested by CI; used by Railway).
│                                 Build context is the repo root (so packages/ is reachable);
│                                 installs via `uv sync --frozen --no-dev --package documentation-system`.
└── alembic.ini
```

**Dependency direction:** `contracts/` has no imports from `src/`. `src/api/` depends only on `contracts/` and `src/config`, `src/ingest`, `src/fetch`, `src/directory`. `src/storage/`, `src/fetch/`, and `src/directory/` each implement a `contracts/` Protocol and know nothing about FastAPI. `src/api/deps.py` is the only place concrete adapters get wired to their Protocols.

## The three boundaries

The service is built around three swappable Protocols, so no concrete dependency (a specific database, a specific fetch strategy, a specific directory service) leaks into the ingest/API layers:

- **`StorageAdapter`** (`contracts/storage.py`) — persistence for docs, sources, and API keys. Two implementations: `InMemoryStorageAdapter` (tests) and `PostgresStorageAdapter` (production). Swapping to Postgres in Task 15 required zero changes to `src/ingest.py` or the routers.
- **`Fetcher`** (`contracts/fetcher.py`) — `fetch(url) -> FetchResult`. `FetcherRegistry` maps a `source_id` to a concrete fetcher (`web.py`, `github.py`); unknown or auth-required sources simply skip fetching. The web fetcher applies SSRF egress protection (http/https scheme allowlist, public-address validation that blocks private/reserved/link-local/CGN/metadata IPs, per-hop redirect revalidation, pinned connections) and rejects a blocked or malformed URL as a `BlockedURLError` (a `FetchError` subclass). Fetch failures raise `FetchError`, which `ingest_doc` catches and turns into a warning — a doc is still created with the URL as its title.
- **`DirectoryClient`** (`contracts/directory.py`) — `get_team_label` / `get_person_label`, backed by `HttpDirectoryClient` calling UTMIST's team-tracking service. When team-tracking is unreachable, `DirectoryUnavailable` is caught and ingest degrades: the id is stored, the label is left null with a warning, and a later read/update backfills the label once the directory is reachable again (see `_backfill_labels` in `src/api/routers/docs.py`).

## Auth model

Auth is **Level 2** (scoped API keys), matching team-tracking:

- Every request needs `X-API-Key`. Keys are either the shared bootstrap env key (`API_KEY`, scope: `admin`) or a per-consumer key issued via the `doc-keys` CLI, stored as an Argon2 hash with a `docs:{read,write}` or `admin` scope set.
- There is **no `X-Actor` header** — the actor stamped on `created_by`/`updated_by`/audit log entries is always the authenticated key's own name (`AuthedKey.name`). A caller cannot claim to be someone else; this is the "attested actor" model.
- `require_scope("docs:read")` / `require_scope("docs:write")` gate each route; `admin` is a wildcard scope that satisfies any check.
- `AuditLogMiddleware` (imported from `platform_auth` directly, wired in `src/api/app.py`) logs every request with the resolved actor.
- Manage keys with the CLI:
  ```bash
  uv run doc-keys issue --name my-consumer --scopes docs:read docs:write
  uv run doc-keys list --active-only
  uv run doc-keys revoke <api_key_id>
  ```

## API at a glance

All endpoints require `X-API-Key` with the appropriate scope.

| Method | Path | Scope | Description |
|--------|------|-------|-------------|
| POST | `/docs` | `docs:write` | Ingest a URL (idempotent; 200 if already catalogued, 201 if new) |
| GET | `/docs` | `docs:read` | List docs (`?owning_team_id=&owning_person_id=&source_id=&tag=&active_only=true`) |
| GET | `/docs/{id}` | `docs:read` | Get one doc (backfills owner labels if previously unresolved) |
| PATCH | `/docs/{id}` | `docs:write` | Update a doc (title, description, owner ids, active flag) |
| POST | `/docs/{id}/tags` | `docs:write` | Add a tag |
| DELETE | `/docs/{id}/tags/{tag}` | `docs:write` | Remove a tag |
| POST | `/docs/{id}/refetch` | `docs:write` | Re-run content fetch for an existing doc |
| GET | `/sources` | `docs:read` | List sources (`?active_only=false`) |
| GET | `/sources/{id}` | `docs:read` | Get one source |

Machine-readable OpenAPI schema: `GET /openapi.json`. Interactive Swagger UI: `GET /swagger`.

## Testing

The test suite has two modes:

**Fast (in-memory, no Docker required):**
```bash
uv run pytest --ignore=tests/test_postgres_adapter.py -q
```

This runs **59 tests** using `InMemoryStorageAdapter` injected via FastAPI's `dependency_overrides`, plus fakes for `Fetcher` and `DirectoryClient`. `tests/conftest.py` provides `build_seed_sources()`, a shared 8-source seed matching migration 002, used across the in-memory adapter, ingest, API, and OpenAPI tests. Runs in well under a second.

Running the whole suite *without* `RUN_PG_TESTS` reports **59 passed, 7 skipped** — the 7 skipped are the Postgres integration tests in `tests/test_postgres_adapter.py`.

**Full (includes Postgres integration):**
```bash
docker compose up -d postgres
RUN_PG_TESTS=1 uv run pytest -q
```

`tests/test_postgres_adapter.py` runs the same behavioral assertions against a live Postgres instance and is gated behind the `RUN_PG_TESTS=1` environment variable so it's skipped by default (and in the fast suite above). Requires `DATABASE_URL` in `.env` pointing at the running container on port 5434.

Lint before committing:

```bash
uv run ruff check .
uv run ruff format --check .
```

## Further documentation

| Doc | Audience | Contents |
|-----|----------|----------|
| [`docs/API.md`](docs/API.md) | Consumers (bots, dashboards, HTTP clients) | Every endpoint, auth, scopes, ingest idempotency, degrade behavior, error semantics |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Contributors | The three Protocols, adapters, ingest orchestrator, URL normalization, fetcher registry, directory client, data model |
| [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md) | Contributors | Task walkthroughs: add an endpoint, add a fetcher, add an adapter method, write a migration, run the tests |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Operators | Running in prod: Postgres, env vars, migrations, issuing keys, enabling fetchers, the directory dependency |

## Status

v1 covers: the docs + sources registry, idempotent ingest with DB-enforced dedup (partial unique index on active URLs), best-effort content fetching with SSRF egress protection and graceful degradation, team-tracking-backed ownership with label backfill, tag management, soft delete (`active` flag), Level 2 scoped API keys with an attested-actor audit trail, and both storage adapters — 59 passing fast tests (plus 7 Postgres integration tests behind `RUN_PG_TESTS`).

**Deferred (per design §3/§11 non-goals):**

- Full-text / semantic search over `content_snapshot` — the snapshot is stored for future indexing but no search endpoint exists yet.
- Scheduled/background refetching — `POST /docs/{id}/refetch` is on-demand only; no cron or queue re-fetches stale snapshots.
- Additional fetchers — only `web` and `github` fetchers exist; Google Drive/Docs/Sheets/Slides, Notion, and YouTube are registered as sources but have no fetcher implementation yet (auth-gated sources skip fetching by design).
- Bulk import — ingest is one URL per request; no CSV/bulk endpoint.
- Pagination — list endpoints return all matching rows, adequate for current catalog size.
- Admin UI — no dashboard; catalog browsing goes through the API or a future thin client.
