# gateway

The **public** external API gateway — a thin, scoped, rate-limited door onto UTMIST's
internal directory for third-party consumers (GitHub Actions, external integrations)
that should never see the internal `team-tracking` API directly.

## What this service does

team-tracking is the internal source of truth for the org, but not every consumer of
that data is inside the org's trust boundary. A future GitHub Action could
turn a GitHub login into a Discord id (to @-mention a reviewer) without holding a
team-tracking key or seeing anything else in the directory.

The resolver is a working example for future gateway operations. No current
integration consumes it: the existing Discord notifications display GitHub
reviewer logins, but do not call the gateway or mention reviewers on Discord.

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

See [API](docs/API.md), [architecture](docs/ARCHITECTURE.md),
[contributing](docs/CONTRIBUTING.md), and the [deployment runbook](docs/DEPLOYMENT.md).
The [integration audit](docs/CHANGE-AUDIT.md) records compatibility checks and file rationale.

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
uv run uvicorn src.api.app:app --reload --port 8006
```

> The repo is a single [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/) (root `pyproject.toml` with `[tool.uv.workspace] members = ["services/*", "packages/*"]`, one root `uv.lock`). gateway depends on the shared `platform-auth` package (`[tool.uv.sources] platform-auth = { workspace = true }`) but the commands above are unchanged — `uv sync`, `uv run pytest`, `uv run alembic` still work exactly as shown when run from this directory.

The API is now at `http://localhost:8006` (8000–8005 are the internal services).
Interactive Swagger UI is at `http://localhost:8006/docs`; the machine-readable
schema is at `http://localhost:8006/openapi.json`.

### Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | The gateway's **own** Postgres — it holds only `api_keys` (its external key registry), never a copy of team-tracking's data. |
| `DIRECTORY_BASE_URL` | team-tracking's base URL — where the gateway makes its **outbound** call. |
| `DIRECTORY_API_KEY` | The gateway's **one internal** team-tracking key, scoped `identifiers:read`. Issued on team-tracking via `team-tracking-keys issue --name gateway --scopes identifiers:read`. |
| `GATEWAY_ENV` | `local` / `staging` / `production`. In non-`local` envs, startup refuses to boot if `DIRECTORY_API_KEY` is blank or still the built-in dev default. |
| `TRUST_PROXY_HEADERS` | Whether a proxy in front of the gateway sets `X-Forwarded-For` / `X-Real-IP`. **Set this to `true` on Railway.** Left `false`, the per-IP flood guard buckets every caller behind the proxy together; set `true` where no proxy exists, and a caller can forge their own source address. |

**There is deliberately no `API_KEY`.** Every other service in this repo carries
one to feed `platform_auth`'s env-bootstrap path, which authenticates you as a
key holding `admin` — a wildcard scope. On the internal services that is a
sanctioned grace path, reachable only from the private network, and it is how
you authenticate to the admin API that issues the first real key. The gateway
has neither justification: its keys are issued straight into the database by
`gateway-keys`, and it is the only service exposed to the internet. Setting
`API_KEY` here does nothing — `src/api/auth.py` passes `get_env_key=lambda:
None`, and `tests/test_auth.py` pins that it stays that way.

## The resolver endpoint

```http
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
| `404` | No Discord id for that GitHub login. |
| `429` | Rate limit exceeded — either the caller's key quota or the per-IP flood guard (see below). |
| `503` | team-tracking (the internal directory) is unreachable, or answered in a shape we don't recognise — the gateway doesn't guess; it fails closed. |

The `404` is deliberately one message for two different situations: "that login
isn't in the directory" and "it is, but there's no Discord account linked". Told
apart, they let anyone holding a `resolve:discord` key feed in a list of GitHub
logins and learn which of them are UTMIST members — a membership oracle on a
public endpoint. The audit records the requested login and status without directory response data.
`tests/test_resolve.py` pins that the two responses are byte-identical.

### Two things to know about this endpoint

**The lookup is case-sensitive, and GitHub logins are not.** team-tracking
matches `person_identifiers.external_id` exactly for every provider except
`email`, so a person stored as `octocat` will **not** be found by a request for
`OctoCat`. Whoever links the identifier and whoever calls the endpoint have to
agree on casing. The gateway does not lowercase the login, because that would
only help if stored values were already lowercase and would break the mixed-case
links that currently work. The real fix belongs upstream — normalise `github`
identifiers on write in team-tracking, plus a migration for existing rows — and
is tracked separately. Until then, link GitHub identifiers using the exact login
GitHub reports.

**The GitHub login appears in the audit log.** It's a path segment, and
`AuditLogMiddleware` records `request.url.path`, so every call writes the login
the caller asked about to stdout. That's intentional: an audit trail for the
public door that omitted *what was requested* would be close to useless for
investigating abuse, and a GitHub login is public, pseudonymous, and supplied by
the caller in the first place. What is never logged is anything the directory
answered back — no person id, no name, no other identifiers, and not the Discord
id itself.

### Rate limiting

Two layers, because they defend different things:

| Layer | Where | Limit | Keyed on |
|---|---|---|---|
| Per-consumer quota | Router dependency, **after** auth | 60 / 60s | `AuthedKey.name` — an issued key |
| Flood guard | Middleware, **in front of** auth | 120 / 60s | Client IP (health routes exempt) |

The quota is the one a consumer notices. It runs after `require_api_key`, so it
can only ever be keyed on the name of a key we issued — the set is bounded by
our own key registry, no plaintext key sits in process memory, and nobody
outside can grow it.

The flood guard exists because authentication is itself the expensive step: a
well-formed `gw_` key forces an argon2 verification, which is slow by design.
Per-key limiting can't bound that — an attacker just varies the key — so
something in front of auth has to. Its limit is loose on purpose; it is a
floodgate, not a quota.

Both are in-memory and process-local, with synchronized updates across request
threads. Run one replica and one worker. A
shared store (Redis) is needed only if the gateway is ever scaled past one.

## Managing external keys (`gateway-keys`)

The gateway keeps its own key registry, separate from team-tracking's. Manage it with
the bundled CLI from the repository root, with the gateway database configured:

```bash
# Issue a key for a test consumer; replace the name when integrating a real consumer
uv --directory services/gateway --project . run python -m src.cli issue --name example-consumer --scopes resolve:discord
# Prints: gw_<prefix>_<secret>  (shown ONCE — capture it now)

# List existing keys (IDs and metadata only, never plaintext)
uv --directory services/gateway --project . run python -m src.cli list --active-only

# Revoke a compromised key (soft-delete; history preserved)
uv --directory services/gateway --project . run python -m src.cli revoke <api_key_id>
```

In the shared workspace, use the module commands above: `uv --project` alone
does not resolve collisions between services named `src`. The isolated gateway
container can use `gateway-keys` directly.

Scopes recognized today:

- `resolve:discord` — the only external-facing scope, and the only one to issue.

`platform_auth` also treats `admin` as a wildcard that satisfies every scope
check. The CLI rejects `admin`, unknown scopes, and missing scopes. There is no bootstrap path that
needs one here (see the note on `API_KEY` above), and on a service reachable
from the internet a wildcard key is a standing invitation. Scope every external
key to exactly the endpoint its consumer calls.

## Repo layout

```text
gateway/
├── contracts/               The domain boundary — no framework imports
│   ├── types.py              Pydantic ApiKey type
│   ├── storage.py            StorageAdapter Protocol
│   └── directory.py           DirectoryClient Protocol + DirectoryUnavailable
│
├── src/
│   ├── api/
│   │   ├── app.py             App factory; mounts the resolver router + flood guard + audit middleware
│   │   ├── auth.py            Thin shim over `platform_auth`: require_scope, get_actor — env-bootstrap path OFF
│   │   ├── hashing.py         Thin shim over `platform_auth`: argon2 key hashing + gw_<prefix>_<secret> generation
│   │   ├── middleware.py      Thin shim over `platform_auth`: AuditLogMiddleware
│   │   ├── ratelimit.py       Bounded fixed-window counter; per-key quota + per-IP flood guard
│   │   ├── deps.py            get_storage() / get_directory() dependencies (pooled directory client)
│   │   └── routers/resolve.py `GET /v1/resolve/discord/{github_login}`
│   │
│   ├── directory/http_client.py  HTTP DirectoryClient — calls team-tracking with DIRECTORY_API_KEY
│   ├── storage/               StorageAdapter implementations (in-memory + Postgres) for the gateway's own api_keys table
│   ├── cli.py                 gateway-keys CLI (issue / list / revoke external keys)
│   └── config.py               Settings (DATABASE_URL, DIRECTORY_*, GATEWAY_ENV, TRUST_PROXY_HEADERS)
│
├── migrations/                Alembic — 001_api_keys
├── tests/                     pytest — auth, cli, config guards, directory client, health, rate limit, resolver, storage
├── Dockerfile, railway.json   Production image + Railway config (repo-root Docker context)
└── docker-compose.yml         Local Postgres on port 5435
```

## Testing

From `services/gateway`, run the fast suite:

```bash
uv run pytest --ignore=tests/test_postgres_adapter.py
```

For the full suite, start the local Compose Postgres and create a separate,
disposable `gateway_test` database. The adapter tests clear its key table:

```bash
docker compose up -d postgres
docker compose exec postgres createdb -U gateway gateway_test
export GATEWAY_TEST_DATABASE_URL=postgresql+psycopg://gateway:dev_password@localhost:5435/gateway_test
DATABASE_URL="$GATEWAY_TEST_DATABASE_URL" uv run alembic upgrade head
DATABASE_URL="$GATEWAY_TEST_DATABASE_URL" uv run alembic downgrade base
DATABASE_URL="$GATEWAY_TEST_DATABASE_URL" uv run alembic upgrade head
RUN_PG_TESTS=1 uv run pytest
```

From the repo root, verify the actual HTTP integration with the current directory:

```bash
docker build -f services/team-tracking/Dockerfile -t team-tracking:ci .
docker build -f services/gateway/Dockerfile -t gateway:ci .
python3 services/gateway/tests/smoke_stack.py
```

The smoke script creates and removes its own Docker network, containers, and
separate databases, using generated test keys and synthetic records. It verifies
that the gateway runs the API, migrations, and key CLI as an unprivileged user.
[CI](../../.github/workflows/ci.yml) runs both the Postgres suite and this HTTP check,
plus an isolated wheel install outside the checkout to catch missing runtime
packages. `make check` includes the fast gateway suite; `make test-full` includes
Postgres when the test variables above are set.

Lint and format with ruff:

```bash
uv run ruff check .
uv run ruff format --check .
```

The health endpoints are documented in [API.md](docs/API.md#health). Railway uses
`/health/ready` to verify the key database before admitting a deployment.

## Status

Public gateway with its own `api_keys` registry (migration 001), one internal
team-tracking key for outbound calls, and one endpoint:
`GET /v1/resolve/discord/{github_login}`. Rate-limited (60 req/min/key, plus a
per-IP flood guard) and audit-logged. Every caller must present a key issued by
`gateway-keys` — there is no env-bootstrap admin path. Built on `packages/auth`
(`platform_auth`) — no auth logic is reimplemented here.

**Not implemented (by design):**

- **More endpoints** — the gateway only exposes what an external consumer has an
  actual need for; new endpoints are added deliberately, not by mirroring
  team-tracking's surface.
- **Multi-replica rate limiting** — the in-memory limiter is correct for a single
  Railway replica; a shared store (Redis) would be needed to scale horizontally.

**Known constraint:** GitHub logins resolve case-sensitively, because that is how
team-tracking matches identifiers. See "Two things to know about this endpoint"
above — the fix belongs upstream, not here.
