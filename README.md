# UTMIST Ops Platform

The internal operations infrastructure for UTMIST — a canonical, machine-queryable record of _who runs the org_, _what the org owns_, and _how people reach it_. Deployed and running in production.

UTMIST is a student org with rotating leadership and mixed technical fluency. Every year new leads inherit their predecessor's undocumented spreadsheets and lost Google Docs. This platform is our answer to that: **institutional knowledge that survives graduation.** Everything is exposed as HTTP APIs plus a Discord frontend, self-hostable if you ever want, deployed to Railway today because it's turnover-proof.

**Status:** Live on Railway (staging + production). The directory, catalog, verification service and gateway each own a Neon Postgres database. Merges to `staging` auto-deploy to staging; merges to `main` auto-deploy to production.

**Using Misty in Discord?** Start with the [Misty user guide](docs/wiki/Home.md).

---

## What you can do with it

### As a UTMIST member (in Discord)

**Stable (registered globally — visible in every UTMIST server the bot is in):**

- **`/link`** — attach your Discord account to your directory record so the bot knows who you are. Public (you don't need to be linked yet).
- **`/verify-code`** — confirm the one-time code emailed to you to finish linking. Public.
- **`/add-email`** — verify and add another email to your directory record. Requires you to be linked.
- **`/verify-email`** — confirm the code emailed to you to finish adding that email. Requires you to be linked.
- **`/whoami`** — see your linked identity, teams you're on, and access level. Requires you to be linked.
- **`/team`** — look up, create, rename, add/remove members, and view rosters; also inspect or sync a team's Google Group. Ordinary reads require a linked identity; writes and both `google-status` / `google-sync` are admin-only. See the [bot command reference](discord-bot/README.md#commands).
- **`/my-teams`** — list your active memberships. Requires you to be linked.
- **`/doc`** — catalog and browse links (subcommands: `add`, `list`, `search`, `remove`), backed by documentation-system. `add`/`list`/`search` require a linked identity; `remove` is admin-only. Team-owner field has slug autocomplete.
- **`/record`** — record the voice channel you're in and get meeting minutes back (subcommands: `start`, `status`, `stop`). When the first human enters an empty voice channel, the bot sends them a direct message prompting them to run `/record start`. Recording ends on `/record stop` **or automatically once everyone leaves the voice channel** (audio ingestion stops accepting frames after the configured duration cap; the cap does not finalize the meeting). On stop, the bot posts a branded PDF and, after saving succeeds, the Google Doc link with minutes and the full transcript in the originating channel or thread, @-mentioning whoever started the recording. Recipients need their own Drive access; `/record start save:false` keeps PDF only. Delayed saves and link delivery recover automatically; see the [delivery runbook](discord-bot/docs/meeting-delivery.md). Status reports Misty's current voice channel, starts are refused while a recording is already active in the server, and stops must come from the recorded voice channel unless it's already empty (the escape hatch for a runaway recording); refused actions explain the active channel and auto-stop behavior. Audio is never returned or persisted — it streams straight to AWS as transcription input and is never written to disk. `start` requires you to be linked; `status`/`stop` are public so a directory outage can't strand a running recording.
- **Misty helper threads** — start a question with `@Misty`; Misty opens a thread and
  answers with its recent history as context. Inside a thread Misty created, reply
  directly to one of Misty's messages with the reply ping on to continue without
  typing another mention. Ping-off replies, replies to people, and replies outside
  Misty-created threads are ignored. A typed leading `@Misty` remains an independent
  trigger.
- **`/help`** — list the commands you can use, or show details for one. Public.
- **`/bug`** — where to report a bug (GitHub issue link and optional infrastructure contact). Public.
- **DM Misty directly** — linked members can start a private helper conversation and send follow-ups without a server channel or @mention. DM history is isolated per Discord user.
- **Use plain language for documents and commands** — with `HELPER_TOOLS_ENABLED=true`,
  ask about accessible documents, look up your profile and teams, or request actions such
  as adding a teammate or starting a recording. Misty uses the existing command
  permissions and shows a private preview for confirmation before writes. DMs use
  your document access; server answers use the channel's configured teams. See
  [natural-language commands](discord-bot/README.md#commands-through-mentions-and-dms)
  and the [user guide](docs/wiki/Talking-to-Misty.md).

There are currently no beta commands.

**In progress (available globally):** `/qna question:<text>` privately answers questions about the current recording's last 15 minutes. Run it in the text channel where recording started. Replies include speaker/timestamp references and disclose any shortened window. See the [bot command documentation](discord-bot/README.md#commands).

### As an admin (in Discord)

- **`/seed`** — create a new person in the directory (member / admin / superuser). Requires admin+. Stable (visible globally). You can only grant a level at or below your own. An existing person is rejected; `/seed` does not promote them.
- **`/team create`**, **`/team add`**, **`/team remove`**, **`/team rename`** — the write subcommands are admin-gated. Stable (visible globally).
- **`/channel-access`** — show, set, append, or clear which teams' documents Misty may use when answering in the current channel (subcommands: `show`, `set`, `append`, `clear`). Admin-only. `set` replaces the list; `append` unions onto it. Threads inherit the parent channel's scope. Drive access stays personal.
- **`/helper-limits show` / `set`** — inspect or change the helper request allowance.
  Changes preserve current usage and last until the bot restarts; see
  [helper limits](discord-bot/docs/helper-limits.md).

### As a developer / integration builder

Every domain has a first-class HTTP API — build your own dashboard, sync job, or automation on top:

- **[team-tracking](services/team-tracking/README.md)** — Endpoints across `people`, `teams`, `role_kinds`, `team_memberships`, `providers`, `person_identifiers`, `channels`, and `api_keys`. Full point-in-time roster queries. Scoped API keys, per-request audit log. **Actively consumed** by the Discord bot in production.
- **[documentation-system](services/documentation-system/README.md)** — catalog, authorized search, source discovery/refresh and durable meeting records. Supported URLs provide full text for indexing plus a bounded preview. The bot uses it for `/doc` and document questions; the meeting service hands it finalized text for Google Doc persistence. Ownership-label lookup can degrade during a directory outage; authorized retrieval fails closed.
- **[gateway](services/gateway/README.md)** — the one **public** service. A narrow, scoped, rate-limited door onto the directory for external consumers (e.g. a GitHub Action) that shouldn't hold an internal team-tracking key. First endpoint: `GET /v1/resolve/discord/{github_login}`, returning only a Discord id.

Every service speaks OpenAPI. Point Swagger UI or codegen at them. (`meeting`'s WebSocket route isn't representable in OpenAPI — its wire format is documented in [`services/meeting/README.md`](services/meeting/README.md).)

The other four are internal-facing: **[llm](services/llm/README.md)** (`POST /chat` via OpenAI or Bedrock, `chat` scope; embeddings use `POST /embed`), **[verification](services/verification/README.md)** (request/confirm an email code, `verification:write` scope), **[meeting](services/meeting/README.md)** (live transcription → minutes, `meetings` scope), and **[connectors](services/connectors/README.md)** (Google/GitHub content fetch, discovery and Google meeting-Doc writer operations; scopes depend on the endpoint).

### As an operator

- **Deploy is `git push`.** Merge to `staging` → Railway rebuilds and deploys staging automatically. Promote via a `staging → main` PR for production. No separate CD system.
- **Roll back through a revert PR.** Follow the [rollback procedure](docs/RAILWAY-DEPLOYMENT.md#redeploy-and-rollback), including migration-specific recovery; downgrade does not necessarily restore cleaned-up data.
- **Manage API keys.** Three different storage models, by design:
  - **team-tracking, documentation-system, gateway** — issued into separate `api_keys` tables via each service’s key CLI. Gateway accepts issued `gw_` keys only; it has no bootstrap env key. Scoped, revocable, per-consumer, argon2-hashed at rest.
  - **llm, meeting, connectors** — no key table. `llm-keys` / `meeting-keys` / `connectors-keys` _print_ a key plus a JSON entry you paste into that service's `CONSUMER_KEYS` variable; adding or revoking one is a redeploy.
  - **verification** — no per-consumer keys at all. Only the bootstrap `API_KEY` env var authenticates, since its single consumer is the bot.

---

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
│   ├── WIKI.md                        Maintaining and publishing the user guide
│   └── wiki/                          Member/admin guides and example graphics
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
