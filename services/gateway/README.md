# gateway

The **public** external API gateway — a thin, scoped, rate-limited door onto UTMIST's
internal directory for third-party consumers (GitHub Actions, external integrations)
that should never see the internal `team-tracking` API directly.

## What this service does

team-tracking is the internal source of truth for the org, but not every consumer of
that data is inside the org's trust boundary. A GitHub Action, for instance, needs to
turn a GitHub login into a Discord id (to @-mention a reviewer) without holding a
team-tracking key or seeing anything else in the directory.

The gateway exists for exactly that shape of consumer:

- It holds **one internal team-tracking key** (scoped `identifiers:read`) for its own
  outbound calls — external callers never see it.
- It issues and manages its **own, separate registry of external API keys** (via the
  `gateway-keys` CLI), scoped to gateway-specific permissions like `resolve:discord`.
- It exposes a **narrow, curated surface** — today, one endpoint — that returns only
  what the consumer needs (a Discord id), never a raw pass-through of team-tracking's
  response.
- It rate-limits and audit-logs every request, since (unlike the internal services)
  its callers are outside UTMIST's control.

This is the "external door" half of the [access architecture](../../docs/ARCHITECTURE.md):
internal services trust each other via the shared `packages/auth` library and their own
keys; anything reaching in from outside the org goes through the gateway instead.

## Quick start

Prerequisites: Docker, Python 3.11+, [uv](https://github.com/astral-sh/uv).

```bash
# 0. From the repo root, enter the service directory (all commands below run here)
cd services/gateway

# 1. Copy environment config and start Postgres
cp .env.example .env
docker compose up -d postgres

# 2. Install dependencies (including dev tools)
uv sync --extra dev

# 3. Apply database migrations (creates the api_keys table)
uv run alembic upgrade head

# 4. Start the API server
uv run uvicorn src.api.app:app --reload --port 8002
```

> The repo is a single [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/) (root `pyproject.toml` with `[tool.uv.workspace] members = ["services/*", "packages/*"]`, one root `uv.lock`). gateway depends on the shared `platform-auth` package (`[tool.uv.sources] platform-auth = { workspace = true }`) but the commands above are unchanged — `uv sync`, `uv run pytest`, `uv run alembic` still work exactly as shown when run from this directory.

The API is now at `http://localhost:8002`. Interactive Swagger UI is at
`http://localhost:8002/docs`; the machine-readable schema is at
`http://localhost:8002/openapi.json`.

### Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | The gateway's **own** Postgres — it holds only `api_keys` (its external key registry), never a copy of team-tracking's data. |
| `API_KEY` | Env grace-period bootstrap key for `platform_auth` (local dev only). |
| `DIRECTORY_BASE_URL` | team-tracking's base URL — where the gateway makes its **outbound** call. |
| `DIRECTORY_API_KEY` | The gateway's **one internal** team-tracking key, scoped `identifiers:read`. Issued on team-tracking via `team-tracking-keys issue --name gateway --scopes identifiers:read`. |
| `GATEWAY_ENV` | `local` / `staging` / `production`. In non-`local` envs, startup refuses to boot if `API_KEY` or `DIRECTORY_API_KEY` are still the built-in dev default. |

## The resolver endpoint

```
GET /v1/resolve/discord/{github_login}
```

Resolves a GitHub login to the Discord id linked to the same person in team-tracking's
directory. Requires `X-API-Key` on a key scoped `resolve:discord`.

**Response — `200 OK`:**

```json
{ "discord_id": "123456789012345678" }
```

Only the Discord id is returned — no name, no team, no other identifiers. That's the
whole point of the curated surface: the gateway composes two internal calls
(`get_person_by_github` → `list_identifiers`) and hands back exactly one field.

**Error responses:**

| Status | Meaning |
|---|---|
| `401` | Missing or invalid `X-API-Key`. |
| `403` | Key valid but lacks the `resolve:discord` scope. |
| `404` | No person with that GitHub login, or that person has no linked Discord identifier. |
| `429` | Caller's key has exceeded the rate limit (60 requests / 60s per key, in-memory, single-replica). |
| `503` | team-tracking (the internal directory) is unreachable — the gateway doesn't guess; it fails closed. |

## Managing external keys (`gateway-keys`)

The gateway keeps its own key registry, separate from team-tracking's. Manage it with
the bundled CLI:

```bash
# Issue an external key for a consumer (e.g. a GitHub Action)
uv run gateway-keys issue --name reviewer-ping --scopes resolve:discord
# Prints: gw_<prefix>_<secret>  (shown ONCE — capture it now)

# List existing keys (metadata only, never plaintext)
uv run gateway-keys list --active-only

# Revoke a compromised key (soft-delete; history preserved)
uv run gateway-keys revoke <api_key_id>
```

Scopes recognized today:

- `resolve:discord` — the only external-facing scope so far.
- `admin` — wildcard, grants all scopes.

## Repo layout

```
gateway/
├── contracts/               The domain boundary — no framework imports
│   ├── types.py              Pydantic ApiKey type
│   ├── storage.py            StorageAdapter Protocol
│   └── directory.py           DirectoryClient Protocol + DirectoryUnavailable
│
├── src/
│   ├── api/
│   │   ├── app.py             App factory; mounts the resolver router + rate limit + audit middleware
│   │   ├── auth.py            Thin shim over `platform_auth`: require_scope, get_actor
│   │   ├── hashing.py         Thin shim over `platform_auth`: argon2 key hashing + gw_<prefix>_<secret> generation
│   │   ├── middleware.py      Thin shim over `platform_auth`: AuditLogMiddleware
│   │   ├── ratelimit.py       Per-key fixed-window rate limit (in-memory, single-replica)
│   │   ├── deps.py            get_storage() / get_directory() dependencies
│   │   └── routers/resolve.py `GET /v1/resolve/discord/{github_login}`
│   │
│   ├── directory/http_client.py  HTTP DirectoryClient — calls team-tracking with DIRECTORY_API_KEY
│   ├── storage/               StorageAdapter implementations (in-memory + Postgres) for the gateway's own api_keys table
│   ├── cli.py                 gateway-keys CLI (issue / list / revoke external keys)
│   └── config.py               Settings (DATABASE_URL, API_KEY, DIRECTORY_*, GATEWAY_ENV)
│
├── migrations/                Alembic — 001_api_keys
├── tests/                     pytest — auth, cli, directory client, health, rate limit, resolver, storage
├── Dockerfile, railway.json   Production image + Railway config (repo-root Docker context)
└── docker-compose.yml         Local Postgres on port 5435
```

## Testing

```bash
uv run pytest
```

Lint and format with ruff:

```bash
uv run ruff check .
uv run ruff format .
```

## Status

Public gateway with its own `api_keys` registry (migration 001), one internal
team-tracking key for outbound calls, and one endpoint:
`GET /v1/resolve/discord/{github_login}`. Rate-limited (60 req/min/key) and
audit-logged. Built on `packages/auth` (`platform_auth`) — no auth logic is
reimplemented here.

**Not implemented (by design):**

- **More endpoints** — the gateway only exposes what an external consumer has an
  actual need for; new endpoints are added deliberately, not by mirroring
  team-tracking's surface.
- **Multi-replica rate limiting** — the in-memory limiter is correct for a single
  Railway replica; a shared store (Redis) would be needed to scale horizontally.
