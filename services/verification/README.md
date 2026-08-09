# verification

HTTP API that proves someone controls an email address for a given subject, via short-lived one-time codes.

## What this service does

UTMIST needs to confirm that a person actually controls an email before binding it to an identity — linking a UofT address to a directory person, gating a signup, confirming an on-behalf-of action. verification is the small, focused service that does exactly that and nothing else.

The flow is two calls:

- **request-code** — a consumer names a `subject` (any opaque string it wants proven, e.g. a person id or a pending-signup token) and an `email`. The service generates a 6-digit numeric code, emails it, and stores only an HMAC of the code.
- **confirm-code** — the consumer submits the `subject` and the code the user typed back. On success it returns `{verified: true, subject, email}` — the caller now knows that email was reachable and belongs to whoever holds the subject.

The service is deliberately narrow: it keeps at most one live code per subject, never stores a code in the clear, and hardens the confirm path against replay and brute-force (see [confirm-code semantics](#confirm-code-semantics)). It owns no identity data — binding the proven email to a person is the caller's job.

## Quick start

Prerequisites: Docker, Python 3.11+, [uv](https://github.com/astral-sh/uv).

```bash
# 0. From the repo root, enter the service directory (all commands below run here)
cd services/verification

# 1. Copy environment config and start Postgres
cp .env.example .env
docker compose up -d postgres

# 2. Install dependencies (including dev tools)
uv sync --extra dev

# 3. Apply database migrations
uv run alembic upgrade head

# 4. Start the API server
uv run uvicorn src.api.app:app --reload --port 8003
```

> The repo is a single [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/) (root `pyproject.toml` with `[tool.uv.workspace] members = ["services/*", "packages/*"]`, one root `uv.lock`). verification depends on the shared `platform-auth` package (`[tool.uv.sources] platform-auth = { workspace = true }`) but the commands above are unchanged — `uv sync`, `uv run pytest`, `uv run alembic` still work exactly as shown when run from this directory.

The API is now running at `http://localhost:8003`. Interactive Swagger UI is at `http://localhost:8003/docs`; the machine-readable schema is at `http://localhost:8003/openapi.json`.

The default dev API key is `dev-api-key-change-me` and the default HMAC secret is `dev-hmac-secret-change-me` (both set in `.env`). Pass the key as `X-API-Key` on every request:

```bash
# Request a code (the fake email backend just captures it in memory in dev)
curl -sS -X POST http://localhost:8003/verification/request-code \
  -H "X-API-Key: dev-api-key-change-me" -H "Content-Type: application/json" \
  -d '{"subject": "person:42", "email": "alex@example.com"}'

# Confirm it
curl -sS -X POST http://localhost:8003/verification/confirm-code \
  -H "X-API-Key: dev-api-key-change-me" -H "Content-Type: application/json" \
  -d '{"subject": "person:42", "code": "123456"}'
```

**Ports:** this service runs on **8003** (team-tracking uses 8000, documentation-system 8001) and its Postgres container is exposed on **5434**.

### Environment tiers (`VF_ENV`)

`VF_ENV` declares which environment this instance represents: `local` (default), `staging`, or `production`. In any non-`local` tier the app **refuses to start** (`verify_production_secrets` in `src/config.py`) if it still holds a built-in dev default or an unsafe setting — the dev `API_KEY`, the dev `CODE_HMAC_SECRET`, a `dev_password@localhost` `DATABASE_URL`, `EMAIL_BACKEND=fake` (which silently drops mail), or a selected real backend missing its required credentials. This is checked once at boot, so a misconfigured deploy fails fast instead of 202-ing every request while delivering nothing.

## Auth model

Auth is provided by the shared `platform_auth` package (`build_auth` in `src/api/auth.py`), configured **env-key-only**:

- Every request needs `X-API-Key`. The only accepted key is the shared bootstrap env key (`API_KEY`), which carries the `admin` scope. There is **no key-issuing CLI and no DB-backed keys** here — `NullApiKeyStore` (`src/storage/null_keys.py`) short-circuits all key lookups, so the `vf_<prefix>_<secret>` envelope is reserved but unused. The env key must therefore be a plain opaque string, not shaped like that envelope.
- `require_scope("verification:write")` gates both verification endpoints. The scopes recognized are **`verification:write`** and **`admin`** (a wildcard that satisfies any check); the env key's `admin` scope covers both.
- `/health` is unauthenticated.
- `AuditLogMiddleware` (from `platform_auth`, logger `verification.audit`) logs one line per request with the resolved actor. The bootstrap key honors an `X-Actor` header for the audit actor; `dev:spoof` is disabled.

## API at a glance

| Method | Path | Scope | Description |
|--------|------|-------|-------------|
| POST | `/verification/request-code` | `verification:write` | Generate + email a one-time code for `{subject, email}`. **202 Accepted** on success |
| POST | `/verification/confirm-code` | `verification:write` | Verify `{subject, code}`; **200** → `{verified, subject, email}` |
| GET | `/health` | — (none) | Liveness probe → `{"status": "ok"}` |

Request/response shapes (`contracts/types.py`): both request bodies `forbid` extra fields. `email` is lightly validated (must contain `@` and a dotted domain) and normalized to trimmed-lowercase; `subject` is trimmed and must be non-empty. `request-code` returns `{"status": "sent"}`; `confirm-code` returns `{"verified": true, "subject": ..., "email": ...}`.

Machine-readable OpenAPI schema: `GET /openapi.json`. Interactive Swagger UI: `GET /docs`.

### request-code behavior

- **Rate limited per email.** If an unconsumed code for that email was created within the last `RATE_LIMIT_WINDOW` (**60 seconds**), the request is rejected with **429 `rate_limited`**.
- **Send-before-persist.** The email is dispatched *before* the code row is written. If delivery fails (`EmailSendError`), the endpoint returns **502 `email_send_failed`** and stores nothing — so the caller isn't rate-limited out of an immediate retry and no unsendable code is left behind.
- **Supersede-on-reissue.** A fresh request for an existing subject replaces that subject's row (atomic upsert), so only the newest code is ever live.
- Codes are 6-digit numeric, generated with `secrets`, expire after `CODE_TTL` (**10 minutes**), and are stored only as an **HMAC-SHA256** digest under `CODE_HMAC_SECRET` (`src/codes.py`). Verification is a constant-time compare; the plaintext code exists only in the email.

### confirm-code semantics

This path was hardened against replay and brute-force — the exact behavior:

- **No pending code** for the subject → **404 `no_pending_code`**.
- **Single-use + TTL.** The first correct code marks the row consumed (`mark_consumed`) and returns `{verified: true, subject, email}`.
- **Attempt limit.** Each wrong code atomically increments the subject's attempt counter; once it reaches `MAX_ATTEMPTS` (**5**) further confirms return **429 `too_many_attempts`**. A wrong code returns **400 `invalid_code`** (or 429 if that attempt hit the cap).
- **Expiry.** A code past its TTL that was never consumed → **410 `expired`**.
- **Idempotent replay (correct code, still within TTL).** Re-submitting the *correct* code against a consumed-but-unexpired subject re-returns `{verified: true, subject, email}`. This makes retries safe.
- **Wrong code on a consumed-but-unexpired subject** returns the *normal* failure — **400 `invalid_code`** (no email is leaked) — and counts toward the same attempt limit. So a holder of the `verification:write` scope who never knew the code cannot replay an arbitrary subject to harvest its verified email, nor brute-force it against the consumed record.
- **Consumed + past TTL** → **404 `no_pending_code`** (the proof window has closed).

## Storage

One SQLAlchemy Core table, `verification_codes` (`src/storage/schema.py`): `subject` (unique — enforces at most one live row per subject), `email` (indexed), `code_hash`, `expires_at`, `attempts`, `consumed_at`, `created_at`.

Two adapters implement the `VerificationStore` Protocol (`contracts/storage.py`):

- **`PostgresVerificationStore`** (`src/storage/postgres.py`) — production. `create_code` is an `INSERT ... ON CONFLICT (subject) DO UPDATE` upsert (safe under concurrent request-code calls). `increment_attempts` is a single `UPDATE ... RETURNING attempts` statement — an **atomic read-and-increment**, so parallel invalid confirms can't lose increments and slip past the attempt lockout.
- **`InMemoryVerificationStore`** (`src/storage/in_memory.py`) — used in tests and quick prototyping; same semantics, not persistent.

### Migrations

Schema is managed by Alembic (`migrations/`). Migration `001` creates `verification_codes` plus the email index. Apply locally with `uv run alembic upgrade head`. In production this runs automatically as the Railway **`preDeployCommand`** (`railway.json`) before each release — it is **not** run by the container `CMD`.

## Email providers

The sender is chosen by `EMAIL_BACKEND` and wired in `src/api/deps.py` behind the `EmailSender` Protocol (`src/email/base.py`); any provider failure is normalized to `EmailSendError` → a clean 502.

- **`fake`** (default; `src/email/fake.py`) — captures messages in memory instead of sending. Used in tests and the dev playground; refused in non-`local` `VF_ENV`.
- **`resend`** (`src/email/resend.py`) — Resend HTTP API (HTTPS, works on any Railway plan). Requires `RESEND_API_KEY` and `EMAIL_FROM`.
- **`gmail`** (`src/email/gmail.py`) — Gmail API via a service account with domain-wide delegation, impersonating `GMAIL_SENDER`. Requires `GMAIL_SENDER` and `GMAIL_CREDENTIALS_JSON` (base64-encoded service-account JSON).

## Folder tour

```
verification/
├── contracts/                Domain boundary — no framework imports
│   ├── types.py              Pydantic DTOs (RequestCode/ConfirmCode In/Out) + VerificationCode record
│   └── storage.py            VerificationStore Protocol (create/get/latest/increment/consume)
│
├── src/
│   ├── api/                  FastAPI application
│   │   ├── app.py            create_app(): verify_production_secrets, audit middleware, /health, router
│   │   ├── auth.py           build_auth() over platform_auth: require_scope, env-key-only (envelope "vf_")
│   │   ├── deps.py           get_storage / get_email_sender / get_key_store wiring
│   │   └── routers/
│   │       └── verification.py   request-code + confirm-code endpoints
│   │
│   ├── storage/              VerificationStore implementations
│   │   ├── schema.py         verification_codes table (SQLAlchemy Core)
│   │   ├── in_memory.py      InMemoryVerificationStore — tests
│   │   ├── postgres.py       PostgresVerificationStore — production (atomic upsert + increment)
│   │   └── null_keys.py      NullApiKeyStore — env-key-only auth (no DB keys)
│   │
│   ├── email/                EmailSender implementations + registry
│   │   ├── base.py           EmailSender Protocol + EmailSendError
│   │   ├── fake.py           FakeSender (in-memory capture; dev/tests)
│   │   ├── resend.py         ResendSender (Resend HTTP API)
│   │   └── gmail.py          GmailSender (Gmail API, service account)
│   │
│   ├── codes.py              generate_code / hash_code / verify_code (HMAC-SHA256, constant-time)
│   ├── policy.py             CODE_LENGTH=6, CODE_TTL=10m, RATE_LIMIT_WINDOW=60s, MAX_ATTEMPTS=5
│   └── config.py             Settings (DATABASE_URL, API_KEY, CODE_HMAC_SECRET, VF_ENV, EMAIL_*) + prod-secret guard
│
├── migrations/               Alembic migrations (001_initial_schema)
├── tests/                    Two-mode test suite (see Testing)
├── docker-compose.yml        Local dev Postgres (host port 5434)
├── Dockerfile                Production image (build context = repo root; uv sync --package verification)
└── railway.json              Railway build/deploy: preDeployCommand = alembic upgrade head, healthcheck /health
```

**Dependency direction:** `contracts/` imports nothing from `src/`. `src/api/` depends only on `contracts/` and `src/config`/`src/codes`/`src/policy`/`src/email`. The storage and email adapters each implement a `contracts`/`base` Protocol and know nothing about FastAPI; `src/api/deps.py` is the single wiring point.

## Testing

The suite runs in two modes.

**Fast (in-memory, no Docker required):**

```bash
uv run pytest --ignore=tests/test_postgres_adapter.py -q
```

Runs against `InMemoryVerificationStore`, injected into FastAPI via `app.dependency_overrides` (see `tests/conftest.py`), plus the `FakeSender`. Covers both endpoints, the full confirm-code state machine, rate limiting, the code HMAC helpers, all three email senders, config/prod-secret guards, and auth. Completes in well under a second.

**Full (adds Postgres integration):**

```bash
docker compose up -d postgres
uv run alembic upgrade head
uv run pytest
```

Adds `tests/test_postgres_adapter.py`, which replays the adapter's behavioral assertions (including the atomic increment) against a live Postgres. They connect via `DATABASE_URL` (in `.env`) pointing at the running container on port 5434.

Lint and format with ruff:

```bash
uv run ruff check .
uv run ruff format --check .
```

**CI.** The `verification-test` job in `.github/workflows/ci.yml` spins up a Postgres 16 service, runs `uv sync --extra dev`, `uv run alembic upgrade head`, the full `uv run pytest`, and both `ruff check` and `ruff format --check`. A separate step builds the Dockerfile and smoke-tests that `import src.api.app` resolves at boot.

## Status

One table (`verification_codes`), two endpoints plus `/health`, two storage adapters, three email backends, migration 001. The confirm-code path is hardened: single-use codes, 10-minute TTL, 5-attempt lockout, idempotent correct-code replay within the window, and no email leakage on wrong-code replay of a consumed subject. Env-key-only auth (`verification:write` / `admin`) with an audit log, and a boot-time guard that refuses to start a non-`local` deploy on dev secrets or the mail-dropping fake backend.

**Not implemented (by design):**

- **DB-backed / per-consumer keys + CLI** — auth is the shared env key only; the `vf_` envelope is reserved but unused.
- **Identity binding** — the service proves an email is reachable; associating it with a directory person is the caller's job (team-tracking owns identity).
- **Resend/throttle beyond the 60s window and 5-attempt cap** — no exponential backoff, per-IP limiting, or code-length/TTL configurability.
- **Pagination / listing** — there is no endpoint to enumerate codes; rows are looked up only by subject or email internally.

## Documentation

- [docs/API.md](docs/API.md) — consumer-facing endpoint reference: request/response shapes, errors, curl examples
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — contributor orientation: why the service is shaped this way, boundaries, trade-offs
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) — task walkthroughs and the pre-push checklist
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — deploy shape, variables, key provisioning, troubleshooting
