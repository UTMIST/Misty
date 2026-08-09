# team-tracking

Source-of-truth HTTP API for UTMIST's directory: people, teams, roles, memberships, external identity mapping, and scoped API-key auth.

## What this service does

UTMIST runs a rotating volunteer org — people join teams, move between roles, and eventually hand off to successors. Without a canonical record of who is on what team right now (and who was on it six months ago), every downstream system has to guess. team-tracking is that canonical record — the **source of truth** for the directory.

It answers questions like "who is currently on the Partnerships team," "who were the leads during fall semester," "is Alex an admin of the Events team right now," and "which person owns this Discord account." Downstream systems — the [documentation-system](../documentation-system) catalog, a future Discord bot, dashboards, sync jobs — call this API rather than each maintaining their own roster.

Two design choices shape everything:

- **Membership rows are never deleted.** When someone leaves a team, their row gains an `ended_at` date. The full history of who held what role survives leadership transitions, and point-in-time queries ("roster as of 2024-12-15") work without special logic.
- **External accounts are mapped, not embedded.** A person's Discord/GitHub/Notion/UofT-email accounts live in a `person_identifiers` table keyed by a `providers` vocabulary — so the Discord bot can reverse-look-up a person from a raw snowflake without the base `people` table ever gaining an integration-specific column.

## The API-only principle

**Nothing runs inside team-tracking.** It is an HTTP API and nothing else — no in-process consumers, no embedded jobs, no UI. Every consumer talks to it over HTTP and treats its internals as a black box. This is deliberate:

- The directory's data model stays independent of any one integration's details.
- Consumers can be written in any language, deployed anywhere, and revoked individually.
- The service can be tested, versioned, and redeployed without coordinating with its consumers.

Connectors (Discord sync, Google Workspace, etc.) are separate processes that speak the published HTTP API — they never import this codebase.

## Quick start

Prerequisites: Docker, Python 3.11+, [uv](https://github.com/astral-sh/uv).

```bash
# 0. From the repo root, enter the service directory (all commands below run here)
cd services/team-tracking

# 1. Copy environment config and start Postgres
cp .env.example .env
docker compose up -d postgres

# 2. Install dependencies (including dev tools)
uv sync --extra dev

# 3. Apply database migrations (creates all 7 tables + seeds)
uv run alembic upgrade head

# 4. Start the API server
uv run uvicorn src.api.app:app --reload --port 8000
```

> The repo is a single [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/) (root `pyproject.toml` with `[tool.uv.workspace] members = ["services/*", "packages/*"]`, one root `uv.lock`). team-tracking depends on the shared `platform-auth` package (`[tool.uv.sources] platform-auth = { workspace = true }`) but the commands above are unchanged — `uv sync`, `uv run pytest`, `uv run alembic` still work exactly as shown when run from this directory.

The API is now at `http://localhost:8000`. Interactive Swagger UI is at `http://localhost:8000/docs`; the machine-readable schema is at `http://localhost:8000/openapi.json`.

### Environment tiers (`TT_ENV`)

`TT_ENV` declares which environment this team-tracking instance represents:

- `local` (default) — safe for spoofable dev keys.
- `staging` — same behaviour as `local` for now; reserved for future staging playground.
- `production` — refuses to issue or accept `dev:spoof`-scoped keys.

Set it in `.env`:

```bash
TT_ENV=production
```

The `dev:spoof` scope is a local-dev-only affordance used by the discord-bot
web playground to act as arbitrary Discord users. Attempting to issue such a
key against a `TT_ENV=production` instance is refused by the CLI, and any
request presenting one is 403'd at request time — belt-and-suspenders.

The default dev bootstrap key is `dev-api-key-change-me` (set in `.env`). Pass it as `X-API-Key` on every request:

```bash
curl -sS http://localhost:8000/role_kinds \
  -H "X-API-Key: dev-api-key-change-me" | python3 -m json.tool
```

**For production, use per-consumer scoped keys instead of the shared env key.** Issue them via the CLI:

```bash
# Issue a read-only key for a Discord bot
uv run team-tracking-keys issue --name discord-bot --scopes people:read memberships:read identifiers:read
# Prints: tt_<prefix>_<secret>  (shown ONCE — capture it now)

# List existing keys (metadata only, never plaintext)
uv run team-tracking-keys list --active-only

# Revoke a compromised key (soft-delete; history preserved)
uv run team-tracking-keys revoke <api_key_id>
```

Scopes recognized today:

- `people:read`, `people:write`
- `people:elevate` — required to set a non-`member` `access_level` when creating (`POST /people`) or to change `access_level` at all when updating (`PATCH /people/{id}`); a plain `people:write` key gets **403** if the payload would set/change `access_level`. The `admin` wildcard still satisfies it.
- `memberships:read`, `memberships:write`
- `identifiers:read`, `identifiers:write`
- `providers:read`, `providers:write`
- `role_kinds:read`, `role_kinds:write`
- `admin` — wildcard, grants all scopes
- `dev:spoof` — local-dev only (issuance refused against `TT_ENV=production`)

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the full auth model, scopes, rotation runbook, and audit-log integration.

## Repo layout

```
team-tracking/
├── contracts/              The domain boundary — no framework imports
│   ├── types.py            Pydantic models (Person, Team, RoleKind, TeamMembership,
│   │                       Provider, PersonIdentifier, ApiKey) + Create/Update DTOs
│   └── storage.py          StorageAdapter Protocol — the interface the API depends on
│
├── src/
│   ├── api/                FastAPI application
│   │   ├── app.py          App factory; mounts all 6 routers + audit middleware
│   │   ├── auth.py         Thin shim over the shared `platform_auth` package: require_scope, get_actor (attested actor)
│   │   ├── hashing.py      Thin shim over `platform_auth`: argon2 key hashing + tt_<prefix>_<secret> generation
│   │   ├── deps.py         get_storage() dependency (injects the Postgres adapter)
│   │   └── routers/        One file per resource:
│   │                       people, teams, role_kinds, memberships, providers, identifiers
│   │
│   ├── storage/            StorageAdapter implementations
│   │   ├── schema.py       SQLAlchemy Core table definitions (7 tables)
│   │   ├── in_memory.py    InMemoryStorageAdapter — used in tests + prototyping
│   │   └── postgres.py     PostgresStorageAdapter — used in production
│   │
│   ├── cli.py              team-tracking-keys CLI (issue / list / revoke API keys)
│   └── config.py           Settings (DATABASE_URL, API_KEY) loaded from environment
│
├── migrations/             Alembic migrations
│   ├── env.py
│   └── versions/
│       ├── 001_initial_schema.py     people, teams, role_kinds, team_memberships + indexes
│       ├── 002_seed_role_kinds.py    seeds executive / director / lead / member
│       ├── 003_api_keys.py           api_keys table (Level-2 auth)
│       ├── 004_person_identifiers.py providers + person_identifiers; seeds 4 providers
│       ├── 005_person_access_level.py    people.access_level column
│       ├── 006_email_provider_multivalued.py  multi-valued email identifiers
│       └── 007_membership_no_overlap.py membership temporal-overlap EXCLUDE constraint
│
├── tests/                  Two-mode test suite (see Testing below)
│
├── docs/
│   ├── API.md              Consumer-facing endpoint reference (all 26 endpoints)
│   ├── ARCHITECTURE.md     Contributor orientation: boundaries, adapters, auth, data model
│   ├── CONTRIBUTING.md     Task walkthroughs: add an endpoint, adapter method, migration, tests
│   └── DEPLOYMENT.md       Ops reference: security posture, key management CLI, audit log
│
└── deploy/                 Historical VPS reverse-proxy examples (Caddyfile, nginx.conf).
                            Superseded by Railway — kept for reference in case we ever
                            self-host. See docs/RAILWAY-DEPLOYMENT.md for the live path.
```

**Seven tables:** `people`, `teams`, `role_kinds`, `team_memberships`, `api_keys`, `providers`, `person_identifiers`.
**Seven routers, 26 endpoints.** **Seven migrations (001–007);** the latest, 007, adds the membership temporal-overlap constraint.

**Dependency direction:** `contracts/` imports nothing from `src/`. The API layer imports only from `contracts/` and `src/config`. The storage layer imports `contracts/` for types and defines its own schema. Nothing imports from `src/storage/` except `src/api/deps.py` (the single wiring point). This is the **Protocol boundary** — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## API at a glance

Every endpoint requires an `X-API-Key`. The actor stamped into `created_by`/`updated_by` is the **key's name** (attested by the key, not a self-declared header).

| Method | Path | Scope | Description |
|--------|------|-------|-------------|
| POST | `/people` | `people:write` (+`people:elevate` for non-`member` `access_level`) | Create a person |
| GET | `/people` | `people:read` | List people (`?active_only=`) |
| GET | `/people/{id}` | `people:read` | Get one person |
| PATCH | `/people/{id}` | `people:write` (+`people:elevate` to change `access_level`) | Update a person |
| GET | `/people/by-identifier/{provider}/{external_id}` | `identifiers:read` | Reverse lookup → Person |
| GET | `/people/{id}/identifiers` | `identifiers:read` | List a person's linked accounts |
| POST | `/people/{id}/identifiers` | `identifiers:write` | Link an external account |
| PATCH | `/people/{id}/identifiers/{provider}` | `identifiers:write` | Update a link |
| DELETE | `/people/{id}/identifiers/{provider}` | `identifiers:write` | Unlink an account |
| POST | `/teams` | `teams:write` | Create a team |
| GET | `/teams` | `teams:read` | List teams (`?active_only=`) |
| GET | `/teams/by-slug/{slug}` | `teams:read` | Get team by slug |
| GET | `/teams/{id}` | `teams:read` | Get team by UUID |
| PATCH | `/teams/{id}` | `teams:write` | Update a team |
| GET | `/role_kinds` | `role_kinds:read` | List role kinds |
| GET | `/role_kinds/{id}` | `role_kinds:read` | Get one role kind |
| GET | `/providers` | `providers:read` | List identity providers |
| GET | `/providers/{id}` | `providers:read` | Get one provider |
| POST | `/memberships` | `memberships:write` | Create a membership |
| GET | `/memberships` | `memberships:read` | List memberships (filterable) |
| GET | `/memberships/{id}` | `memberships:read` | Get one membership |
| PATCH | `/memberships/{id}` | `memberships:write` | Update a membership |
| POST | `/memberships/{id}/end` | `memberships:write` | End a membership (set `ended_at`) |

See [docs/API.md](docs/API.md) for full request/response shapes, query parameters, error codes, and curl examples.

### Behavior notes

**Access-level changes (`people:elevate`).** Setting a non-`member` `access_level` on `POST /people`, or changing `access_level` on `PATCH /people/{id}`, requires the `people:elevate` scope (the `admin` wildcard satisfies it). A plain `people:write` key is rejected with **403** rather than having the field silently dropped — `access_level` is a privilege grant that downstream consumers trust for authorization.

**Membership temporal integrity.**

- Creating (`POST /memberships`) or updating (`PATCH /memberships/{id}`) a membership whose active date range **temporally overlaps** an existing membership for the same `(person_id, team_id)` is rejected with **400**. This is enforced at the database by a `btree_gist` `EXCLUDE` constraint (migration 007). "Active/overlap" uses the half-open range `[started_at, ended_at)`, with an open-ended (`NULL`) `ended_at` treated as infinity; because the upper bound is exclusive, ending a membership and re-adding the person on the **same day** does not overlap and is allowed.
- Foreign-key violations on membership create/update (unknown `person_id`, `team_id`, or `role_kind_id`) now return **400** (previously surfaced as a 500).
- `GET /memberships?active_only=true` means **currently active** = `ended_at IS NULL OR ended_at > current_date`. A membership with a *future* `ended_at` still counts as active (previously a future end date excluded it immediately).

**Unknown parent team.** `POST /teams` with a nonexistent `parent_id` returns **400** ("unknown parent team"), not a misleading `409 slug already exists`.

## Testing

The suite runs in two modes.

**Fast (in-memory, no Docker required):**

```bash
uv run pytest --ignore=tests/test_postgres_adapter.py
```

Runs against `InMemoryStorageAdapter`, injected into FastAPI via `app.dependency_overrides`. Covers every endpoint (including `/api-keys/self`), auth path (including the `dev:spoof` × `TT_ENV=production` guard), error case, the CLI, hashing, and the audit log. Completes in a few seconds — no database needed.

**Full (adds Postgres integration):**

```bash
docker compose up -d postgres
uv run pytest
```

Runs the fast suite above plus `tests/test_postgres_adapter.py`, which replays the adapter's behavioral assertions against a live Postgres instance. Requires `DATABASE_URL` (in `.env`) pointing at the running container.

Lint and format with ruff:

```bash
uv run ruff check .
uv run ruff format .
```

See [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) for task walkthroughs.

## Where to find things

- [docs/API.md](docs/API.md) — consumer-facing endpoint reference (all 26 endpoints, scopes, errors, curl)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — contributor orientation: Protocol boundary, adapters, temporal memberships, Level-2 auth, data model
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) — task walkthroughs for adding endpoints, adapter methods, migrations
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — ops reference: security posture, key management CLI, audit log

Machine-readable OpenAPI schema: `GET /openapi.json`. Interactive Swagger UI: `GET /docs`.

## Status

Seven tables (`people`, `teams`, `role_kinds`, `team_memberships`, `api_keys`, `providers`, `person_identifiers`), 26 endpoints across 7 routers, two storage adapters, migrations 001–007 (latest: 007, membership temporal-overlap constraint). Level-2 auth (DB-issued scoped argon2 keys + attested actor + audit log) is merged, as is the `person_identifiers`/providers identity-mapping feature.

**Not implemented (by design):**

- **Connectors layer** — no Discord/Google/GitHub/Notion sync. Connectors hang off the HTTP API when built.
- **Permission enforcement** — the API exposes primitives (memberships, admin flag) from which downstream systems derive access rules. No policy engine lives here.
- **Admin UI** — officer edits go through whatever admin surface is chosen (NocoDB, Directus, direct SQL, or a custom app).
- **Pagination** — list endpoints return all matching rows. Adequate for UTMIST's roster size; add limit/offset when needed.
