# Developer onboarding

The single path from a fresh clone to your first merged change. It ties the
per-service docs together rather than repeating them — where a service README
already explains something, this guide links to it.

Two tracks:

- **Know this stack already?** Follow the [Quickstart](#quickstart) and skip the rest.
- **New to Python/Node/Docker, or new to this repo?** Read
  [Prerequisites](#prerequisites-new-to-this-stack) first, then work down the page.

This page is the *technical* path. The *process* side — zone ownership, how
work gets assigned, PR review norms, what's expected of you — is
[`ONBOARDING.md`](ONBOARDING.md).

The platform is **seven backend services plus the Discord bot**. You almost never
need all of them at once — stand up only what you're touching.

| Service | Port | DB | What it is |
|---------|------|----|------------|
| [`team-tracking`](../services/team-tracking/README.md) | 8000 | Postgres :5433 | The directory — people, teams, roles, identities. Source of truth. |
| [`documentation-system`](../services/documentation-system/README.md) | 8001 | Postgres :5434 | The docs catalog. Validates owners against team-tracking; per-doc visibility + grants. |
| [`llm`](../services/llm/README.md) | 8002 | — | Stateless chat/embedding proxy; OpenAI default, optional Bedrock chat. No DB. |
| [`verification`](../services/verification/README.md) | 8003 | Postgres :5434 ⚠️ | Email one-time codes (request / confirm). Backs `/link` and `/add-email`. |
| [`meeting`](../services/meeting/README.md) | 8004 | — | **Stateful.** Live voice transcription → minutes/PDF; saved text is handed to the catalog. No local DB. |
| [`connectors`](../services/connectors/README.md) | 8005 | — | Google/GitHub content, Google ACLs/discovery and meeting-Doc writes. No local DB. |
| [`gateway`](../services/gateway/README.md) | 8006 | Postgres :5435 | Public scoped API; owns its issued-key registry. |
| [`discord-bot`](../discord-bot/README.md) | 3001 (playground) | — | Slash commands, mention/DM helper and voice recording. The browser playground needs no Discord token; its helper is text-only. |

> ⚠️ **Known port clash:** `documentation-system` and `verification` both bind
> host port **5434** for their dev Postgres
> (`services/*/docker-compose.yml`). They cannot run at the same time as
> written. If you need both, override one:
> use an unused host port in a local Compose override and point that service's
> `DATABASE_URL` at it. Gateway already uses 5435; 5436 is an option if free.

`team-tracking`, `documentation-system`, `verification`, and `gateway` own a database
(and therefore Alembic migrations). `llm`, `meeting`, and `connectors` are
DB-free.

---

## Quickstart

Prerequisites: Docker, Python 3.11+, [`uv`](https://github.com/astral-sh/uv), Node 22.12+, git (see the runtime mismatch below).

**Directory API (team-tracking):**

```bash
cd services/team-tracking
cp .env.example .env
docker compose up -d postgres
uv sync --extra dev
uv run alembic upgrade head
uv run uvicorn src.api.app:app --reload --port 8000
```

Swagger at `http://localhost:8000/docs`. Dev API key: `dev-api-key-change-me`
(pass as `X-API-Key`).

**Catalog API (documentation-system)** — same shape, different ports:

```bash
cd services/documentation-system
cp .env.example .env
docker compose up -d postgres
uv sync --extra dev
uv run alembic upgrade head
uv run uvicorn src.api.app:app --reload --port 8001
```

Swagger at `http://localhost:8001/swagger` (not `/docs` — that path is a real
router here). Same dev key. Its Postgres runs on **5434** so it coexists with
team-tracking's on **5433**.

**Verification API** — same shape again, port **8003**, Postgres **5434** (see
the clash warning above):

```bash
cd services/verification
cp .env.example .env
docker compose up -d postgres
uv sync --extra dev
uv run alembic upgrade head
uv run uvicorn src.api.app:app --reload --port 8003
```

`EMAIL_BACKEND=fake` is the default — it drops mail instead of sending, so no
Resend/Gmail credentials are needed for local work.

**LLM API** — no Docker, no database:

```bash
cd services/llm
cp .env.example .env
uv sync --extra dev
uv run uvicorn src.api.app:app --reload --port 8002
```

`POST /chat` requires the `chat` scope and credentials for the selected
provider: OpenAI by default, or AWS for an explicitly selected Bedrock backend.
Local mode can boot and answer `/health` without provider credentials; that
does not verify a real chat/embedding request.

**Meeting API** — no Docker, no database, no ffmpeg binary (PyAV bundles its
own):

```bash
cd services/meeting
cp .env.example .env
uv sync --extra dev
uv run uvicorn src.api.app:app --reload --port 8004
```

Live transcription needs AWS Transcribe credentials plus a reachable `llm`
service; the test suite fakes both, so you only need those to exercise a real
recording.

**Connectors API** — no Docker, no database:

```bash
cd services/connectors
cp .env.example .env
uv sync --extra dev
uv run uvicorn src.api.app:app --reload --port 8005
```

`POST /fetch` requires the `fetch` scope. Google credentials are **optional** —
with `GOOGLE_CREDENTIALS_JSON` empty the service still boots and answers
`/health`, and `/fetch` returns 503 for Google sources. That's a supported
running state, not a broken one.

**Discord bot — playground mode** (recommended dev loop, no Discord token —
but Docker must be running):

```bash
cd discord-bot
cp .env.example .env
npm install
npm run dev:web
```

Open `http://127.0.0.1:3001`, paste `100000000000000000` into "Acting as", pick
a command, run it. `Ctrl+C` tears the whole stack down.

This one command boots its own scratch team-tracking + throwaway DB, so you do
**not** need port 8000 running for it — but it is not free of infrastructure.
It will:

- run `docker compose up -d postgres` in `services/team-tracking`, i.e. bring up
  **Postgres on 5433** and clone `team_tracking` into a scratch
  `team_tracking_playground` database;
- spawn its own team-tracking process on **port 8001**.

> ⚠️ Port **8001** is also `documentation-system`'s API port. Stop the catalog
> before running `npm run dev:web`, or the scratch instance fails to bind.

Stuck, or new to this stack? Keep reading.

---

## Prerequisites (new to this stack?)

Install these once per machine. All seven services are Python; the bot is Node.
Docker is needed by the four DB-backed services **and by the bot's playground**.

- **git** — you have it if `git --version` works.
- **Docker** ([Docker Desktop](https://www.docker.com/products/docker-desktop/)) —
  runs the local Postgres for the four DB-backed services.
  `docker compose up -d postgres` starts it; the data lives in a named volume
  that survives reboots. Not needed for `llm` or `meeting`, but it **is** needed
  for the bot's playground: `npm run dev:web` shells out to
  `docker compose up -d postgres` inside `services/team-tracking` and clones a
  scratch database from it, so Docker must be running even though you're only
  touching Node code.
- **Python 3.11+** and **[`uv`](https://github.com/astral-sh/uv)** — `uv` is the
  package manager and runner for every Python service. It creates the
  virtualenv, installs deps (`uv sync`), and runs commands inside it
  (`uv run ...`). You do not `pip install` or activate a venv by hand. Install
  `uv`, and it manages Python for you. The repo is a single **uv workspace**:
  one root `pyproject.toml` (`services/*`, `packages/*`) and one root `uv.lock`
  cover all seven services and the shared `packages/auth` (`platform_auth`)
  library — `uv sync` resolves the whole workspace even when run from a service
  subdirectory.
  No `ffmpeg` binary is needed anywhere, including for `meeting` — Opus decode
  runs in-process via PyAV, which bundles its own ffmpeg libraries.
- **Node 22.12+** — required by the locked `@discordjs/voice` dependency.
  The bot package declaration, Dockerfile and CI still select/allow Node 20;
  this is an unresolved runtime/dependency mismatch, not a compatibility guarantee.
  See the [bot prerequisites](../discord-bot/README.md#setup).
  Check with `node --version`. The bot reads its `.env` via Node's built-in `--env-file`, so no `dotenv` package needed.

Sanity check: `git --version`, `docker --version`, `uv --version`, `node --version`
should all print a version.

---

## Environment configuration

Every service and the bot ship a committed `.env.example`. The setup is always
the same — `cp .env.example .env` inside that directory — and `.env` is
gitignored everywhere, so a filled-in file never gets committed.

**The internal Python services provide local development defaults.**
Their examples are pre-filled with working local defaults (`TT_ENV=local`,
`API_KEY=dev-api-key-change-me`, a `DATABASE_URL` matching the service's own
`docker-compose.yml`). Copy and run. The `*_ENV=local` tier is what makes that
safe: non-local boot checks reject the configured dev defaults according to
each service’s policy. Gateway is different: it has no inbound bootstrap
`API_KEY`; follow its [setup and issued-key instructions](../services/gateway/README.md).

Where those values come from — and who to ask when they're org-owned — is
[`ACCESS.md`](ACCESS.md). The short answer is that you need **no UTMIST
credentials at all** for normal development.

What you must supply yourself, and only when you need that capability:

| Variable(s) | Needed for | Where it comes from |
|---|---|---|
| `DISCORD_TOKEN`, `DISCORD_CLIENT_ID` | `npm start` — the real Discord surface | [Discord Developer Portal](https://discord.com/developers/applications). **Not needed for `npm run dev:web`.** |
| `DIRECTORY_API_KEY` | the bot talking to team-tracking | Mint it — see below. Blank in the example on purpose. |
| `AWS_REGION` + AWS credentials | Optional Bedrock chat; meeting transcription | Supported AWS credential chain. Local mode can boot without provider credentials; non-local boot checks have service-specific requirements. |
| `OPENAI_API_KEY` | Default LLM chat and embeddings | A key for the configured OpenAI project; offline tests use fakes. |
| `RESEND_API_KEY` / `GMAIL_*` | `verification` sending real mail | Not needed locally — `EMAIL_BACKEND=fake` is the default and drops mail. |

**The bot is the strict one.** `discord-bot/src/config.js` hard-fails at startup
with `Missing required env vars: …` if any of these ten are unset:
`DISCORD_TOKEN`, `DISCORD_CLIENT_ID`, `DIRECTORY_BASE_URL`, `DIRECTORY_API_KEY`,
`DOC_BASE_URL`, `DOC_API_KEY`, `LLM_BASE_URL`, `LLM_API_KEY`,
`VERIFICATION_BASE_URL`, `VERIFICATION_API_KEY`. The example fills every one
except `DISCORD_TOKEN`, `DISCORD_CLIENT_ID`, and `DIRECTORY_API_KEY` — those
three are yours to provide. `MEETING_*` is genuinely optional: leave
`MEETING_BASE_URL` blank and the bot boots fine with `/record` reporting
"not configured".

**Minting `DIRECTORY_API_KEY`** (against a running team-tracking on 8000):

```bash
uv --project services/team-tracking run team-tracking-keys issue \
  --name discord-bot \
  --scopes people:read people:write people:elevate \
           identifiers:read identifiers:write \
           teams:read teams:write memberships:read memberships:write \
           role_kinds:read channels:read channels:write
```

That scope list is the one
[`scripts/provision-directory-key.sh`](../scripts/provision-directory-key.sh)
uses, and it's the authoritative set. A shorter key still lets the bot boot —
it just 403s on `/team`, `/seed`, `/my-teams`, or `/channel-access` at runtime, which is a
confusing way to discover the problem. Always pass `--project`; see the
`src`-collision gotcha in [Troubleshooting](#troubleshooting) for why a bare
`team-tracking-keys` can mint an unusable key. `llm` and `meeting` mint
differently (they have no key table) — see
[Run the platform, in order](#run-the-platform-in-order).

---

## The 10-minute mental model

Read this before touching code; see [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full picture.

**team-tracking is the foundation — everything references it.** documentation-system
validates every doc's owner id against team-tracking. The discord-bot reads and
writes the directory over HTTP, and fans out to the other services for specific
commands. Each service owns its **own** database (or none); they never share
tables, and everything talks over HTTP (no in-process consumers).

The [platform dependency map](ARCHITECTURE.md#the-services) covers directory
lookups, catalog retrieval/embedding, connector fetching and saved-meeting handoff.

The bot calls verification and meeting, and the catalog calls connectors.
`llm` serves the bot’s helper chat, meeting minutes/Q&A, and the catalog’s
embeddings. The bot requires `LLM_BASE_URL` and `LLM_API_KEY` at startup;
retrieval also needs the catalog’s embed-scoped LLM key. Meeting hands saved
text to the catalog, whose Google writer uses connectors.

The shared conventions are listed in the
[root README](../README.md#working-conventions) and explained in
[`ARCHITECTURE.md`](ARCHITECTURE.md).

Some services deviate deliberately, and it's worth knowing why before you read
their code:

- **`llm`, `meeting`, and `connectors` have no database.** Their API keys are
  seeded from a `CONSUMER_KEYS` JSON env var and parsed at boot into an
  in-memory store, not an `api_keys` table. Rotating a key means editing the
  variable and redeploying; there is no `revoke` command.
- **`verification` has a database but no key table.** It uses a
  `NullApiKeyStore`, so only the bootstrap `API_KEY` authenticates — there are no
  per-consumer keys to mint. Its database retains verification request state
  and code hashes; expiry does not delete rows.
- **`meeting` is stateful.** It holds one in-memory session per live meeting.
  It cannot be horizontally scaled without sticky routing on `session_id`, and
  a restart drops every in-flight meeting. See
  [`MEETING-RECORDING.md`](MEETING-RECORDING.md) for why that trade was made.

---

## Run the platform, in order

Most tasks need only one service. If you need the full chain (bot → directory,
or catalog with real owner validation), bring them up in this order:

1. **team-tracking first** (port 8000) — it's the source of truth. Follow its
   [Quick start](../services/team-tracking/README.md#quick-start).
2. **Seed one identity** so there's something to reference. With team-tracking
   running:

   ```bash
   curl -X POST http://localhost:8000/people \
     -H "X-API-Key: dev-api-key-change-me" \
     -H "Content-Type: application/json" \
     -d '{"display_name":"Your Name","primary_email":"you@example.com","access_level":"superuser"}'
   ```

3. **Then the consumer you're working on:**
   - **Catalog with real ownership validation** — start documentation-system and
     point its `DIRECTORY_*` config at `http://localhost:8000`.
   - **Discord bot, real Discord surface** — issue a scoped key against **main**
     (port 8000) and put it in `discord-bot/.env` as `DIRECTORY_API_KEY`, then
     `npm start`. Full steps, including the key scopes, are in the
     [discord-bot README → Complete startup](../discord-bot/README.md#complete-startup-from-cold).
   - **Discord bot, playground** — just `npm run dev:web`; it needs none of the
     above (it runs its own scratch team-tracking on 8001). It does still need
     Docker, and it will occupy **8001** and **5433** — so stop
     `documentation-system` first if it's running.
   - **`/link` / `/add-email` end-to-end** — also start `verification` (8003) and
     set `VERIFICATION_BASE_URL` / `VERIFICATION_API_KEY` in `discord-bot/.env`.
     `EMAIL_BACKEND=fake` is for injected tests/in-process inspection: it sends
     no mail and never logs codes. A manual end-to-end flow needs a configured
     test mailbox/provider; see the [verification testing guide](../services/verification/docs/CONTRIBUTING.md).
   - **`/record` end-to-end** — start `llm` (8002) **then** `meeting` (8004),
     since `meeting` calls `llm` for minutes. Set `MEETING_BASE_URL` /
     `MEETING_API_KEY` in `discord-bot/.env` and run the real Discord surface
     (`npm start`) — voice capture has no playground equivalent. Leave
     `MEETING_BASE_URL` blank and the bot still boots; `/record` just reports
     "not configured". For the default saved-text path, also configure the
     [catalog/connector persistence dependencies](../services/documentation-system/docs/DEPLOYMENT.md#meeting-record-persistence);
     a successful PDF-only recording does not verify Google Doc saving.

**Minting keys for the DB-free services.** `llm` and `meeting` have no
`api_keys` table, so their CLIs *print* a key and its `CONSUMER_KEYS` JSON entry
rather than storing it:

```bash
uv --project services/llm     run llm-keys     --name meeting     --scopes chat
uv --project services/meeting run meeting-keys --name discord-bot --scopes meetings
```

stdout is the plaintext key (shown **once** — give it to the consumer); stderr is
the JSON object you append to that service's `CONSUMER_KEYS` array before
restarting it.

---

## Make your first contribution

The end-to-end loop. The per-service CONTRIBUTING docs have the code-level
walkthroughs (add an endpoint, add a storage method, write a migration) — this
is the workflow around them.

Update developer docs in the same PR as the code. For user-visible changes,
identify the affected wiki pages and release-note items in the PR; prepare and
publish those through the [release guide](RELEASING.md). User walkthroughs live
in the separate wiki repository.

1. **Branch off `staging`.** Never commit straight to `main`; a CI guard rejects
   PRs to `main` that don't come from `staging` (see below).

   ```bash
   git switch staging && git pull
   git switch -c your-feature
   ```

2. **Copy the nearest example.** The codebase is intentionally repetitive so
   patterns are easy to imitate. When adding something, find the closest existing
   case and mirror its shape. Read the CONTRIBUTING doc for the service you're
   touching first — **every service has one**, and each ends with a pre-push
   checklist specific to that service:
   - [team-tracking CONTRIBUTING](../services/team-tracking/docs/CONTRIBUTING.md)
   - [documentation-system CONTRIBUTING](../services/documentation-system/docs/CONTRIBUTING.md)
   - [verification CONTRIBUTING](../services/verification/docs/CONTRIBUTING.md) — read the warning at the top before touching the confirm path
   - [llm CONTRIBUTING](../services/llm/docs/CONTRIBUTING.md)
   - [meeting CONTRIBUTING](../services/meeting/docs/CONTRIBUTING.md)
   - [connectors CONTRIBUTING](../services/connectors/docs/CONTRIBUTING.md)
   - [gateway CONTRIBUTING](../services/gateway/docs/CONTRIBUTING.md)
   - [discord-bot CONTRIBUTING](../discord-bot/docs/CONTRIBUTING.md)

3. **Run the fast tests locally** before you push.

   The whole repo at once, from the root — this is what you want most of the
   time:

   ```bash
   make install  # first time only
   make check    # lint + format check + fast tests, everywhere
   ```

   > **`make check` is not CI-equivalent.** It runs the fast suites. For storage,
   > adapter or migration changes, follow [database test setup](#database-test-setup).
   > `make test-full` collects all suites, but does not set the environment flags
   > that enable every Postgres/migration suite. Starting Postgres alone is insufficient.

   `make help` lists the rest (`make test`, `make format`, `make test-full`).
   It's a convenience wrapper over the per-directory commands below, not a
   replacement for them — [`ci.yml`](../.github/workflows/ci.yml) stays
   authoritative, and if the Makefile ever disagrees with CI, CI is right.

   To run one service on its own — for a DB-backed API (team-tracking,
   documentation-system, verification, gateway):

   ```bash
   cd services/<service>
   uv run pytest --ignore=tests/test_postgres_adapter.py   # fast, in-memory adapter
   ```

   `llm`, `meeting`, and `connectors` have no Postgres adapter, so it's just `uv run pytest` —
   no Docker, no network, no AWS credentials (the Bedrock/Transcribe clients are
   faked via `app.dependency_overrides`).

   For the bot: `cd discord-bot && npm test`. If you touched the shared auth
   library, its own tests live alongside it: `cd packages/auth && uv run pytest`.

   Lint before pushing too: `uv run ruff check . && uv run ruff format --check .`
   Both are enforced on every Python service and on `packages/auth`. Use
   `uv run ruff` rather than a system-wide `ruff` — it picks up the version
   pinned in `uv.lock`, and formatter output drifts between ruff versions.

   **The bot has the same pair**, spelled ESLint and Prettier:
   `cd discord-bot && npm run lint && npm run format:check`. `npm run lint:fix`
   and `npm run format` fix most findings. Both gate CI in the `node-test` job,
   and both are scoped to `*.js` — the Markdown in this repo is hand-wrapped, so
   Prettier is deliberately not pointed at it. Prettier's `printWidth` is 100,
   matching ruff's `line-length = 100` on the Python side.

4. **Open a PR into `staging`.** Every PR runs
   [`ci.yml`](../.github/workflows/ci.yml): full test suites against real
   Postgres, `ruff` lint, and Docker builds with boot smoke tests. Green CI is
   required. PR workflows also check ownership/labels and PR size; the
   [zone check](../.github/workflows/pr-zone-check.yml) warns when a PR spans
   multiple zones, and [review notifications](../.github/workflows/discord-pr-notify.yml)
   notify Discord. [Blocked/ready automation](../.github/workflows/blocked-ready-automation.yml)
   runs on issue events, not PR events; see its [current limits](ONBOARDING.md#getting-work).

   Add test, lint and image-build coverage in the same PR as a new service.
   The [CI inventory](#continuous-integration) describes the current checks.

5. **Merging deploys.** Merging to `staging` deploys to Railway **staging**;
   merging `staging → main` deploys to **production**. PRs to `main` additionally
   run [`main-source-guard`](../.github/workflows/main-source-guard.yml), which
   enforces that they originate from `staging`. Operators: see
   [`RAILWAY-DEPLOYMENT.md`](RAILWAY-DEPLOYMENT.md).

### Merge queue on `staging`

[`ci.yml`](../.github/workflows/ci.yml) supports GitHub's native merge queue
for `staging`. Enforcing the queue is a separate GitHub branch-protection
setting; merging the workflow change does not enable it automatically.

Once enabled, get the required approval and green PR checks, then use
**Merge when ready** to enter the queue. GitHub tests the proposed merge
against the latest `staging` and changes ahead of it in the queue. A failing
required check removes the PR from the queue; fix the failure and requeue it.
The eventual merge triggers the normal Railway staging deployment.

**Operator rollout:**

1. Merge the workflow support into `staging` through the existing PR process
   and confirm its CI passes before enabling queue enforcement.
2. In GitHub **Settings → Branches**, edit the protection rule for the exact
   branch `staging` and enable **Require merge queue**. Preserve the existing
   required checks and approval requirement. Start with these settings:

   | Setting | Initial value |
   | --- | --- |
   | Merge method | Merge commit |
   | Build concurrency | 1 |
   | Minimum / maximum PRs per merge | 1 / 1 |
   | Only merge non-failing pull requests | Enabled |
   | Status check timeout | 30 minutes; increase if CI runs approach it |

3. Queue a small approved PR. Confirm every required CI job runs on the
   `merge_group` commit and succeeds before the PR merges, then verify the
   staging deployment. Keep Railway's deployment branches on `staging` and
   `main`; queue refs are temporary CI inputs.

Keep production promotion through `staging → main`. The required
[`main-source-guard`](../.github/workflows/main-source-guard.yml) only handles
PR events, so enabling a queue on `main` would require adapting that check.
Any future required staging check must also report on `merge_group` events.
Keep checkout on the event's ref so CI tests the combined changes.

To roll back enforcement, disable **Require merge queue** on `staging` and
restore **Require branches to be up to date before merging**, retaining the
required checks and reviews. The `merge_group` trigger can stay in place.
See GitHub's [merge queue documentation](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue)
for queue settings and failure handling.

---

## Continuous integration

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) defines the current jobs:

- **`python-test`** — team-tracking, Postgres 16 service container, applies migrations, runs the full pytest suite
- **`python-lint`** — team-tracking ruff check + format
- **`auth-lib-test`** — the shared `packages/auth` (`platform_auth`) pytest suite + ruff check/format
- **`verification-test`** — services/verification, Postgres 16 service container, `alembic upgrade head`, then `pytest` + ruff check/format
- **`llm-test`** — services/llm pytest suite + ruff check/format
- **`meeting-test`** — services/meeting pytest suite + ruff check/format. No Postgres container and no AWS credentials: the service has no database, and its Transcribe/LLM clients are faked via `app.dependency_overrides`, so the suite runs fully offline
- **`connectors-test`** — services/connectors pytest suite + ruff check/format. No container and no Google credentials: the Google API clients are faked, so the suite runs fully offline
- **`documentation-system-test`** — Postgres 16 service container, runs `alembic upgrade head`, then `pytest` with `RUN_PG_TESTS=1` and `RUN_PG_MIGRATION_TESTS=1` + ruff check/format
- **`gateway-test`** — isolated wheel install, migration round trip, opt-in Postgres tests, pytest, ruff check/format
- **`node-test`** — the bot’s `node --test` suite, ESLint and Prettier `--check`, plus readiness-checker verification
- **`docker-build`** — builds *and boot-smoke-tests* every service image (`python -c "import src.api.app"` for the seven APIs, `node --check src/index.js` for the bot), plus the directory/gateway HTTP smoke check

The same jobs also run on pushes to `staging` and `main`, and on
`merge_group` events targeting `staging`. Queue enforcement is configured
separately in GitHub; see the [rollout guide](#merge-queue-on-staging).

Repository files define the jobs, not which checks GitHub currently requires.
Verify branch-protection/merge-queue settings against that inventory; a job’s
presence in YAML alone is not proof it gates merging.

## Database test setup

Use each service’s test guide and the environment settings in
[CI](../.github/workflows/ci.yml) as the authority. `make test-full` runs plain
pytest in each Python directory; it does not enable opt-in suites for you.

- Team-tracking and verification need their configured test Postgres databases.
- Documentation-system's `RUN_PG_TESTS=1` enables its Postgres adapter tests
  **and some destructive migration tests**. Additional migration suites also
  require `RUN_PG_MIGRATION_TESTS=1`. Use a disposable database even when the
  second flag is unset; follow the [catalog test guide](../services/documentation-system/docs/CONTRIBUTING.md).
- Gateway requires `RUN_PG_TESTS=1` and an explicit `GATEWAY_TEST_DATABASE_URL`
  for a disposable database; see [gateway testing](../services/gateway/docs/CONTRIBUTING.md).

Do not point migration regressions at retained development or production data.
Run service-specific commands with their documented database settings; a green
run with skipped suites is not the same coverage as CI.

## Troubleshooting

- **Port already in use (5433–5435 / 8000–8006).** API ports are 8000
  team-tracking, 8001 documentation-system, 8002 llm, 8003 verification, 8004
  meeting, 8005 connectors, 8006 gateway. Dev Postgres is 5433 for team-tracking,
  5434 for *both* documentation-system and verification, and 5435 for gateway.
  If a port is taken, find the process
  (`lsof -i :5434`) or stop a stray `docker compose` from another service.
- **documentation-system and verification can't run at once.** Both
  `docker-compose.yml` files bind host **5434**, so the second `docker compose
  up -d postgres` fails with a port-allocation error — or, worse, you connect to
  the *other* service's database and Alembic reports a revision it doesn't
  recognize. Remap one to an unused port (5436 if free) in a local Compose override
  and update its `DATABASE_URL`; gateway already uses 5435.
- **`npm run dev:web` fails to bind 8001, or dies on `docker compose up`.** The
  playground spawns its own team-tracking on **8001** — the same port
  `documentation-system` uses — and requires Docker for the Postgres it clones
  its scratch DB from. Stop the catalog, start Docker, retry.
- **`relation does not exist` / empty tables.** You skipped
  `uv run alembic upgrade head`. Run it from the service directory after Postgres
  is up.
- **Bot says "directory is temporarily unavailable."** Your `DIRECTORY_API_KEY`
  is invalid against main — often a key that was issued against a scratch DB that
  got wiped. Verify it:

  ```bash
  curl http://localhost:8000/api-keys/self -H "X-API-Key: <key-from-.env>"
  ```

  A `401` means reissue it against **main** (port 8000) and update `.env`.
- **`/docs` 404s on the catalog.** Its Swagger UI is at `/swagger`; `/docs` is a
  real docs-resource router on that service.
- **Minted a directory key with a `doc_` prefix (team-tracking rejects it).**
  Both `services/team-tracking` and `services/documentation-system` declare a
  top-level `src` package, each with a console script at `src.cli:main`
  (`team-tracking-keys` and `doc-keys`). In the shared workspace venv these
  collide, so a bare `team-tracking-keys …` can resolve documentation-system's
  CLI and mint a `doc_`-envelope key — which team-tracking's auth rejects
  (`parse_prefix` in [`packages/auth/platform_auth/hashing.py`](../packages/auth/platform_auth/hashing.py)
  requires the `tt_` envelope). Always pin the project:
  `uv --project services/team-tracking run team-tracking-keys …` (as
  `scripts/provision-directory-key.sh` does), and verify the token's prefix is
  `tt_`, not `doc_`, before using it as `DIRECTORY_API_KEY`.
- **`/record` says "not configured".** `MEETING_BASE_URL` is unset on the bot.
  That's the intended graceful degradation, not a crash — set it (plus
  `MEETING_API_KEY`) to enable the command.
- **`meeting` or `llm` refuses to start.** Outside local mode, each service
  validates its configured credentials/dependencies at boot. LLM needs a
  non-default bootstrap key and OpenAI key for embeddings, plus an AWS region
  when selecting Bedrock. Meeting needs its non-default bootstrap key, AWS
  region and LLM URL/key. Check the service configuration guide for the exact
  policy: meeting's current known-default comparison does not reject an empty
  bootstrap value, so operators must still provide a strong key.
- **`CONSUMER_KEYS` rejected at boot.** `llm` and `meeting` both require it to be
  a **JSON array** (`[{...}]`), not a bare object or a comma-separated string.
  Anything else fails fast at startup by design.
- **`uv: command not found` / `docker: command not found`.** Revisit
  [Prerequisites](#prerequisites-new-to-this-stack).

---

## Where to go next

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the cross-service data flow and why the
  conventions exist.
- [`MEETING-RECORDING.md`](MEETING-RECORDING.md) — how `/record` splits across the
  bot's voice surface and the `meeting` service, and why it breaks two of the
  platform's conventions on purpose.
- [`ACCESS.md`](ACCESS.md) — which credentials you need (mostly none), who to
  ask for the rest, and what to revoke when someone leaves.
- [`RAILWAY-DEPLOYMENT.md`](RAILWAY-DEPLOYMENT.md) — deploy/operate runbook.
- Per-service references — every service now has the full four-doc set
  (`API.md`, `ARCHITECTURE.md`, `CONTRIBUTING.md`, `DEPLOYMENT.md`) under its
  own `docs/`:
  [team-tracking](../services/team-tracking/docs/API.md),
  [documentation-system](../services/documentation-system/docs/API.md),
  [verification](../services/verification/docs/API.md),
  [llm](../services/llm/docs/API.md),
  [meeting](../services/meeting/docs/API.md),
  [connectors](../services/connectors/docs/API.md).
  `meeting`'s WebSocket route isn't representable in OpenAPI, so its
  [API.md](../services/meeting/docs/API.md) *is* the published wire contract.
