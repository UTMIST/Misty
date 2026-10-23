# team-tracking

Source-of-truth HTTP API for UTMIST's directory: people, teams, roles, memberships, external identity mapping, and scoped API-key auth.

## What this service does

UTMIST runs a rotating volunteer org — people join teams, move between roles, and eventually hand off to successors. Without a canonical record of who is on what team right now (and who was on it six months ago), every downstream system has to guess. team-tracking is that canonical record — the **source of truth** for the directory.

It answers questions like "who is currently on the Partnerships team," "who were the leads during fall semester," "is Alex an admin of the Events team right now," and "which person owns this Discord account." Downstream systems — the [documentation-system](../documentation-system) catalog, a future Discord bot, dashboards, sync jobs — call this API rather than each maintaining their own roster.

Two design choices shape everything:

- **Membership rows are never deleted.** When someone leaves a team, their row gains an `ended_at` date. The full history of who held what role survives leadership transitions, and point-in-time queries ("roster as of 2024-12-15") work without special logic.
- **External accounts are mapped, not embedded.** A person's Discord/GitHub/UofT-email accounts live in a `person_identifiers` table keyed by a `providers` vocabulary — so the Discord bot can reverse-look-up a person from a raw snowflake without the base `people` table ever gaining an integration-specific column.

## The API-only principle

Every consumer talks to team-tracking over HTTP and treats its internals as a black box. The service also runs optional [managed Google Groups reconciliation](docs/ARCHITECTURE.md#managed-google-groups) in background tasks after directory writes. This consumer boundary is deliberate:

- The directory's data model stays independent of any one integration's details.
- Consumers can be written in any language, deployed anywhere, and revoked individually.
- The service can be tested, versioned, and redeployed without coordinating with its consumers.

External consumers (the Discord bot, docs catalog, dashboards) speak the published HTTP API — they never import this codebase. Managed Google Groups are the built-in integration; when configured, directory writes can trigger Google mutations inside the API process.

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

# 3. Apply database migrations (creates all 9 tables + seeds)
uv run alembic upgrade head

# 4. Start the API server
uv run uvicorn src.api.app:app --reload --port 8000
```

> The repo is a single [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/) (root `pyproject.toml` with `[tool.uv.workspace] members = ["services/*", "packages/*"]`, one root `uv.lock`). team-tracking depends on the shared `platform-auth` package (`[tool.uv.sources] platform-auth = { workspace = true }`) but the commands above are unchanged — `uv sync`, `uv run pytest`, `uv run alembic` still work exactly as shown when run from this directory.

The API is now at `http://localhost:8000`. Interactive Swagger UI is at `http://localhost:8000/docs`; the machine-readable schema is at `http://localhost:8000/openapi.json`.

### Environment tiers (`TT_ENV`)

`TT_ENV` declares which environment this team-tracking instance represents:

- `local` (default) — safe for spoofable dev keys.
- `staging` — refuses to boot with the built-in bootstrap key; permits `dev:spoof`-scoped keys.
- `production` — refuses to boot with the built-in bootstrap key and refuses to issue or accept `dev:spoof`-scoped keys.

Set it in `.env`:

```bash
TT_ENV=production
```

The `dev:spoof` scope is a development affordance used by the discord-bot
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
- `people:elevate` — additionally required for non-`member` `access_level` on `POST /people`, or any non-null `access_level` on `PATCH /people/{id}` (including demotion or restating the current value). The `admin` wildcard satisfies it; see [the API contract](docs/API.md#patch-peopleperson_id).
- `teams:read`, `teams:write`
- `memberships:read`, `memberships:write`
- `identifiers:read`, `identifiers:write`
- `channels:read`, `channels:write`
- `providers:read`
- `role_kinds:read`
- `admin` — wildcard, grants all scopes
- `dev:spoof` — permitted in local/staging; issuance and use refused against `TT_ENV=production`

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the full auth model, scopes, rotation runbook, and audit-log integration.

## Repo layout

```
team-tracking/
├── contracts/              The domain boundary — no framework imports
│   ├── types.py            Pydantic models (Person, Team, RoleKind, TeamMembership,
│   │                       Provider, PersonIdentifier, ApiKey) + Create/Update DTOs
│   ├── storage.py          StorageAdapter Protocol — the interface the API depends on
│   └── groups.py           GroupProvider Protocol — managed Google Groups
│
├── src/
│   ├── api/                FastAPI application
│   │   ├── app.py          App factory; mounts resource routers + audit middleware
│   │   ├── auth.py         Thin shim over the shared `platform_auth` package: require_scope, get_actor (attested actor)
│   │   ├── hashing.py      Thin shim over `platform_auth`: argon2 key hashing + tt_<prefix>_<secret> generation
│   │   ├── deps.py         get_storage() dependency (injects the Postgres adapter)
│   │   └── routers/        One file per resource:
│   │                       people, teams, role_kinds, memberships, providers, identifiers, channels, api_keys
│   │
│   ├── storage/            StorageAdapter implementations
│   │   ├── schema.py       SQLAlchemy Core table definitions (9 tables)
│   │   ├── in_memory.py    InMemoryStorageAdapter — used in tests + prototyping
│   │   └── postgres.py     PostgresStorageAdapter — used in production
│   │
│   ├── providers/
│   │   └── google_groups.py  GoogleGroupsProvider — Cloud Identity + Groups Settings APIs
│   ├── google_groups.py    sync_team(): reconcile a team's Google Group with its members
│   ├── groups_cli.py       team-tracking-groups CLI (backfill / scheduled sync)
│   ├── cli.py              team-tracking-keys CLI (issue / list / revoke API keys)
│   └── config.py           Settings (DATABASE_URL, API_KEY, GOOGLE_*) loaded from environment
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
│       ├── 007_membership_no_overlap.py membership temporal-overlap EXCLUDE constraint
│       ├── 008_channel_team_access.py   channel_team_access (Discord channel → teams)
│       ├── 009_team_google_groups.py    team_google_groups (team → managed Google Group)
│       ├── 010_pending_group_changes.py  persist unconfirmed membership writes
│       └── 011_remove_retired_provider.py  remove retired provider and identifiers
│
├── tests/                  Two-mode test suite (see Testing below)
│
├── docs/
│   ├── API.md              Consumer-facing endpoint reference
│   ├── ARCHITECTURE.md     Contributor orientation: boundaries, adapters, auth, data model
│   ├── CONTRIBUTING.md     Task walkthroughs: add an endpoint, adapter method, migration, tests
│   └── DEPLOYMENT.md       Ops reference: security posture, key management CLI, audit log
│
└── deploy/                 Historical VPS reverse-proxy examples (Caddyfile, nginx.conf).
                            Superseded by Railway — kept for reference in case we ever
                            self-host. See docs/RAILWAY-DEPLOYMENT.md for the live path.
```

**Nine tables:** `people`, `teams`, `role_kinds`, `team_memberships`, `api_keys`, `providers`, `person_identifiers`, `channel_team_access`, `team_google_groups`.
See the [API reference](docs/API.md) for routes and scopes and the [migration inventory](docs/ARCHITECTURE.md#migrations-layout) for revisions, including [migration 011's cleanup/recovery caveat](docs/DEPLOYMENT.md#registry-cleanup-migration-011).

**Dependency direction:** `contracts/` imports nothing from `src/`. The API layer imports only from `contracts/` and `src/config`. The storage layer imports `contracts/` for types and defines its own schema. Nothing imports from `src/storage/` except `src/api/deps.py` (the single wiring point). This is the **Protocol boundary** — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Managed Google Groups use the five optional `GOOGLE_*` variables in `.env.example`.
The owning account's refresh token needs both `cloud-identity.groups` and
`apps.groups.settings`. Sync configures and verifies external-member settings
before additions, including for empty groups; settings failure holds additions
while removals continue. See [credentials and rollout](docs/DEPLOYMENT.md#managed-google-groups).

## API at a glance

The resource endpoints below require an `X-API-Key`; health, schema and interactive API documentation routes are public. The actor stamped into `created_by`/`updated_by` is the **key's name** (attested by the key, not a self-declared header).

| Method | Path | Scope | Description |
|--------|------|-------|-------------|
| POST | `/people` | `people:write` (+`people:elevate` for non-`member` `access_level`) | Create a person |
| GET | `/people` | `people:read` | List people (`?active_only=`) |
| GET | `/people/{id}` | `people:read` | Get one person |
| PATCH | `/people/{id}` | `people:write` (+`people:elevate` for any non-null `access_level`) | Update a person |
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
| GET | `/teams/google-groups` | `teams:read` | Every team's managed Google Group and sync status |
| GET | `/teams/{id}/google-group` | `teams:read` | One team's Google Group mapping |
| POST | `/teams/{id}/google-group/sync` | `teams:write` | Create/reconcile the team's Google Group now |
| GET | `/role_kinds` | `role_kinds:read` | List role kinds |
| GET | `/role_kinds/{id}` | `role_kinds:read` | Get one role kind |
| GET | `/providers` | `providers:read` | List identity providers |
| GET | `/providers/{id}` | `providers:read` | Get one provider |
| POST | `/memberships` | `memberships:write` | Create a membership |
| GET | `/memberships` | `memberships:read` | List memberships (filterable) |
| GET | `/memberships/{id}` | `memberships:read` | Get one membership |
| PATCH | `/memberships/{id}` | `memberships:write` | Update a membership |
| POST | `/memberships/{id}/end` | `memberships:write` | End a membership (set `ended_at`) |
| GET | `/channels/{guild_id}/{channel_id}/teams` | `channels:read` | A channel's active teams (empty if unconfigured) |
| PUT | `/channels/{guild_id}/{channel_id}/teams` | `channels:write` | Replace a channel's teams |
| POST | `/channels/{guild_id}/{channel_id}/teams/append` | `channels:write` | Atomically union teams onto a channel |
| DELETE | `/channels/{guild_id}/{channel_id}/teams` | `channels:write` | Clear a channel's teams |

See [docs/API.md](docs/API.md) for full request/response shapes, query parameters, error codes, and curl examples.

### Behavior notes

**Access-level changes (`people:elevate`).** Setting a non-`member` `access_level` on `POST /people`, or supplying any non-null `access_level` on `PATCH /people/{id}`, additionally requires `people:elevate` (the `admin` wildcard satisfies it). This includes demotion and restating the current value. A plain `people:write` key is rejected with **403** — `access_level` is a privilege grant that downstream consumers trust for authorization.

**Membership temporal integrity.**

- Creating (`POST /memberships`) or updating (`PATCH /memberships/{id}`) a membership whose active date range **temporally overlaps** an existing membership for the same `(person_id, team_id)` is rejected with **400**. This is enforced at the database by a `btree_gist` `EXCLUDE` constraint (migration 007). "Active/overlap" uses the half-open range `[started_at, ended_at)`, with an open-ended (`NULL`) `ended_at` treated as infinity; because the upper bound is exclusive, ending a membership and re-adding the person on the **same day** does not overlap and is allowed.
- Foreign-key violations on membership create/update (unknown `person_id`, `team_id`, or `role_kind_id`) now return **400** (previously surfaced as a 500).
- `GET /memberships?active_only=true` filters by `ended_at IS NULL OR ended_at > current_date` only; it does not check `started_at`. Use `as_of=<today>` for a current roster, so future-starting memberships are excluded too.

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

- [docs/API.md](docs/API.md) — consumer-facing endpoint reference (scopes, errors, curl)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — contributor orientation: Protocol boundary, adapters, temporal memberships, Level-2 auth, data model
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) — task walkthroughs for adding endpoints, adapter methods, migrations
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — ops reference: security posture, key management CLI, audit log

Machine-readable OpenAPI schema: `GET /openapi.json`. Interactive Swagger UI: `GET /docs`.

## Status

The directory has two storage adapters, scoped API-key auth, identity mapping, and optional managed Google Groups. See the [API reference](docs/API.md), [data model](docs/ARCHITECTURE.md#the-data-model-9-tables), and [migration inventory](docs/ARCHITECTURE.md#migrations-layout) for the current surface.

**Not implemented (by design):**

- **General connector framework** — external integrations use the HTTP API; the built-in Google Groups reconciler is described [above](#the-api-only-principle).
- **Permission enforcement** — the API exposes primitives (memberships, admin flag) from which downstream systems derive access rules. No policy engine lives here.
- **Admin UI** — officer edits go through whatever admin surface is chosen (NocoDB, Directus, direct SQL, or a custom app).
- **Pagination** — list endpoints return all matching rows. Adequate for UTMIST's roster size; add limit/offset when needed.
