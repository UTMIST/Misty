# Architecture

Orientation for contributors: how `team-tracking` is put together and *why* it is shaped this way. Read this after the [README](../README.md) and before [CONTRIBUTING.md](CONTRIBUTING.md) (which turns these ideas into step-by-step tasks).

For the consumer-facing endpoint reference, see [API.md](API.md).

## The big picture

team-tracking is the **source of truth** for UTMIST's directory, and it is **API-only**: nothing runs inside it. Every consumer — the docs catalog, a future Discord bot, dashboards — talks to it over HTTP and treats its internals as a black box. That constraint is what lets the internals stay simple and swappable.

Internally the code is three layers with a strict dependency direction:

```
contracts/          Pure Python. Pydantic models + StorageAdapter Protocol.
                    No framework imports. The domain boundary.
        │
        ▼
src/api/            FastAPI routers + auth + audit middleware + DI wiring.
                    Imports contracts/ only. Never imports src/storage/ directly.
        │
        ▼
src/storage/        Concrete adapters (InMemory, Postgres).
                    Imports contracts/ for types; owns the DB schema.
```

The invariant: `contracts/` knows nothing about FastAPI, SQLAlchemy, or Postgres; the API layer knows nothing about which storage adapter is running. The single place they are wired together is `src/api/deps.py`, via FastAPI dependency injection.

## The Protocol boundary

`contracts/storage.py` defines `StorageAdapter` as a `typing.Protocol` — a structural interface listing every method the API needs from persistence (create/get/list/update people, teams, memberships, identifiers, and API keys). The API layer types its dependency as `StorageAdapter` and never imports a concrete class.

This boundary does two jobs:

- **Portability.** Any class implementing these methods with the same semantics *is* a valid backend — no inheritance required (Python structural typing). Swapping Postgres for SQLite, a mock, or a read-replica wrapper touches only `deps.py`.
- **A written contract.** The Protocol's docstrings are the spec for adapter behavior (e.g. "`list_*` returns every match; pagination is not implemented", "records are never hard-deleted", "`create_person_identifier` raises `ValueError` on a duplicate link"). Both adapters must honor them identically.

`contracts/types.py` defines every domain model as a Pydantic `BaseModel`. All directory records inherit from `DirectoryBase`, which carries the four audit fields (`created_at`, `updated_at`, `created_by`, `updated_by`). Input DTOs (`PersonCreate`, `PersonUpdate`, `TeamCreate`, `PersonIdentifierCreate`, …) live here too — they are what the API deserializes from request bodies, and they use `extra="forbid"` so unknown fields are rejected rather than silently dropped.

## Two storage adapters, and why

Both implement `StorageAdapter`; the app picks one at the wiring point.

- **`InMemoryStorageAdapter`** (`src/storage/in_memory.py`) keeps everything in plain Python dicts. It enforces the *same* invariants as Postgres (email uniqueness, slug uniqueness, FK checks on membership create, identifier-link uniqueness). It is not thread-safe and not persistent — it exists so the entire test suite can run in-process, with no database, in a few seconds. Tests inject it via `app.dependency_overrides[get_storage]`.
- **`PostgresStorageAdapter`** (`src/storage/postgres.py`) is the production backend. It takes a SQLAlchemy `Engine` at construction (it never creates its own), opens a connection per method, runs a typed SQL expression, and converts each result row to a Pydantic model via a `_*_row_to_model` helper.

Having two adapters behind one Protocol is the payoff of the boundary: fast, deterministic tests against the in-memory adapter, plus a smaller integration suite (`tests/test_postgres_adapter.py`) that replays the same behavioral assertions against real Postgres to catch anything dict-land can't model (real FK enforcement, `citext`, unique constraints).

### SQLAlchemy Core, not the ORM

The Postgres adapter uses **SQLAlchemy Core** — `Table` objects and SQL expression constructs — not the ORM. There are no mapped classes, no session/identity-map, no lazy loading. Each method is an explicit `select`/`insert`/`update`/`delete` against the tables in `src/storage/schema.py`, and the result is hand-converted to a Pydantic model. The reasons:

- The Pydantic models in `contracts/` are already the domain objects; an ORM layer would be a second, redundant object model.
- Explicit SQL keeps behavior obvious and debuggable for contributors who may be new to SQLAlchemy — what you read is what runs.
- `schema.py` stays the single, readable source of truth for the columns.

`schema.py` defines the tables but does **not** create them at runtime. Alembic migrations create the schema; `schema.py` and the migrations must be kept in sync by hand (see [CONTRIBUTING.md](CONTRIBUTING.md)).

## Temporal memberships

`team_memberships` rows are **never hard-deleted**. A membership has `started_at` (defaults to today) and a nullable `ended_at`. When someone leaves a team, `ended_at` is set to a date rather than the row being removed. Consequences:

- The full membership history survives leadership transitions — essential for a rotating org.
- Point-in-time queries work off the main table with no separate audit log: `GET /memberships?...&as_of=2024-12-15` returns rows where `started_at <= as_of AND (ended_at IS NULL OR ended_at > as_of)`.
- `POST /memberships/{id}/end` exists as a semantic helper over `PATCH` because *closing* a membership is a distinct intent from *editing* one.

There is intentionally **no** unique constraint on `(person_id, team_id)`. Overlapping rows are valid during role transitions — a person can be a member through November and a lead from December, which is two rows with adjacent or overlapping date ranges.

The same "never hard-delete" rule applies to `people` and `teams` (soft-retire with `active = false`) and `api_keys` (revoke sets `revoked_at`). The one deliberate exception is `person_identifiers`: unlinking hard-deletes the row, because an identity mapping is *current state*, not history.

## Level-2 authentication

The auth machinery itself lives in the shared `platform_auth` package (`packages/auth/`) — a pure leaf package with no imports of any service's `src/` or `contracts/`, shared with documentation-system. `src/api/auth.py` and `src/api/hashing.py` are thin (~15-line) shims that call `platform_auth`'s `build_auth(...)` factory, binding team-tracking's `tt_` key envelope and config (`enable_dev_spoof=True`, `bootstrap_honors_x_actor=True`, `dev_spoof_reject_log_fields={"tt_env": "production"}`), and re-export the same names (`require_api_key`, `require_scope`, `get_actor`). `AuditLogMiddleware` binds nothing per-service, so `app.py` imports it from `platform_auth` directly. The auth behavior and contract are unchanged by this move. The model is still "Level 2": DB-issued, per-consumer, scoped keys with a cryptographically attested actor and an audit log.

**Key format and storage.** A key is `tt_<prefix>_<secret>` — an 8-char public prefix plus a secret. Only an **argon2 hash** of the full key is stored (in the `api_keys` table), alongside the plaintext prefix. On a request, `require_api_key` parses the prefix, looks up the row, and argon2-verifies the candidate against the stored hash. The plaintext is shown once at issuance (by the `team-tracking-keys` CLI) and is never recoverable. All auth failures return an identical `401` — the code never leaks which check failed.

**Scoped access.** Each key carries a list of scopes (`people:read`, `memberships:write`, …; `admin` is a wildcard). Endpoints declare their requirement with the `require_scope("<scope>")` dependency factory; a valid key that lacks the scope gets `403`. This bounds the blast radius of a leaked key — a `people:read`-only Discord-bot key cannot create memberships or mutate anything.

**Attested actor.** `get_actor` returns the **name of the key** that made the request, and that is what gets stamped into `created_by`/`updated_by`. Callers do not (and cannot) self-declare identity: the legacy `X-Actor` header is honored *only* for the deprecated env bootstrap key and ignored for DB-issued keys, so a leaked key cannot impersonate someone else.

**Env bootstrap key.** The `API_KEY` setting is a deprecated grace-period key: if set and matched (constant-time compare), it grants `admin` scope so a brand-new deployment can reach the API before any DB keys exist. Every real consumer should have its own DB-issued key.

**Audit middleware.** `AuditLogMiddleware` (from the shared `platform_auth` package, wired in directly in `src/api/app.py`) emits exactly one JSON line to stdout per request — method, path, status, duration, the resolved `key_name`, `is_bootstrap`, and the real client IP (read from `X-Real-IP`/`X-Forwarded-For` set by the reverse proxy). Auth stashes the resolved key on `request.state.auth_key`; the middleware reads it after the handler runs. Logging never fails the request. Ship stdout to any aggregator; grep by `key_name`/`status` for investigations (see [DEPLOYMENT.md](DEPLOYMENT.md)).

**Self-introspection.** `GET /api-keys/self` returns the calling key's `{name, scopes}` sorted alphabetically. No additional scope required. The discord-bot uses this at startup to decide whether it holds `dev:spoof` and can therefore enable its "act as any Discord ID" web playground mode.

**The `dev:spoof` scope.** A new dev-only scope guarded by the environment tier config (`TT_ENV`, see [DEPLOYMENT.md](DEPLOYMENT.md)). The scope itself gates nothing on team-tracking's HTTP surface — no endpoint requires it. Its purpose is to be a *declaration* that a caller (typically the discord-bot playground) intends to run in a spoofable dev environment. Two defense-in-depth guards fire only when `TT_ENV=production`:
1. The `team-tracking-keys issue` CLI refuses to grant `dev:spoof` on issuance.
2. Request-time auth (enforced in the shared `platform_auth` package, bound with team-tracking's `dev_spoof_reject_log_fields={"tt_env": "production"}` via `src/api/auth.py`) 403s any request whose key has literal `dev:spoof` scope — the `admin` wildcard does NOT satisfy this literal check, precisely so an accidental admin+dev:spoof key against production is trapped, not bypassed.

The result: a `dev:spoof` key cannot be created against a production directory, and if one leaks in via a copied backup or bug, every request it makes is refused at the door. Local dev (`TT_ENV=local` or unset) permits the scope freely.

## The data model (7 tables)

All tables carry the four audit columns (`created_at`, `updated_at` as `timestamptz`; `created_by`, `updated_by` as text). Only the distinguishing columns are shown below.

### `people`
| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | `gen_random_uuid()` default |
| `display_name` | text NOT NULL | Free-form; handles all name conventions |
| `primary_email` | citext NOT NULL UNIQUE | Case-insensitive; normalized to lowercase on write |
| `active` | boolean NOT NULL | Default `true`; soft-retirement only |

`citext` (a Postgres extension enabled by migration 001) makes email equality/uniqueness case-insensitive at the DB level; the Pydantic validator also lowercases on write, and the in-memory adapter normalizes manually — belt and suspenders.

### `teams`
| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | `gen_random_uuid()` default |
| `slug` | text NOT NULL UNIQUE | Pattern `[a-z0-9_.]+`, enforced by the Pydantic DTO |
| `label` | text NOT NULL | Human-readable name |
| `description` | text NULL | One-line summary |
| `parent_id` | UUID NULL FK → teams.id | Self-reference for hierarchy; null = top-level |
| `active` | boolean NOT NULL | Default `true` |

### `role_kinds`
| Column | Type | Notes |
|--------|------|-------|
| `id` | text PK | Slug, e.g. `executive`, `director`, `lead`, `member` |
| `label` | text NOT NULL | Display label |
| `active` | boolean NOT NULL | Default `true` |

Slug-as-PK because downstream rules read `role_kind_id = 'lead'` directly; a surrogate key would force a join to reach the meaningful value. Seed rows come from migration 002.

### `team_memberships`
| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | `gen_random_uuid()` default |
| `person_id` | UUID NOT NULL FK → people.id | |
| `team_id` | UUID NOT NULL FK → teams.id | |
| `role_kind_id` | text NOT NULL FK → role_kinds.id | Default `'member'` |
| `is_team_admin` | boolean NOT NULL | Default `false`; orthogonal to role |
| `started_at` | date NOT NULL | Default `CURRENT_DATE` |
| `ended_at` | date NULL | Null = currently active |

Indexes: `(team_id, ended_at)`, `(person_id, ended_at)`, `(started_at, ended_at)`. `is_team_admin` is a separate boolean because admin authority (can edit the team) is orthogonal to seniority (`role_kind`) — the Partnerships lead may not be its admin, and a member might be.

### `api_keys`
| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `name` | text NOT NULL UNIQUE | Becomes the attested actor |
| `prefix` | text NOT NULL UNIQUE | Public 8-char lookup key |
| `key_hash` | text NOT NULL | argon2 hash of the full key; never returned by any API |
| `scopes` | text[] NOT NULL | e.g. `{people:read, memberships:read}` |
| `active` | boolean NOT NULL | Default `true` |
| `revoked_at` | timestamptz NULL | Set on revoke (soft-delete) |
| `last_used_at` | timestamptz NULL | Best-effort touch on each successful auth |

### `providers`
| Column | Type | Notes |
|--------|------|-------|
| `id` | text PK | Slug, e.g. `discord`, `github`, `notion`, `uoft_email`, `email` |
| `label` | text NOT NULL | Display label |
| `active` | boolean NOT NULL | Default `true` |

The controlled vocabulary of external-identity types. Read-only through the API; the first four are seeded by migration 004, `email` by migration 006.

### `person_identifiers`

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `person_id` | UUID NOT NULL FK → people.id | |
| `provider` | text NOT NULL FK → providers.id | |
| `external_id` | text NOT NULL | The stable external identifier (snowflake, username, email); lowercased before storage when `provider = 'email'` |
| `handle` | text NULL | Optional human-readable handle |

Two unique constraints: a partial index named `uq_person_identifiers_person_provider` on `(person_id, provider) WHERE provider <> 'email'` — one account per provider per person, for every provider *except* `email` — and `(provider, external_id)` — one external account maps to one person. That second index also powers the Discord-bot hot path `GET /people/by-identifier/{provider}/{external_id}`. External identifiers live in this table, *not* as columns on `people`, so adding a new integration is a data change (insert a provider row), not a schema migration.

**The `email` provider is multi-valued.** Every other provider keeps the "one link per person" invariant; `email` is the deliberate exception, because a person legitimately owns several verified addresses (personal, alumni, work) beyond their single `primary_email` on `people`. The partial index above is what makes this possible without weakening the invariant for any other provider: it is the *same constraint name* as the original table-wide unique constraint, just scoped with a `WHERE` clause, so no application code needs to know the shape changed. New `email` identifiers are created only through `add_person_email` / `POST /people/{id}/emails` (see [API.md](API.md)), which normalizes (lowercases) the address and checks it against both `primary_email` and existing `email` identifiers before insert; adding an already-linked address for the same person is idempotent (returns the existing row) rather than erroring. The generic `create_person_identifier` / `update_person_identifier` / `delete_person_identifier` methods — and their `POST`/`PATCH`/`DELETE /people/{id}/identifiers/{provider}` endpoints — explicitly reject `provider = 'email'`.

### Convention: named exec seats

UTMIST has titles like "President", "VP Partnerships", "Treasurer". The schema models these through `team_memberships`, not a `positions` table. A **team-scoped** seat (VP Partnerships) is a membership on that team with `role_kind_id = 'executive'`; an **org-wide** seat (President) is a membership on a top-level `leadership` team. The title is derived from the `(role_kind, team)` composite — the string is never stored — so adding a position is a data operation, not a migration.

## Migrations layout

Alembic manages the Postgres schema; migrations are written by hand and kept in sync with `schema.py`. Six exist:

- **`001_initial_schema.py`** — enables `citext`; creates `people`, `teams`, `role_kinds`, `team_memberships` and their indexes.
- **`002_seed_role_kinds.py`** — inserts `executive`, `director`, `lead`, `member`.
- **`003_api_keys.py`** — creates the `api_keys` table (Level-2 auth).
- **`004_person_identifiers.py`** — creates `providers` and `person_identifiers`; seeds the four providers (`discord`, `github`, `notion`, `uoft_email`).
- **`005_person_access_level.py`** — adds `people.access_level` (`member`/`admin`/`superuser`, default `member`) with a check constraint.
- **`006_email_provider_multivalued.py`** — seeds the `email` provider (five seed providers total) and swaps the table-wide `UNIQUE(person_id, provider)` constraint for a same-named partial unique index (`WHERE provider <> 'email'`), so every provider except `email` still gets "one link per person" while `email` becomes multi-valued. Downgrade fails loud (raises `RuntimeError`) when any `provider='email'` identifiers exist, rather than silently deleting verified addresses to make room for the strict constraint.

Apply with `uv run alembic upgrade head`; roll back with `uv run alembic downgrade base`.

## Extending the system

Concrete, step-by-step walkthroughs — add an endpoint, add a storage-adapter method, write a migration, run the two-mode test suite, lint — live in [CONTRIBUTING.md](CONTRIBUTING.md). The one rule to internalize first: **define the contract before the implementation**. Add the method to the `StorageAdapter` Protocol, implement it in *both* adapters, then expose it through a router. The Protocol is the hinge everything else turns on.

## Non-goals

Explicitly out of scope:

- No Discord/Google/GitHub/Notion sync jobs — connectors are separate processes that speak the HTTP API.
- No permission-enforcement engine — downstream systems derive access from membership data.
- No login/UI for officers editing directory data — handled by whatever admin surface is chosen.
- No content storage — the directory is identity and structure only.
- No pagination — list endpoints return all matching rows; add limit/offset if the roster grows past a few hundred.
