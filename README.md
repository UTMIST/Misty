# UTMIST Ops Platform

The internal operations infrastructure for UTMIST — a canonical, machine-queryable record of *who runs the org*, *what the org owns*, and *how people reach it*. Deployed and running in production.

UTMIST is a student org with rotating leadership and mixed technical fluency. Every year new leads inherit their predecessor's undocumented spreadsheets and lost Google Docs. This platform is our answer to that: **institutional knowledge that survives graduation.** Everything is exposed as HTTP APIs plus a Discord frontend, self-hostable if you ever want, deployed to Railway today because it's turnover-proof.

**Status:** Live on Railway (staging + production) with Neon Postgres backing each service. Merges to `staging` auto-deploy to staging; merges to `main` auto-deploy to production.

---

## What you can do with it

### As a UTMIST member (in Discord)

**Stable (registered globally — visible in every UTMIST server the bot is in):**

- **`/link`** — attach your Discord account to your directory record so the bot knows who you are. Public (you don't need to be linked yet).
- **`/verify-code`** — confirm the one-time code emailed to you to finish linking. Public.
- **`/add-email`** — verify and add another email to your directory record. Requires you to be linked.
- **`/verify-email`** — confirm the code emailed to you to finish adding that email. Requires you to be linked.
- **`/whoami`** — see your linked identity, teams you're on, and access level. Requires you to be linked.
- **`/team`** — look up, create, rename, add/remove members, and view rosters (subcommands: `create`, `list`, `rename`, `add`, `remove`, `roster`). Reads are public; writes are admin-only.
- **`/my-teams`** — list your active memberships. Requires you to be linked.
- **`/doc`** — catalog and browse links (subcommands: `add`, `list`, `show`, `remove`), backed by documentation-system. Reads are public; writes are admin-only. Team-owner field has slug autocomplete.
- **`/record`** — record the voice channel you're in and get meeting minutes back (subcommands: `start`, `status`, `stop`). Recording ends on `/record stop` **or automatically once everyone leaves the voice channel** (with a 4h backstop). On stop, the bot posts a branded `meeting-minutes.pdf` (LLM-generated title, summary, decisions, action items, full transcript) into the channel, @-mentioning whoever started the recording. Audio is never returned or persisted — it streams straight to AWS as transcription input and is never written to disk. `start` requires you to be linked; `status`/`stop` are public so a directory outage can't strand a running recording.
- **`/help`** — list the commands you can use, or show details for one. Public.

There are currently no beta commands.

### As an admin (in Discord)

- **`/seed`** — create or promote a person in the directory (member / admin / superuser). Requires admin+. Stable (visible globally). You can only grant a level at or below your own.
- **`/team create`**, **`/team add`**, **`/team remove`**, **`/team rename`** — the write subcommands are admin-gated. Stable (visible globally).

### As a developer / integration builder

Every domain has a first-class HTTP API — build your own dashboard, sync job, or automation on top:

- **[team-tracking](services/team-tracking/README.md)** — 26 endpoints across `people`, `teams`, `role_kinds`, `team_memberships`, `providers`, `person_identifiers`, `api_keys`. Full point-in-time roster queries. Scoped API keys, per-request audit log. **Actively consumed** by the Discord bot in production.
- **[documentation-system](services/documentation-system/README.md)** — endpoints over `docs` and `sources`; ingest a URL and it's normalized, dedup'd, fetched (title + snapshot for supported sources), and owner-validated against team-tracking. Ownership degrades gracefully if the directory is unreachable. **Consumed** by the Discord bot's `/doc` command group (`add`, `list`, `show`, `remove`).

Every service speaks OpenAPI. Point Swagger UI or codegen at them. (`meeting`'s WebSocket route isn't representable in OpenAPI — its wire format is documented in [`services/meeting/README.md`](services/meeting/README.md).)

The other four are internal-facing: **[llm](services/llm/README.md)** (`POST /chat` over Bedrock, `chat` scope), **[verification](services/verification/README.md)** (request/confirm an email code, `verification:write` scope), **[meeting](services/meeting/README.md)** (live transcription → minutes, `meetings` scope), and **[connectors](services/connectors/README.md)** (`POST /fetch` document content from Google sources, `fetch` scope).

### As an operator

- **Deploy is `git push`.** Merge to `staging` → Railway rebuilds and deploys staging automatically. Promote via a `staging → main` PR for production. No separate CD system.
- **Roll back is `git revert`** + push. If a migration went with it, `railway run … alembic downgrade -1` reverses the schema.
- **Manage API keys.** Three different storage models, by design:
  - **team-tracking, documentation-system** — issued into an `api_keys` table via the `team-tracking-keys` / `doc-keys` CLIs. Scoped, revocable, per-consumer, argon2-hashed at rest.
  - **llm, meeting, connectors** — no key table. `llm-keys` / `meeting-keys` / `connectors-keys` *print* a key plus a JSON entry you paste into that service's `CONSUMER_KEYS` variable; adding or revoking one is a redeploy.
  - **verification** — no per-consumer keys at all. Only the bootstrap `API_KEY` env var authenticates, since its single consumer is the bot.

---

## The services

| Service | What it holds | Status |
|---------|---------------|--------|
| [`services/team-tracking/`](services/team-tracking/README.md) | People, teams, roles, memberships, external identity mapping (Discord/GitHub/Notion/UofT email → person) | **Deployed** (staging + prod). Directory is empty on prod until seeded. |
| [`services/documentation-system/`](services/documentation-system/README.md) | Catalog of URLs (docs/sheets/repos/videos) with owners, tags, and best-effort content snapshots | **Deployed** (staging + prod). Consumed by the bot's `/doc` command group (`add`/`list`/`show`/`remove`), registered globally. |
| [`services/llm/`](services/llm/README.md) | Stateless (no DB) internal `POST /chat` API over AWS Bedrock; requires the `chat` scope | **Deployed** (staging + prod). No database — a thin proxy over Bedrock. |
| [`services/verification/`](services/verification/README.md) | Email verification: request a one-time code and confirm it, linking a subject (e.g. `discord:<id>`) to a verified email; requires the `verification:write` scope | **Deployed** (staging + prod). |
| [`services/meeting/`](services/meeting/README.md) | Meeting recording: transcribes a Discord voice session (Amazon Transcribe) and returns LLM-generated minutes as a branded PDF; no DB, nothing persisted | **Deployed** (staging). Consumed by the bot's `/record` command group; requires the `meetings` scope. |
| [`services/connectors/`](services/connectors/README.md) | Stateless outbound adapter: fetches document content (Google Docs/Sheets/Slides/Drive) on behalf of internal consumers via a service account; no DB | **Deployed** (staging). Consumed by documentation-system's Google source fetches; requires the `fetch` scope. |
| [`discord-bot/`](discord-bot/README.md) | Discord slash-command frontend + a browser-based "web playground" for iterating on commands without a Discord token | **Deployed** (staging + prod). All slash commands are stable and registered globally; 0 beta. |
| Search / retrieval | Full-text + semantic search over the catalog's snapshots | Deferred (not built) |

**How they relate.** team-tracking is the foundation — everything else references it. documentation-system validates every doc's owner against team-tracking, and asks it which teams a person is on to decide which docs that person may see. The discord-bot is the only consumer-facing surface and fans out to every service. `meeting` calls `llm` for minutes, and documentation-system calls `connectors` to fetch Google source content — the two service-to-service dependencies outside the catalog → directory pair. No service shares tables with another; the three that have a database each own it outright, and `llm`/`meeting`/`connectors` have none.

```
  documentation-system ──validates owner ids──▶ team-tracking
   (docs catalog)         resolves team ids     (directory / source of truth)
        │       ▲                                      ▲
        │ /fetch│ degrades gracefully if the directory  │
        ▼       │           is down                     │
   connectors    │  /doc                                │  /link /whoami /team /seed
   (Google docs) └──────────────── discord-bot ──────────┘
                            │        │
              /link,        │        │  /record
              /add-email    │        │
                    ▼       ▼        ▼
               verification      meeting ──/chat──▶ llm ──▶ Bedrock
               (email codes)    (transcript,        (stateless
                                 minutes, PDF)       proxy)
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the cross-service data flow.

---

## Repo layout

```
Misty/
├── README.md                          You are here
├── AGENTS.md                          Instructions for AI coding agents (CLAUDE.md points here)
├── pyproject.toml                     Root uv workspace (members: services/*, packages/*)
├── uv.lock                            Single lockfile for the whole workspace
├── docs/
│   ├── DEVELOPMENT.md                 Developer onboarding — clone to first PR
│   ├── ARCHITECTURE.md                Cross-service architecture — how the pieces fit
│   ├── MEETING-RECORDING.md           How /record splits across the bot + meeting service
│   ├── RAILWAY-DEPLOYMENT.md          Deploy runbook (Railway + Neon setup, key provisioning)
│   └── DEPLOYMENT-HISTORY.md          Design decisions, lessons learned, release log
│
├── services/                          HTTP services (each in its own folder)
│   ├── team-tracking/                 Directory service — port 8000, own Postgres
│   │   ├── README.md                  Overview + quick start
│   │   ├── src/                       FastAPI + SQLAlchemy Core
│   │   ├── contracts/                 Pydantic types + Protocols (framework-free boundary)
│   │   ├── migrations/                Alembic
│   │   ├── tests/                     pytest (in-memory + real-Postgres adapters)
│   │   ├── Dockerfile, railway.json   Production image + Railway config
│   │   └── docs/                      API.md, ARCHITECTURE.md, DEPLOYMENT.md, CONTRIBUTING.md
│   │
│   ├── documentation-system/          Catalog service — 8001, own Postgres (same shape)
│   ├── verification/                  Email one-time codes — 8003, own Postgres
│   ├── llm/                           Bedrock /chat proxy — 8002, NO database
│   ├── meeting/                       Live meeting transcription — 8004, NO database,
│   │                                   stateful (in-memory sessions)
│   └── connectors/                    Google source fetch adapter — 8005, NO database
│                                       (every service above has the same docs/ set:
│                                        API.md, ARCHITECTURE.md, CONTRIBUTING.md, DEPLOYMENT.md)
│
├── packages/
│   └── auth/                          platform_auth — shared API-key auth lib (argon2 hashing,
│                                       scopes, FastAPI deps, audit middleware); a pure leaf
│                                       consumed by all six services via thin shims
│
├── discord-bot/                       Discord frontend + web playground
│   ├── src/                           Node.js + discord.js
│   ├── scripts/dev-web.js             Local playground orchestrator (ephemeral scratch DB)
│   ├── docs/CONTRIBUTING.md           Adding commands, clients, and auth policies
│   ├── Dockerfile, railway.json       Production image + Railway config
│   └── test/                          node --test
│
├── scripts/
│   └── provision-directory-key.sh     Mint + wire scoped API keys per environment
│
└── .github/
    ├── CODEOWNERS                     Per-area reviewers; zones mirror pr-zone-check
    ├── PULL_REQUEST_TEMPLATE.md       Zone, verification steps, deployment notes
    ├── ISSUE_TEMPLATE/                Bug / feature / epic templates (prompt the `Blocked by:` line)
    └── workflows/
        ├── ci.yml                     Tests + lint + Docker builds on every PR (10 jobs)
        ├── main-source-guard.yml      Enforces "PRs to main come from staging"
        ├── pr-zone-check.yml          Warns on PRs spanning multiple CODEOWNERS zones
        ├── discord-pr-notify.yml      Posts to Discord when a PR needs review
        └── blocked-ready-automation.yml   Syncs blocked/ready issue labels
```

Each service is self-contained: its own tests, its own docs, and its own database *if it needs one* — `llm`, `meeting`, and `connectors` deliberately have none. Dependencies are managed as one uv workspace rooted at this repo's `pyproject.toml`/`uv.lock`, and all six services share one leaf, `packages/auth` (`platform_auth`), for API-key auth — a shared *library* dependency, not a dependency between services, which remain independent of each other. Add a new service by dropping it in `services/` following the same shape (and adding its CI job in the same PR).

---

## Running the platform locally

New here? Start with the **[developer onboarding guide](docs/DEVELOPMENT.md)** — it walks a fresh clone through prerequisites, running the platform in order, and your first contribution.

Nothing to bootstrap at the root — stand up only what you need:

- **Directory API** — [`services/team-tracking/README.md` → Quick start](services/team-tracking/README.md#quick-start). Port **8000**, Postgres **5433**.
- **Catalog API** — [`services/documentation-system/README.md` → Quick start](services/documentation-system/README.md#quick-start). Port **8001**, Postgres **5434**.
- **LLM API** — [`services/llm/README.md`](services/llm/README.md). Port **8002**, no database, no Docker.
- **Verification API** — [`services/verification/README.md`](services/verification/README.md). Port **8003**, Postgres **5434**. Defaults to `EMAIL_BACKEND=fake`, so no mail credentials are needed locally.
- **Meeting API** — [`services/meeting/README.md`](services/meeting/README.md). Port **8004**, no database, no Docker.
- **Connectors API** — [`services/connectors/README.md`](services/connectors/README.md). Port **8005**, no database, no Docker.
- **Discord bot** — [`discord-bot/README.md`](discord-bot/README.md). Two modes:
  - `npm start` — real Discord surface (needs a bot token).
  - `npm run dev:web` — browser-based playground on `http://localhost:3001`, no Discord token needed. Orchestrates its own scratch team-tracking + ephemeral DB, so it's fully self-contained for hacking on commands. Note that `/record` has no playground equivalent — voice capture needs the real Discord surface.

> ⚠️ **documentation-system and verification both bind host port 5434** for their dev Postgres, so they can't run locally at the same time as configured. Remap one (`-p 5435:5432`) and update its `DATABASE_URL`. Deployments are unaffected — each has its own Neon project.

For catalog-with-real-ownership-validation, run team-tracking first and point the catalog's `DIRECTORY_*` config at it. For `/record`, start `llm` before `meeting` — `meeting` calls it for minutes and refuses to boot without `LLM_BASE_URL` outside `local`.

---

## Working conventions

All six services are built the same way on purpose — learning one gives you 80% of the others. (`llm`, `meeting`, and `connectors` follow every convention below *except* the storage/migration ones: they own no database.)

- **`contracts/` Protocol boundary.** Each service has a `contracts/` package of Pydantic domain types plus `Protocol` interfaces. Application code depends on the Protocols, never on a concrete implementation.
- **Swappable storage adapters.** `InMemoryStorageAdapter` for fast tests, `PostgresStorageAdapter` for real runs — both satisfy the same Protocol. Tests use in-memory; a small integration test suite gates the Postgres adapter too.
- **Scoped API-key auth.** Every request carries `X-API-Key`. Keys are argon2-hashed with a set of per-resource scopes (`people:read`, `teams:write`, `chat`, `meetings`, `fetch`, etc.). This machinery is implemented once in the shared [`packages/auth`](packages/auth) (`platform_auth`) library and consumed by all six services through a thin shim (`src/api/auth.py`, plus `hashing.py` for the five services that mint their own keys) that binds its own key prefix and config. Anything the library exposes ready-to-use — `AuditLogMiddleware`, for one — is imported from `platform_auth` directly; a per-service file earns its place only by binding something. The three DB-backed services store keys in an `api_keys` table and mint them via a CLI; `llm`, `meeting`, and `connectors` seed them from a `CONSUMER_KEYS` JSON env var instead, so rotating one there is a redeploy.
- **Credentials are `SecretStr`, never `str`.** Every credential field in a service's `Settings` is `pydantic.SecretStr`, so it renders as `**********` in any repr, log line, traceback, or failing assertion diff — a plain `str` once printed a real Google private key into a transcript. Unwrap with `.get_secret_value()` at the boundary; `platform_auth` still takes plain `str`. See [`packages/auth/README.md` → Credential config convention](packages/auth/README.md#credential-config-convention) for the one way forgetting to unwrap fails *silently* rather than loudly.
- **Attested actor.** The `created_by`/`updated_by` on every audit field is the authenticated key's own name — a caller can't claim to be someone else.
- **Per-request audit log.** Middleware emits one JSON line per request with the resolved actor, endpoint, status, and duration.
- **Alembic migrations.** Schema changes are versioned; migrations run as Railway's `preDeployCommand` on every deploy.
- **API-only, nothing runs inside.** No in-process consumers; everything talks to these services over HTTP.
- **CI-gated changes.** Every PR to `staging` or `main` runs [`ci.yml`](.github/workflows/ci.yml) — full test suites against real Postgres, ruff, and Docker builds with boot smoke tests. PRs to `main` also run [`main-source-guard`](.github/workflows/main-source-guard.yml).
- **Branching = deploy.** `staging` merges deploy to Railway staging; `main` merges deploy to production.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for why these choices exist and how they compose.

---

## Where to go next

Depending on what you're here to do:

**Deploying / operating**
- [`docs/RAILWAY-DEPLOYMENT.md`](docs/RAILWAY-DEPLOYMENT.md) — the runbook (deploy, redeploy, provision keys, register commands, verify).
- [`docs/DEPLOYMENT-HISTORY.md`](docs/DEPLOYMENT-HISTORY.md) — why the platform is deployed the way it is, and the non-obvious lessons.

**Building against the APIs**
- [`services/team-tracking/docs/API.md`](services/team-tracking/docs/API.md) — all 26 endpoints with request/response shapes.
- [`services/documentation-system/docs/API.md`](services/documentation-system/docs/API.md) — ingest, retrieve, and update the catalog.

**Contributing code**
- [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) — onboarding: clone → running locally → first PR.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the cross-service picture.
- Then whichever service you're touching: [`services/team-tracking/`](services/team-tracking/README.md), [`services/documentation-system/`](services/documentation-system/README.md), or [`discord-bot/`](discord-bot/README.md).
- Every service, plus `discord-bot` and `packages/auth`, has a `docs/CONTRIBUTING.md` with task walkthroughs and a pre-push checklist.

**Working with an AI coding agent**
- [`AGENTS.md`](AGENTS.md) — the compressed set of invariants, workflow rules, and repo-specific gotchas an agent needs. `CLAUDE.md` is a pointer to it, so there is one source of truth.

**New to the platform?** Start here, read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), then dive into whichever service you're most likely to touch.
