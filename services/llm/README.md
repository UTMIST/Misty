# llm

Thin, stateless HTTP API for UTMIST's internal consumers: `POST /chat` for Claude completions on Amazon Bedrock, `POST /embed` for batch text embeddings on OpenAI, scoped API-key auth, no database.

## What this service does

UTMIST's internal tools — a docs helper bot, reviewer-summary jobs, dashboards — all want model access, but none should carry provider credentials, pin vendor model ids, or reimplement auth. The llm service is the single choke point: consumers hold a scoped `llm_` API key and POST chat turns or embedding inputs; the service owns the credentials, model catalog, and provider quirks behind neutral request/response shapes.

Two design choices shape everything:

- **Stateless — no database, no migrations.** The service holds no persistent state. It maps an inbound `/chat` or `/embed` request to a provider call and returns the result. There is no Postgres, no Alembic, no `docker compose` for a DB. API keys are not stored in a table; they are seeded from a config env var at boot (see the auth model below).
- **Provider-agnostic core.** The API layer speaks neutral chat and embedding dataclasses (`src/providers/base.py`) and never imports a vendor SDK. `src/providers/registry.py` selects the chat provider from config and builds the OpenAI embedder; tests inject fakes without changing the routers.

## The choke-point principle

**Nothing runs inside llm except the chat and embedding proxy.** It is an HTTP API and nothing else — no job queues, no scheduled jobs, no UI, no persistence. Every consumer talks to it over HTTP:

- AWS/OpenAI credentials and the model catalog live in exactly one place, not scattered across every bot and job.
- Consumers can be written in any language, deployed anywhere, and revoked individually by rotating their key out of config.
- The chat backend can be redeployed or re-pointed at a different Bedrock endpoint without touching its consumers.

## Quick start

Prerequisites: Python 3.11+, [uv](https://github.com/astral-sh/uv). No Docker or database needed for local dev. Tests use fakes; real calls need AWS credentials for chat and `OPENAI_API_KEY` for embeddings.

```bash
# 0. From the repo root, enter the service directory (setup commands below run here)
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

`LLM_ENV` declares which environment this instance represents: `local` (default), `staging`, or `production`. Outside `local`, `create_app()` calls `verify_production_secrets()` and **refuses to start** unless `API_KEY` is overridden from the dev default and both `AWS_REGION` and `OPENAI_API_KEY` are set. Configure these before merging to an auto-deploy branch; see [DEPLOYMENT.md](docs/DEPLOYMENT.md#embeddings-post-embed).

### Configuration

All settings load from the environment (`src/config.py`, `.env` in dev):

| Var | Default | Purpose |
|-----|---------|---------|
| `LLM_ENV` | `local` | Environment tier (`local` / `staging` / `production`). |
| `API_KEY` | `dev-api-key-change-me` | Env-bootstrap key; carries `admin` scope. Must be overridden outside `local`. |
| `CONSUMER_KEYS` | `""` | JSON array of per-consumer keys (see auth model). |
| `LLM_PROVIDER` | `bedrock-converse` | Provider backend (`bedrock-converse` or `bedrock`). |
| `LLM_MODEL` | `claude-sonnet-4-6` | Default chat model when a request omits `model`. |
| `EMBED_MODEL` | `openai-embed-3-small` | Embedding default at **1536 dimensions**; see [models and initial choice](docs/ARCHITECTURE.md#initial-embedding-model). |
| `OPENAI_API_KEY` | `""` | OpenAI credential (`SecretStr`), required outside `local`. Missing locally → `/embed` returns 503. |
| `REQUEST_TIMEOUT_S` | `60` | SDK timeout setting (seconds); see [embedding timeout caveats](docs/API.md#batching-and-timeouts). |
| `EMBED_MAX_REQUEST_CHARS` | `400000` | Aggregate character cap; see [input limits](docs/API.md#post-embed). |
| `THINKING_DEFAULT` | `true` | Whether extended thinking is on when a request omits `thinking`. |
| `AWS_REGION` | `""` | Bedrock region. Standard AWS credential chain also reads `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_BEARER_TOKEN_BEDROCK`. |

Chat bills as **standard Amazon Bedrock** (AWS credits apply) — deliberately *not* Claude Platform on AWS / Marketplace. Embeddings bill through **OpenAI, not AWS credits**.

## Auth model

Auth is scoped API keys via the shared `platform_auth` package, matching team-tracking and documentation-system — but **DB-free**. There is no `api_keys` table.

- Every request needs `X-API-Key`. A key is either the shared bootstrap env key (`API_KEY`, scope: `admin`) or a per-consumer key seeded from the `CONSUMER_KEYS` env var.
- **`CONSUMER_KEYS` is a JSON array**, each entry `{"name", "prefix", "key_hash", "scopes"}`. At boot, `key_store_from_config()` (`packages/auth/platform_auth/memory_store.py`) parses it into an in-memory store that satisfies `platform_auth`'s `ApiKeyStore` protocol — the stateless equivalent of team-tracking's Postgres key table, swappable for a persistent store later. Keys are stored only as Argon2 hashes; a malformed `CONSUMER_KEYS` fails fast at startup.
- Keys use the **`llm_` envelope** (`llm_<prefix>_<secret>`). There is **no `X-Actor` header** and no `dev:spoof` scope — this is a service-to-service API, so the audit actor is always the authenticated key's own name (attested actor).

### Minting consumer keys

Use the `llm-keys` CLI **from the repo root**. It prints the plaintext key **once** to stdout and the `CONSUMER_KEYS` JSON entry to stderr — it does not (and cannot) write to any store:

```bash
uv --project services/llm run llm-keys --name reviewer-summaries --scopes chat
# stdout: llm_<prefix>_<secret>   (the key — give it to the consumer, shown ONCE)
# stderr: {"name": "reviewer-summaries", "prefix": "...", "key_hash": "$argon2id$...", "scopes": ["chat"]}
# For an approved embedding-only caller:
uv --project services/llm run llm-keys --name embedding-caller --scopes embed
# Use --scopes chat embed only when a caller needs both.
```

Append the printed JSON object to the service's `CONSUMER_KEYS` array and redeploy. To **revoke** a key, drop its entry from `CONSUMER_KEYS` and redeploy — there is no revoke command because there is no database.

## API at a glance

Every endpoint except `/health` requires `X-API-Key`.

| Method | Path | Scope | Description |
|--------|------|-------|-------------|
| POST | `/chat` | `chat` | Send chat turns to Claude, get a completion back. |
| POST | `/embed` | `embed` | Turn a batch of strings into vectors. |
| GET | `/health` | none | Liveness probe → `{"status": "ok"}`. |

`POST /chat` requires `chat`; `POST /embed` requires `embed`. These scopes are independent, and `admin` satisfies both. A valid key missing the required scope receives **403** before any provider call.

**Request body** (`ChatRequest`, `contracts/chat.py`):

| Field | Type | Notes |
|-------|------|-------|
| `messages` | `[{role, content}]` | Required, non-empty. `role` is `user` or `assistant`; `content` non-empty. |
| `system` | string \| null | Optional system prompt. |
| `model` | string \| null | Optional. Must be one of `claude-sonnet-4-6`, `claude-opus-4-6`; defaults to `LLM_MODEL`. |
| `max_tokens` | int | Default `16000`, range `1`–`64000`. |
| `thinking` | bool \| null | Extended thinking; defaults to `THINKING_DEFAULT` when omitted. |

**Response** (`ChatResponse`): `{ "content", "model", "stop_reason", "usage": { "input_tokens", "output_tokens" } }`.

**Chat provider errors** are normalized to HTTP status: rate limit → **429**, timeout → **504**, other upstream/5xx/config faults → **502**. Validation failures (empty `messages`, unknown `model`) → **422**.

For the embedding contract, limits, and errors, see [`POST /embed`](docs/API.md#post-embed).

## Repo layout

```
llm/
├── contracts/
│   ├── chat.py            Pydantic chat request/response models
│   └── embed.py           Pydantic embedding request/response models
│
├── src/
│   ├── api/               FastAPI application
│   │   ├── app.py         App factory (create_app); mounts /chat + /embed + /health, audit middleware
│   │   ├── auth.py        Builds require_scope / get_actor from platform_auth (envelope="llm_")
│   │   ├── deps.py        get_key_store / get_llm / get_embedder — wiring via cached private builders
│   │   ├── hashing.py     Thin shim over platform_auth: llm_-envelope key generation
│   │   └── routers/
│   │       ├── chat.py    POST /chat — require_scope("chat"), maps body → provider → response
│   │       └── embed.py   POST /embed — require_scope("embed"), batch text → vectors
│   │
│   ├── providers/         Provider-agnostic model layer (no FastAPI)
│   │   ├── base.py            Chat/embedding dataclasses, Protocols + normalized error hierarchy
│   │   ├── bedrock_converse.py  Default chat: Claude via bedrock-runtime Converse API (US-regional profiles)
│   │   ├── bedrock.py           Alt chat: Claude via AnthropicBedrockMantle (Messages endpoint)
│   │   ├── openai_embed.py      Embeddings via the OpenAI API
│   │   └── registry.py          Chat provider selection and OpenAI embedding builder
│   │
│   ├── mint_key.py        llm-keys CLI — prints a key + its CONSUMER_KEYS entry, no store writes
│   └── config.py          Chat/embedding settings and credential boot checks
│
├── tests/                 Fast tests — no Docker, no network (fake provider)
├── Dockerfile             Production image (built + import-smoke-tested by CI; used by Railway).
│                          Build context is the repo root; installs via `uv sync --frozen --no-dev --package llm`.
├── docker-compose.yml     Builds/runs the image locally on port 8002 (no DB service).
└── railway.json           Railway deploy config: Dockerfile builder, /health check, NO preDeployCommand
                           (stateless — nothing to migrate).
```

**Dependency direction:** `contracts/` imports nothing from `src/`. `src/api/` depends only on `contracts/`, `src/config`, and the provider Protocols. `src/providers/` implements the `LLMProvider` and `EmbeddingProvider` Protocols and knows nothing about FastAPI. `src/api/deps.py` is the only place the concrete providers and key store get wired in — so tests override `get_llm` / `get_embedder` / `get_key_store` via `app.dependency_overrides`.

### The two Bedrock chat providers

Both bill as standard Amazon Bedrock; they differ only in the Bedrock endpoint and how model ids are formed:

- **`bedrock-converse`** (default) — `BedrockConverseProvider` calls the `bedrock-runtime` **Converse** API. Used because this account's model access is US-regional cross-region inference profiles (`us.anthropic.claude-sonnet-4-6`), which the Messages endpoint can't target. Maps neutral model names to inference-profile ids via an explicit table.
- **`bedrock`** — `BedrockClaudeProvider` calls the **Mantle Messages** endpoint via `AnthropicBedrockMantle`. Needs global/Messages model access.

In tests, neither is used: a `_FakeProvider` implementing the `LLMProvider` protocol is injected via `dependency_overrides`, so the suite runs with no AWS credentials and no network.

## Testing

The suite is single-mode — no Docker, no database, no network:

```bash
uv run pytest
```

Route tests inject fake chat and embedding providers via `app.dependency_overrides`. Adapter tests use stubbed clients or mock transports, never real provider calls. Coverage includes auth/scopes, request limits, ordered fixed-width embeddings, normalized failures, key provisioning, config/boot checks, audit metadata, and OpenAPI.

Lint and format with ruff:

```bash
uv run ruff check .
uv run ruff format .
```

**CI** runs an `llm-test` job (`.github/workflows/ci.yml`): `uv sync --extra dev`, `uv run pytest`, `ruff check`, and `ruff format --check`. A separate image job builds the Dockerfile and smoke-tests that the app imports at boot (`python -c "import src.api.app"`).

## Status

Stateless Bedrock chat and OpenAI batch embeddings, config-seeded scoped API keys (`chat` / `embed` / `admin`) with an attested-actor audit trail, neutral provider Protocols, normalized failures, and offline tests.

**Not implemented (by design):**

- **Streaming** — `/chat` returns the full completion; no SSE/token streaming.
- **Persistence** — no conversation storage, no DB. Consumers keep their own history and send it on each call.
- **Retrieval pipelines** — `/embed` returns vectors, but chunking, indexing, vector storage, and document search belong to consumers.
- **Persistent key store** — keys come from `CONSUMER_KEYS` config; a DB-backed store (with a `revoke` command) can drop in later behind the same `ApiKeyStore` protocol.

## Documentation

- [docs/API.md](docs/API.md) — consumer-facing endpoint reference: request/response shapes, errors, curl examples
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — contributor orientation: why the service is shaped this way, boundaries, trade-offs
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) — task walkthroughs and the pre-push checklist
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — deploy shape, variables, key provisioning, troubleshooting
