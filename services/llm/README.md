# llm

Thin, stateless HTTP API that fronts Claude on Amazon Bedrock for UTMIST's internal consumers: one `POST /chat` endpoint, scoped API-key auth, no database.

## What this service does

UTMIST's internal tools — a docs helper bot, reviewer-summary jobs, dashboards — all want to call an LLM, but none of them should carry AWS credentials, pin a Bedrock model id, or reimplement auth. The llm service is the single choke point: consumers hold a scoped `llm_` API key and POST chat turns; the service owns the Bedrock credentials, the model catalog, and the provider quirks behind a neutral request/response shape.

Two design choices shape everything:

- **Stateless — no database, no migrations.** The service holds no persistent state. It maps an inbound `POST /chat` to a Bedrock call and returns the completion. There is no Postgres, no Alembic, no `docker compose` for a DB. API keys are not stored in a table; they are seeded from a config env var at boot (see the auth model below).
- **Provider-agnostic core.** The API layer speaks a neutral `LLMRequest`/`LLMResult` (`src/providers/base.py`) and never imports a vendor SDK. A `Callable` registry (`src/providers/registry.py`) picks the concrete provider from config, so swapping Bedrock endpoints — or mocking the provider entirely in tests — needs no change to the router.

## The choke-point principle

**Nothing runs inside llm except the chat proxy.** It is an HTTP API and nothing else — no queues, no scheduled jobs, no UI, no persistence. Every consumer talks to it over HTTP:

- Bedrock credentials and the model catalog live in exactly one place, not scattered across every bot and job.
- Consumers can be written in any language, deployed anywhere, and revoked individually by rotating their key out of config.
- The service can be redeployed or re-pointed at a different Bedrock endpoint without touching its consumers.

## Quick start

Prerequisites: Python 3.11+, [uv](https://github.com/astral-sh/uv). (No Docker or database needed for local dev — the fake provider covers the tests, and you only need AWS credentials to make real Bedrock calls.)

```bash
# 0. From the repo root, enter the service directory (all commands below run here)
cd services/llm

# 1. Copy environment config
cp .env.example .env

# 2. Install dependencies (including dev tools)
uv sync --extra dev

# 3. Start the API server
uv run uvicorn src.api.app:app --reload --port 8002
```

> The repo is a single [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/) (root `pyproject.toml` with `[tool.uv.workspace] members = ["services/*", "packages/*"]`, one root `uv.lock`). llm depends on the shared `platform-auth` package (`[tool.uv.sources] platform-auth = { workspace = true }`) but the commands above are unchanged — `uv sync` and `uv run pytest` work exactly as shown when run from this directory.

The API is now at `http://localhost:8002`. Interactive Swagger UI is at `http://localhost:8002/docs`; the machine-readable schema is at `http://localhost:8002/openapi.json`.

The default dev bootstrap key is `dev-api-key-change-me` (set in `.env`, carries the `admin` wildcard scope). Pass it as `X-API-Key` on every request:

```bash
curl -sS http://localhost:8002/chat \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hello!"}]}' | python3 -m json.tool
```

**Ports:** this service runs on **8002** (team-tracking uses 8000, documentation-system 8001) — chosen to avoid colliding when running the services locally.

### Environment tiers (`LLM_ENV`)

`LLM_ENV` declares which environment this instance represents: `local` (default), `staging`, or `production`. Outside `local`, `create_app()` calls `verify_production_secrets()` and **refuses to start** unless `API_KEY` is overridden from the dev default and `AWS_REGION` is set — a misconfigured deploy dies at boot, not on first request.

### Configuration

All settings load from the environment (`src/config.py`, `.env` in dev):

| Var | Default | Purpose |
|-----|---------|---------|
| `LLM_ENV` | `local` | Environment tier (`local` / `staging` / `production`). |
| `API_KEY` | `dev-api-key-change-me` | Env-bootstrap key; carries `admin` scope. Must be overridden outside `local`. |
| `CONSUMER_KEYS` | `""` | JSON array of per-consumer keys (see auth model). |
| `LLM_PROVIDER` | `bedrock-converse` | Provider backend (`bedrock-converse` or `bedrock`). |
| `LLM_MODEL` | `claude-sonnet-4-6` | Default model when a request omits `model`. |
| `REQUEST_TIMEOUT_S` | `60` | Per-request timeout to the provider (seconds). |
| `THINKING_DEFAULT` | `true` | Whether extended thinking is on when a request omits `thinking`. |
| `AWS_REGION` | `""` | Bedrock region. Standard AWS credential chain also reads `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_BEARER_TOKEN_BEDROCK`. |

Usage bills as **standard Amazon Bedrock** (AWS credits apply) — deliberately *not* Claude Platform on AWS / Marketplace.

## Auth model

Auth is scoped API keys via the shared `platform_auth` package, matching team-tracking and documentation-system — but **DB-free**. There is no `api_keys` table.

- Every request needs `X-API-Key`. A key is either the shared bootstrap env key (`API_KEY`, scope: `admin`) or a per-consumer key seeded from the `CONSUMER_KEYS` env var.
- **`CONSUMER_KEYS` is a JSON array**, each entry `{"name", "prefix", "key_hash", "scopes"}`. At boot, `key_store_from_config()` (`packages/auth/platform_auth/memory_store.py`) parses it into an in-memory store that satisfies `platform_auth`'s `ApiKeyStore` protocol — the stateless equivalent of team-tracking's Postgres key table, swappable for a persistent store later. Keys are stored only as Argon2 hashes; a malformed `CONSUMER_KEYS` fails fast at startup.
- Keys use the **`llm_` envelope** (`llm_<prefix>_<secret>`). There is **no `X-Actor` header** and no `dev:spoof` scope — this is a service-to-service API, so the audit actor is always the authenticated key's own name (attested actor).

### Minting consumer keys

Use the `llm-keys` CLI. It prints the plaintext key **once** to stdout and the `CONSUMER_KEYS` JSON entry to stderr — it does not (and cannot) write to any store:

```bash
uv run llm-keys --name reviewer-summaries --scopes chat
# stdout: llm_<prefix>_<secret>   (the key — give it to the consumer, shown ONCE)
# stderr: {"name": "reviewer-summaries", "prefix": "...", "key_hash": "$argon2id$...", "scopes": ["chat"]}
```

Append the printed JSON object to the service's `CONSUMER_KEYS` array and redeploy. To **revoke** a key, drop its entry from `CONSUMER_KEYS` and redeploy — there is no revoke command because there is no database.

## API at a glance

Every endpoint except `/health` requires `X-API-Key`.

| Method | Path | Scope | Description |
|--------|------|-------|-------------|
| POST | `/chat` | `chat` | Send chat turns to Claude, get a completion back. |
| GET | `/health` | none | Liveness probe → `{"status": "ok"}`. |

`POST /chat` requires the `chat` scope (`require_scope("chat")`); the `admin` wildcard satisfies it, so the bootstrap key and any admin key also work. A valid key **without** `chat` is rejected with **403** before the provider is ever called (no budget spent).

**Request body** (`ChatRequest`, `contracts/chat.py`):

| Field | Type | Notes |
|-------|------|-------|
| `messages` | `[{role, content}]` | Required, non-empty. `role` is `user` or `assistant`; `content` non-empty. |
| `system` | string \| null | Optional system prompt. |
| `model` | string \| null | Optional. Must be one of `claude-sonnet-4-6`, `claude-opus-4-6`; defaults to `LLM_MODEL`. |
| `max_tokens` | int | Default `16000`, range `1`–`64000`. |
| `thinking` | bool \| null | Extended thinking; defaults to `THINKING_DEFAULT` when omitted. |

**Response** (`ChatResponse`): `{ "content", "model", "stop_reason", "usage": { "input_tokens", "output_tokens" } }`.

**Provider errors** are normalized to HTTP status: rate limit → **429**, timeout → **504**, other upstream/5xx/config faults → **502**. Validation failures (empty `messages`, unknown `model`) → **422**.

## Repo layout

```
llm/
├── contracts/
│   └── chat.py            Pydantic request/response models (ChatRequest, ChatResponse, Usage)
│
├── src/
│   ├── api/               FastAPI application
│   │   ├── app.py         App factory (create_app); mounts /chat + /health, audit middleware
│   │   ├── auth.py        Builds require_scope / get_actor from platform_auth (envelope="llm_")
│   │   ├── deps.py        get_key_store / get_llm — the single wiring point (both lru_cached)
│   │   ├── hashing.py     Thin shim over platform_auth: llm_-envelope key generation
│   │   └── routers/
│   │       └── chat.py    POST /chat — require_scope("chat"), maps body → provider → response
│   │
│   ├── providers/         Provider-agnostic LLM layer (no FastAPI)
│   │   ├── base.py            LLMRequest/LLMResult/LLMProvider Protocol + normalized error hierarchy
│   │   ├── bedrock_converse.py  Default: Claude via bedrock-runtime Converse API (US-regional profiles)
│   │   ├── bedrock.py           Alt: Claude via AnthropicBedrockMantle (Messages endpoint)
│   │   └── registry.py          Config-driven provider selection (LLM_PROVIDER → builder)
│   │
│   ├── mint_key.py        llm-keys CLI — prints a key + its CONSUMER_KEYS entry, no store writes
│   └── config.py          Settings (LLM_ENV, API_KEY, CONSUMER_KEYS, LLM_*, AWS_REGION) + boot check
│
├── tests/                 Fast tests — no Docker, no network (fake provider)
├── Dockerfile             Production image (built + import-smoke-tested by CI; used by Railway).
│                          Build context is the repo root; installs via `uv sync --frozen --no-dev --package llm`.
├── docker-compose.yml     Builds/runs the image locally on port 8002 (no DB service).
└── railway.json           Railway deploy config: Dockerfile builder, /health check, NO preDeployCommand
                           (stateless — nothing to migrate).
```

**Dependency direction:** `contracts/` imports nothing from `src/`. `src/api/` depends only on `contracts/`, `src/config`, and the provider Protocol. `src/providers/` implements the `LLMProvider` Protocol and knows nothing about FastAPI. `src/api/deps.py` is the only place the concrete provider and key store get wired in — so tests override `get_llm` / `get_key_store` via `app.dependency_overrides`.

### The two Bedrock providers

Both bill as standard Amazon Bedrock; they differ only in the Bedrock endpoint and how model ids are formed:

- **`bedrock-converse`** (default) — `BedrockConverseProvider` calls the `bedrock-runtime` **Converse** API. Used because this account's model access is US-regional cross-region inference profiles (`us.anthropic.claude-sonnet-4-6`), which the Messages endpoint can't target. Maps neutral model names to inference-profile ids via an explicit table.
- **`bedrock`** — `BedrockClaudeProvider` calls the **Mantle Messages** endpoint via `AnthropicBedrockMantle`. Needs global/Messages model access.

In tests, neither is used: a `_FakeProvider` implementing the `LLMProvider` protocol is injected via `dependency_overrides`, so the suite runs with no AWS credentials and no network.

## Testing

The suite is single-mode — no Docker, no database, no network:

```bash
uv run pytest
```

Runs against a fake provider injected via `app.dependency_overrides`. Covers the `/chat` happy path and neutral-type mapping, the auth paths (missing key → 401, key without `chat` → 403, `chat` scope → 200, `admin` wildcard → 200), request validation (empty messages, unknown model → 422), provider-error → HTTP-status mapping, the key store, the `llm-keys` CLI, config/boot checks, both Bedrock adapters (with stubbed clients), the audit log, and the OpenAPI schema.

Lint and format with ruff:

```bash
uv run ruff check .
uv run ruff format .
```

**CI** runs an `llm-test` job (`.github/workflows/ci.yml`): `uv sync --extra dev`, `uv run pytest`, `ruff check`, and `ruff format --check`. A separate image job builds the Dockerfile and smoke-tests that the app imports at boot (`python -c "import src.api.app"`).

## Status

v0.1: a stateless `POST /chat` proxy over Amazon Bedrock, config-seeded scoped API keys (`chat` / `admin`) with an attested-actor audit trail, two swappable Bedrock providers behind a neutral Protocol, normalized provider-error mapping, and a fast 65-test suite with no external dependencies.

**Not implemented (by design):**

- **Streaming** — `/chat` returns the full completion; no SSE/token streaming.
- **Persistence** — no conversation storage, no DB. Consumers keep their own history and send it on each call.
- **RAG / retrieval** — this service does not embed or search documents; that lives in the consumers (e.g. the helper bot).
- **Persistent key store** — keys come from `CONSUMER_KEYS` config; a DB-backed store (with a `revoke` command) can drop in later behind the same `ApiKeyStore` protocol.

## Documentation

- [docs/API.md](docs/API.md) — consumer-facing endpoint reference: request/response shapes, errors, curl examples
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — contributor orientation: why the service is shaped this way, boundaries, trade-offs
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) — task walkthroughs and the pre-push checklist
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — deploy shape, variables, key provisioning, troubleshooting
