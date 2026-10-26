# Misty: UTMIST Internal Operations Platform

Misty is a Discord-based assistant for UTMIST, connecting UTMIST's team directory,
Google document access, and meeting notes. Team members can chat with her on Discord
to find teammates, transcribe meetings, and search for documentation.

Misty tracks team memberships and syncs Discord profiles with Google Drive
permissions so that team resources are shared with the right members.

## For users

- **[Misty user guide](https://github.com/UTMIST/Misty/wiki)**: setup, teams,
  document sharing, conversations and meetings for members and administrators.
- **[Releases](https://github.com/UTMIST/Misty/releases)**: user-facing changes and
  release updates.

## For developers

This repository's documentation is for developers and operators building and
running Misty. It describes implementation contracts, local development,
testing, deployment and recovery.

| Task | Start here |
|---|---|
| Run the platform locally | [Developer onboarding](docs/DEVELOPMENT.md) |
| Understand service boundaries and data flow | [Architecture](docs/ARCHITECTURE.md) |
| Get credentials and access | [Access guide](docs/ACCESS.md) |
| Join the team and pick up work | [Contributor onboarding](docs/ONBOARDING.md) |
| Change a service | Its README, API reference and `docs/CONTRIBUTING.md`, linked below |
| Deploy or recover a service | [Railway runbook](docs/RAILWAY-DEPLOYMENT.md) |
| Ship a release and update the user wiki | [Release guide](docs/RELEASING.md) |

The platform runs on Railway with separate staging and production environments.
Merges to `staging` deploy to staging; production changes arrive through a
`staging` → `main` promotion PR. Each database-backed service owns its database.

## The services

| Service | Responsibility | Consumers and configuration |
| --- | --- | --- |
| [team-tracking](services/team-tracking/README.md) | People, teams, memberships, identities, channel scopes and managed Google Groups | Directory for the bot, catalog and gateway; Google Groups sync requires its own OAuth setup. |
| [documentation-system](services/documentation-system/README.md) | Catalog, indexed full text, authorized search, source grants and durable meeting records | Used by `/doc`, helper document questions, discovery/refresh jobs and the meeting hand-off. |
| [llm](services/llm/README.md) | Stateless chat, client-executed tool transport and embeddings | Bot helper, meeting minutes/Q&A and catalog embeddings; providers configured per operation. |
| [verification](services/verification/README.md) | Email ownership codes | The bot uses it for account linking and additional verified emails. |
| [meeting](services/meeting/README.md) | Live transcription, minutes/PDF and live Q&A | `/record` and `/qna`; completed text can be handed to the catalog. One process owns live sessions. |
| [connectors](services/connectors/README.md) | Google/GitHub fetch, Google discovery and meeting-Doc operations | Catalog consumer with separate read, discovery and writer scopes/credentials; no local database. |
| [gateway](services/gateway/README.md) | Scoped public API backed by private services | `GET /v1/resolve/discord/{github_login}`; external consumers need an issued `gw_` key. |
| [discord-bot](discord-bot/README.md) | Slash commands, mention/DM helper, voice recording and local web playground | Commands register globally. Live Q&A remains in progress; helper tools require explicit activation. |

This is the implemented surface. Use the [deployment runbook](docs/RAILWAY-DEPLOYMENT.md)
for environment setup and acceptance; record deployment results in the release PR.

**How they relate.** Consumers use authenticated HTTP; services never share
tables. The catalog calls the directory for ownership and access context,
connectors for source content/ACLs, and llm for embeddings. Meeting calls llm
for minutes and hands saved text to the catalog, which uses the connector's
Google writer. The bot calls the internal APIs; gateway exposes curated public
operations backed by the directory. Ownership-label lookup can degrade during
a directory outage, while authorized retrieval fails closed. Each of the four
database-backed services owns its database; llm, meeting and connectors have none.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the cross-service data flow.

---

## Repo layout

```
Misty/
├── README.md                          You are here
├── AGENTS.md                          Instructions for AI coding agents (CLAUDE.md points here)
├── Makefile                           make check / test / lint / format across every package
├── pyproject.toml                     Root uv workspace (members: services/*, packages/*)
├── uv.lock                            Single lockfile for the whole workspace
├── docs/
│   ├── ONBOARDING.md                  Joining the team — zones, getting work, PR norms, expectations
│   ├── DEVELOPMENT.md                 Developer onboarding — clone to first PR
│   ├── ACCESS.md                      Which credentials you need (mostly none) and who to ask
│   ├── ARCHITECTURE.md                Cross-service architecture — how the pieces fit
│   ├── MEETING-RECORDING.md           How /record splits across the bot + meeting service
│   ├── RAILWAY-DEPLOYMENT.md          Deploy runbook (Railway + Neon setup, key provisioning)
│   ├── CODE-OWNERSHIP.md              Zones — what they are, who owns each, PR scope
│   └── RELEASING.md                   Release notes and publication of the separate user wiki
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
│   ├── llm/                           Chat/embedding proxy — 8002, NO database
│   ├── meeting/                       Live meeting transcription — 8004, NO database,
│   │                                   stateful (in-memory sessions)
│   ├── connectors/                    Source fetch/discovery/writer — 8005, NO database
│   └── gateway/                       External API gateway — 8006, own Postgres (external
│                                       key registry). The one PUBLIC service: scoped,
│                                       rate-limited, curated read surface
│                                       (every service above has the same docs/ set:
│                                        API.md, ARCHITECTURE.md, CONTRIBUTING.md, DEPLOYMENT.md)
│
├── packages/
│   └── auth/                          platform_auth — shared API-key auth lib (argon2 hashing,
│                                       scopes, FastAPI deps, audit middleware); a pure leaf
│                                       consumed by all seven services via thin shims
│
├── discord-bot/                       Discord frontend + web playground
│   ├── src/                           Node.js + discord.js
│   ├── scripts/dev-web.js             Local playground orchestrator (ephemeral scratch DB)
│   ├── docs/CONTRIBUTING.md           Adding commands, clients, and auth policies
│   ├── Dockerfile, railway.json       Production image + Railway config
│   └── test/                          node --test
│
├── scripts/
│   ├── check-labels.mjs              Fails CI when the zone or area list drifts between its copies
│   └── provision-directory-key.sh    Mint + wire scoped API keys per environment
│
└── .github/
    ├── CODEOWNERS                     Per-zone reviewers; zones mirror pr-zone-check
    ├── labeler.yml                    Path globs -> `zone: *` PR labels
    ├── PULL_REQUEST_TEMPLATE.md       Zone, verification steps, deployment notes
    ├── ISSUE_TEMPLATE/                Bug / feature / epic issue forms (Blocked by + Area fields)
    └── workflows/
        ├── ci.yml                     Tests + lint + Docker builds on every PR (11 jobs)
        ├── main-source-guard.yml      Enforces "PRs to main come from staging"
        ├── pr-zone-check.yml          Warns on PRs spanning multiple CODEOWNERS zones
        ├── label-consistency.yml      Fails when the zone or area list drifts (runs check-labels.mjs)
        ├── codeowners-valid.yml       Fails when a CODEOWNERS owner can't actually review
        ├── zone-label.yml             Labels a PR with the zone it touches
        ├── area-label-issues.yml      Labels an issue from its form's Area dropdown
        ├── pr-size-label.yml          Labels a PR size/xs .. size/xl
        ├── discord-pr-notify.yml      Posts to Discord when a PR needs review
        └── blocked-ready-automation.yml   Syncs blocked/ready issue labels
```

Each service is self-contained: its own tests, its own docs, and its own database _if it needs one_ — `llm`, `meeting`, and `connectors` deliberately have none. Dependencies are managed as one uv workspace rooted at this repo's `pyproject.toml`/`uv.lock`, and all seven services share one leaf, `packages/auth` (`platform_auth`), for API-key auth — a shared _library_ dependency, not a dependency between services, which remain independent of each other. Add a new service by dropping it in `services/` following the same shape (and adding its CI job in the same PR).

---

## Running the platform locally

New here? Start with the **[developer onboarding guide](docs/DEVELOPMENT.md)** — it walks a fresh clone through prerequisites, running the platform in order, and your first contribution. Joining as a contributor? [`docs/ONBOARDING.md`](docs/ONBOARDING.md) covers the process side — zones, getting work, and PR norms.

Nothing to bootstrap at the root to _run_ a service — stand up only what you need. To _verify_ a change, there is one root command:

```bash
make install   # uv workspace + bot dependencies
make check     # lint + format check + fast tests, across everything
make test-full # collects all suites; DB suites still need explicit opt-ins
               # and disposable databases — see the testing guide below
make help      # the rest
```

For storage/migration changes, follow the [database test setup](docs/DEVELOPMENT.md#database-test-setup) before using `test-full`; starting Postgres alone does not enable every suite.

Stand up individual services as needed:

- **Directory API** — [`services/team-tracking/README.md` → Quick start](services/team-tracking/README.md#quick-start). Port **8000**, Postgres **5433**.
- **Catalog API** — [`services/documentation-system/README.md` → Quick start](services/documentation-system/README.md#quick-start). Port **8001**, Postgres **5434**.
- **LLM API** — [`services/llm/README.md`](services/llm/README.md). Port **8002**, no database, no Docker.
- **Verification API** — [`services/verification/README.md`](services/verification/README.md). Port **8003**, Postgres **5434**. Defaults to `EMAIL_BACKEND=fake`, so no mail credentials are needed locally.
- **Meeting API** — [`services/meeting/README.md`](services/meeting/README.md). Port **8004**, no database, no Docker.
- **Connectors API** — [`services/connectors/README.md`](services/connectors/README.md). Port **8005**, no database, no Docker.
- **Gateway API** — [`services/gateway/README.md`](services/gateway/README.md). Port **8006**, Postgres **5435**; requires an issued `gw_` key.
- **Discord bot** — [`discord-bot/README.md`](discord-bot/README.md). Two modes:
  - `npm start` — real Discord surface (needs a bot token).
  - `npm run dev:web` — browser-based playground on `http://localhost:3001`, no Discord token needed. Orchestrates its own scratch team-tracking + ephemeral DB, so it's fully self-contained for hacking on commands. Voice capture needs the real Discord surface; **Meetings → Preview meeting delivery** simulates stop, link delivery and recovery offline.

> ⚠️ **documentation-system and verification both bind host port 5434** for their dev Postgres, so they can't run locally at the same time as configured. Remap one to an unused host port and update its `DATABASE_URL`; gateway already uses 5435, so 5436 is an option if free. Deployments are unaffected — each has its own Neon project.

For catalog-with-real-ownership-validation, run team-tracking first and point the catalog's `DIRECTORY_*` config at it. For `/record`, start `llm` before `meeting`. Saved Google Docs also need the [meeting-persistence dependencies](services/documentation-system/docs/DEPLOYMENT.md#meeting-record-persistence); the PDF-only path does not prove that setup works.

---

## Working conventions

All seven services are built the same way on purpose — learning one gives you 80% of the others. (`llm`, `meeting`, and `connectors` follow every convention below _except_ the storage/migration ones: they own no database.)

- **`contracts/` Protocol boundary.** Each service has a `contracts/` package of Pydantic domain types plus `Protocol` interfaces. Application code depends on the Protocols, never on a concrete implementation.
- **Swappable storage adapters.** `InMemoryStorageAdapter` for fast tests, `PostgresStorageAdapter` for real runs — both satisfy the same Protocol. Tests use in-memory; a small integration test suite gates the Postgres adapter too.
- **Scoped API-key auth.** Protected HTTP endpoints use `X-API-Key`; health and API schema/UI routes remain unauthenticated within the service’s network exposure. Keys are argon2-hashed with a set of per-resource scopes (`people:read`, `teams:write`, `chat`, `meetings`, `fetch`, etc.). This machinery is implemented once in the shared [`packages/auth`](packages/auth) (`platform_auth`) library and consumed by all seven services through a thin shim (`src/api/auth.py`, plus `hashing.py` for the services that mint their own keys) that binds its own key prefix and config. Anything the library exposes ready-to-use — `AuditLogMiddleware`, for one — is imported from `platform_auth` directly; a per-service file earns its place only by binding something. team-tracking, documentation-system, and gateway store consumer keys in their own `api_keys` tables; `llm`, `meeting`, and `connectors` seed them from a `CONSUMER_KEYS` JSON env var instead, so rotating one there is a redeploy.
- **Credentials are `SecretStr`, never `str`.** Every credential field in a service's `Settings` is `pydantic.SecretStr`, so it renders as `**********` in any repr, log line, traceback, or failing assertion diff — a plain `str` once printed a real Google private key into a transcript. Unwrap with `.get_secret_value()` at the boundary; `platform_auth` still takes plain `str`. See [`packages/auth/README.md` → Credential config convention](packages/auth/README.md#credential-config-convention) for the one way forgetting to unwrap fails _silently_ rather than loudly.
- **Attested actor.** The `created_by`/`updated_by` on every audit field is the authenticated key's own name — a caller can't claim to be someone else.
- **Per-request audit log.** Middleware emits one JSON line per request with the resolved actor, endpoint, status, and duration.
- **Alembic migrations.** Schema changes are versioned; migrations run as Railway's `preDeployCommand` on every deploy.
- **API-only consumer boundary.** Consumers use authenticated HTTP. Services can own internal work such as directory Google-group reconciliation; they do not share tables or import each other.
- **CI-gated changes.** Every PR to `staging` or `main` runs [`ci.yml`](.github/workflows/ci.yml) — full test suites against real Postgres, ruff, and Docker builds with boot smoke tests. PRs to `main` also run [`main-source-guard`](.github/workflows/main-source-guard.yml).
- **Branching = deploy.** `staging` merges deploy to Railway staging; `main` merges deploy to production.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for why these choices exist and how they compose.

---

## Where to go next

Depending on what you're here to do:

**Deploying / operating**

- [`docs/RAILWAY-DEPLOYMENT.md`](docs/RAILWAY-DEPLOYMENT.md) — the runbook (deploy, redeploy, provision keys, register commands, verify).
- [`docs/RELEASING.md`](docs/RELEASING.md) — production promotion, release notes and publishing user-guide changes from the separate wiki repository.

**Building against the APIs**

- [`services/team-tracking/docs/API.md`](services/team-tracking/docs/API.md) — directory endpoints with request/response shapes.
- [`services/documentation-system/docs/API.md`](services/documentation-system/docs/API.md) — ingest, retrieve, and update the catalog.

**Contributing code**

- [`docs/ONBOARDING.md`](docs/ONBOARDING.md) — joining the team: zones, getting work, PR norms, and what's expected of you.
- [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) — onboarding: clone → running locally → first PR.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the cross-service picture.
- Then whichever service you're touching: [`services/team-tracking/`](services/team-tracking/README.md), [`services/documentation-system/`](services/documentation-system/README.md), or [`discord-bot/`](discord-bot/README.md).
- Every service, plus `discord-bot` and `packages/auth`, has a `docs/CONTRIBUTING.md` with task walkthroughs and a pre-push checklist.

**Working with an AI coding agent**

- [`AGENTS.md`](AGENTS.md) — the compressed set of invariants, workflow rules, and repo-specific gotchas an agent needs. `CLAUDE.md` is a pointer to it, so there is one source of truth.

**New to the platform?** Start here, read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), then dive into whichever service you're most likely to touch.
