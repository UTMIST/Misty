# Railway Deployment (staging + production)

Deploys the platform's six backend services plus the discord-bot to Railway,
backed by Neon Postgres (a branch per environment) for the three services that
own a database. Repo-side config lives in each service's `Dockerfile` +
`railway.json`; the steps below are the account-side setup you run in the
Railway + Neon dashboards / CLIs.

| Railway service | Root dir | Database | Pre-deploy | Notes |
|---|---|---|---|---|
| `team-tracking` | `/` | Neon (own project) | `alembic upgrade head` | Deploy first — everything references it. |
| `documentation-system` | `/` | Neon (own project) | `alembic upgrade head` | Consumes team-tracking (hard dependency) and connectors (soft — recommended to deploy connectors first, not required). |
| `verification` | `/` | Neon (own project) | `alembic upgrade head` | Email one-time codes. |
| `llm` | `/` | **none** | — | Stateless Bedrock chat + OpenAI embedding API; keys from `CONSUMER_KEYS`. |
| `meeting` | `/` | **none** | — | **Stateful in-memory**; keys from `CONSUMER_KEYS`. See the single-replica warning in step 2, and the WebSocket keepalive note in the Notes section — its `startCommand` is the one that isn't the plain template. |
| `connectors` | `/` | **none** | — | Stateless outbound adapter (Google Drive/Docs); keys from `CONSUMER_KEYS`. Recommended to deploy before `documentation-system` (not required) — see its `CONNECTORS_API_KEY` note in step 3. |
| `discord-bot` | `discord-bot` | none | `node src/registerCommands.js` | Node; the only consumer-facing surface. Pre-deploy registers the slash commands with Discord (step 5). |

All seven are **private** — no public domains. They reach each other over
Railway's internal network as `<service>.railway.internal:<PORT>`.

## Prerequisites
- A Railway account + the `railway` CLI (`railway login`).
- A Neon account.
- `uv` locally (for the key-provisioning script and the key-minting CLIs).
- An AWS account with Bedrock **and** Amazon Transcribe enabled in your
  `AWS_REGION`, for `llm` chat and `meeting` respectively.
- An OpenAI API key for `llm` embeddings, configured before the target environment auto-deploys.

## Branching + auto-deploy model
Each Railway environment is wired to a git branch. Merging a PR flips a deploy.

```
feature branch  ──PR──▶  staging  ──PR──▶  main
                          │                  │
                          ▼                  ▼
                    Railway staging   Railway production
                    (auto-deploy)     (auto-deploy)
```

- **`staging`** is the integration branch. Feature PRs target it (it's the repo
  default). Every merge auto-deploys to the `staging` Railway environment
  (staging Neon branch + staging Discord app).
- **`main`** is the release branch. **PRs to `main` may only come from
  `staging`** — enforced by the `main-source-guard` workflow
  ([`.github/workflows/main-source-guard.yml`](../.github/workflows/main-source-guard.yml)),
  which fails any PR to `main` whose head is not `staging`. Every merge to
  `main` auto-deploys to the `production` Railway environment.
- Both branches are protected; **all ten** CI jobs are required status checks,
  and `main` additionally requires `main-source-guard`. See
  [`DEPLOYMENT-HISTORY.md`](DEPLOYMENT-HISTORY.md) for what each job covers.
- CI supports a merge queue on `staging`; enable it only after the workflow
  support has landed. See the [rollout guide](DEVELOPMENT.md#merge-queue-on-staging).

## 1. Neon: databases + branches
Create **three Neon projects** — `team-tracking`, `documentation-system`, and
`verification` (each service owns its own DB). In each project you get a `main`
branch (= production); create a second branch named `staging` (copy-on-write
from main). Copy the six connection strings (3 projects × 2 branches). Use the
`postgresql+psycopg://…` form (append `?sslmode=require` if not present).

`llm`, `meeting`, and `connectors` have no database — nothing to provision for them here.

## 2. Railway: project, environments, services
1. Create a Railway project; it starts with a `production` environment — add a
   `staging` environment too.
2. Add seven services from this repo:
   - The six Python services (`team-tracking`, `documentation-system`,
     `verification`, `llm`, `meeting`, `connectors`). Their Dockerfiles build from the **repo
     root** as context (so `packages/` is reachable) and install their workspace
     member with `uv sync --frozen --no-dev --package <name>` into a venv at
     `/app/.venv`. So each one's Railway **root directory is `/`**, with
     `railway.json` → `dockerfilePath` pointing at
     `services/<name>/Dockerfile`.
   - `discord-bot` — Node, unaffected by the workspace change; its root
     directory stays `discord-bot`.

   Railway picks up each service's `railway.json` (Dockerfile build, start
   command, health check, and the pre-deploy step: `alembic upgrade head` for
   the three DB-backed services, `node src/registerCommands.js` for the bot).
   `llm`, `meeting`, and `connectors` have no `preDeployCommand` because they
   have no schema.
3. Keep **every** service private (no public domain). The bot needs no domain.
4. **Pin `meeting` to a single replica.** It keeps each live meeting's session
   entirely in process memory, so a given `session_id`'s WebSocket,
   `/transcript` polls, and `/stop` call must all land on the same process.
   Scaling it horizontally without sticky routing on `session_id` misroutes
   `/stop` to a process that never saw the meeting, and the recording is lost.
   Every other service is stateless and scales freely.

## 3. Environment variables
Set these per environment (staging vs production) per service.

**The three DB-backed services:**

| Var | team-tracking | documentation-system | verification |
|---|---|---|---|
| `DATABASE_URL` | tt Neon branch | docs Neon branch | verification Neon branch |
| `API_KEY` | a strong random secret | a strong random secret | a strong random secret |
| env tier | `TT_ENV` = `staging`/`production` | — | `VF_ENV` = `staging`/`production` |
| `PORT` | `8000` | `8000` | `8000` |
| `DIRECTORY_BASE_URL` | — | `http://${{team-tracking.RAILWAY_PRIVATE_DOMAIN}}:${{team-tracking.PORT}}` | — |
| `DIRECTORY_API_KEY` | — | *(set by the provisioning script — step 4)* | — |
| `CONNECTORS_BASE_URL` | — | `http://${{connectors.RAILWAY_PRIVATE_DOMAIN}}:${{connectors.PORT}}` | — |
| `CONNECTORS_API_KEY` | — | a `connectors` consumer key with the `fetch` scope — see step 4b | — |
| `CODE_HMAC_SECRET` | — | — | a strong random secret |
| `EMAIL_BACKEND` | — | — | `resend` (or `gmail`) — **not** `fake` |
| `EMAIL_FROM` | — | — | `UTMIST <noreply@utmist.ca>` |
| `RESEND_API_KEY` | — | — | from Resend |

> **documentation-system boots fine without `CONNECTORS_API_KEY`.** Unlike
> `API_KEY`/`DIRECTORY_API_KEY`, `verify_production_secrets()` only logs a
> startup warning if it's still on the dev default outside `local` — it does
> not fail the deploy. Without it, Google-source fetches (`gdocs`, `gsheets`,
> `gslides`, `gdrive`) fail and are recorded as per-doc ingest warnings; the
> catalog itself still works. Deploying connectors before documentation-system
> is still recommended so Google fetches work from the start, but it is not
> required — see the deploy order below.

**The three DB-free services:**

| Var | llm | meeting | connectors |
|---|---|---|---|
| env tier | `LLM_ENV` = `staging`/`production` | `MEETING_ENV` = `staging`/`production` | `CONNECTORS_ENV` = `staging`/`production` |
| `API_KEY` | a strong random secret | a strong random secret | a strong random secret |
| `CONSUMER_KEYS` | JSON array — see step 4b | JSON array — see step 4b | JSON array — see step 4b |
| `PORT` | `8000` | `8000` | `8000` |
| `AWS_REGION` | e.g. `us-east-1` (Bedrock) | e.g. `us-east-1` (Transcribe) | — |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | yes | yes | — |
| `LLM_PROVIDER` / `LLM_MODEL` | `bedrock-converse` / `claude-sonnet-4-6` (chat) | — | — |
| `EMBED_MODEL` | `openai-embed-3-small` → `text-embedding-3-small`, **1536 dimensions** | — | — |
| `OPENAI_API_KEY` | required outside `local` | — | — |
| `EMBED_MAX_REQUEST_CHARS` | optional; default `400000` total characters, a coarse input guard | — | — |
| `LLM_BASE_URL` | — | `http://${{llm.RAILWAY_PRIVATE_DOMAIN}}:${{llm.PORT}}` | — |
| `LLM_API_KEY` | — | an `llm` consumer key with the `chat` scope | — |
| `MAX_MEETING_MS` | — | optional; defaults to the 4h backstop | — |
| `DISCONNECT_GRACE_S` | — | optional; defaults to 60s. How long a disconnected session is held so `POST /stop` can still finalize it | — |
| `GOOGLE_CREDENTIALS_JSON` | — | — | base64 Google service-account key; empty is a valid running state (Google fetches 503, rest of the service works) |
| `MAX_FILE_BYTES` | — | — | optional; defaults to 25 MiB. Pre-download limit for uploaded PDF, `.docx`, and `text/*` files |

> **Before merging or deploying llm**, configure a real `OPENAI_API_KEY` in the target
> Railway environment, even for chat-only consumers. The boot check verifies presence,
> not credential validity; follow the
> [llm rollout prerequisites](../services/llm/docs/DEPLOYMENT.md#embeddings-post-embed).

> Bedrock chat usage bills as **Amazon Bedrock** (credits apply) — do *not* point
> `llm` at Claude Platform on AWS. Embeddings use **OpenAI billing**, not AWS
> credits.

**discord-bot:**

| Var | Value |
|---|---|
| `DISCORD_TOKEN` | staging app / prod app token |
| `DISCORD_CLIENT_ID` | per app |
| `DISCORD_GUILD_ID` | test guild (staging) / blank (prod) |
| `ENABLE_DISCORD` / `ENABLE_WEB` | `true` / `false` |
| `HELPER_USER_MAX_REQUESTS` / `HELPER_USER_WINDOW_SECONDS` | optional helper allowance overrides; see [bot configuration and reset semantics](../discord-bot/README.md#helper-bot-request-limits) |
| `PORT` | Railway-injected port for the Discord readiness listener; defaults to `3002` locally |
| `DIRECTORY_BASE_URL` / `DIRECTORY_API_KEY` | team-tracking; key set by the provisioning script |
| `DOC_BASE_URL` / `DOC_API_KEY` | documentation-system |
| `VERIFICATION_BASE_URL` / `VERIFICATION_API_KEY` | verification |
| `INFRASTRUCTURE_DISCORD_USERNAME` | *optional* — Discord username or ID shown by `/bug` for infrastructure support |
| `MEETING_BASE_URL` / `MEETING_API_KEY` | meeting; a `meetings`-scoped consumer key |
| `MEETING_WS_URL` | *optional* — derived from `MEETING_BASE_URL` if unset |

Each `*_BASE_URL` follows the same private-network template shape, e.g.
`http://${{meeting.RAILWAY_PRIVATE_DOMAIN}}:${{meeting.PORT}}`.

> **`MEETING_BASE_URL` is the `/record` kill switch.** Leave it unset and the
> bot boots normally; `/record` just answers "not configured". That's the
> intended way to register commands in an environment where `meeting` isn't
> provisioned yet.

> **Why `PORT=8000` explicitly?** `${{team-tracking.PORT}}` in the consumers'
> `DIRECTORY_BASE_URL` only resolves when `PORT` is an explicit Railway variable.
> Railway's dynamically-injected `PORT` isn't visible to cross-service template
> refs. Without this, `DIRECTORY_BASE_URL` resolves to `http://…railway.internal:`
> (empty port) and every bot → team-tracking call fails with "directory
> temporarily unavailable." One of the three real bugs shipped through the
> branching flow — see [`DEPLOYMENT-HISTORY.md`](DEPLOYMENT-HISTORY.md).

Generate the `API_KEY` values locally so nothing sensitive appears in a shell
transcript:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```
Twelve of them — one per backend service per environment (plus
`CODE_HMAC_SECRET` for verification). Paste into Railway's variable dashboard,
never into a file.

**Deploy order matters for the hard dependencies below; connectors is a
recommended-but-not-required exception.** In each environment:

1. **team-tracking first** — its `preDeployCommand` runs `alembic upgrade head`
   against the environment's Neon branch, which the provisioning script (next)
   depends on.
2. **connectors** — recommended before documentation-system so Google-source
   fetches work immediately, but not required: documentation-system boots
   fine without connectors reachable or `CONNECTORS_API_KEY` set (see step 4b
   and the warning above) — it just can't fetch Google content until then.
3. **documentation-system + verification** — they migrate the same way as
   team-tracking.
4. **`llm` before `meeting`** — `meeting` needs `LLM_BASE_URL` pointing at a
   running `llm`, and refuses to boot without it outside `local`.
5. **discord-bot last** — it consumes all of the above.

## 4a. Provision the directory keys
Once team-tracking is up + migrated in an environment, mint + wire the scoped
consumer keys:

```bash
railway login          # once
TT_DATABASE_URL="<team-tracking Neon branch DATABASE_URL for this env>" \
  ./scripts/provision-directory-key.sh staging      # then: production
```

This issues scoped `team-tracking-keys` for discord-bot + documentation-system
and sets each service's `DIRECTORY_API_KEY`. The discord-bot key's scopes
include `people:elevate` (alongside its `people:*`/`teams:*`/`memberships:*`
scopes) so its `/seed` can promote people to `admin`/`superuser` — plain
`people:write` cannot set a non-`member` `access_level`. The
documentation-system key stays read-only (`people:read teams:read`). Re-running issues a fresh key and
repoints the consumer; the previous key stays active until you revoke it
manually (`team-tracking-keys revoke <id>`).

> **Gotcha — `src` package collision when minting keys.** Both
> `services/team-tracking` and `services/documentation-system` declare a top-level
> `src` package, each with a console script pointing at `src.cli:main`
> (`team-tracking-keys` and `doc-keys` respectively — see their
> `[project.scripts]`). In the shared workspace venv these collide, so a bare
> `team-tracking-keys …` can resolve **documentation-system's** CLI and mint a
> `doc_`-envelope key. team-tracking's auth rejects that: `parse_prefix`
> ([`packages/auth/platform_auth/hashing.py`](../packages/auth/platform_auth/hashing.py))
> returns `None` for any token that doesn't start with the `tt_` envelope, so the
> key is dead on arrival. This actually bit us during key provisioning. Always
> invoke the CLI with the project pinned —
> `uv --project services/team-tracking run team-tracking-keys …`, which
> `scripts/provision-directory-key.sh` already does — and **verify the minted
> token's prefix is `tt_`, not `doc_`, before wiring it as `DIRECTORY_API_KEY`.**

## 4b. Provision the `llm` + `meeting` + `connectors` consumer keys (manual)

`llm`, `meeting`, and `connectors` have **no `api_keys` table** — their keys live in a
`CONSUMER_KEYS` JSON array env var, parsed into an in-memory store at boot. So
`provision-directory-key.sh` does not cover them; this step is manual, and you
repeat it per environment.

Three keys are needed:

```bash
# 1. A key for meeting -> llm (scope: chat)
uv --project services/llm run llm-keys --name meeting --scopes chat

# 2. A key for discord-bot -> meeting (scope: meetings)
uv --project services/meeting run meeting-keys --name discord-bot --scopes meetings

# 3. A key for documentation-system -> connectors (scope: fetch)
uv --project services/connectors run connectors-keys --name documentation-system --scopes fetch
```

Each CLI prints the **plaintext key to stdout** (shown exactly once — it is
argon2-hashed, never recoverable) and the `CONSUMER_KEYS` **JSON entry to
stderr**. Wire them like this:

| Printed to stderr (the JSON entry) | Printed to stdout (the plaintext key) |
|---|---|
| append to `llm`'s `CONSUMER_KEYS` array | set as `meeting`'s `LLM_API_KEY` |
| append to `meeting`'s `CONSUMER_KEYS` array | set as `discord-bot`'s `MEETING_API_KEY` |
| append to `connectors`'s `CONSUMER_KEYS` array | set as `documentation-system`'s `CONNECTORS_API_KEY` |

Then redeploy the service whose `CONSUMER_KEYS` you changed — the store is
built at boot, so the new key isn't live until it restarts.

For an **approved internal embedding caller**, mint an additional key from the repo root:

```bash
uv --project services/llm run llm-keys --name embedding-caller --scopes embed
```

Install it using [llm's consumer-key steps](../services/llm/docs/DEPLOYMENT.md#consumer-keys).
`chat` does not grant `embed`; grant both only when needed. This provisions access,
not a documentation-system indexing pipeline.

**Revocation is a redeploy.** There is no `revoke` command; drop the entry from
`CONSUMER_KEYS` and redeploy. `CONSUMER_KEYS` must stay a JSON **array** —
both services reject any other shape at boot, deliberately, so a malformed
variable fails the deploy rather than silently disabling auth.

Before deploying the Discord bot, enable **Message Content Intent** for both the
staging and production Discord applications in Developer Portal → **Bot** →
**Privileged Gateway Intents**. The bot requests it at startup so members can
use helper threads with full recent-message context, including messages that did
not mention Misty. Without the portal toggle, Discord rejects the gateway
connection or omits that content.

## 5. Register Discord slash commands
The bot has to tell Discord which slash commands it supports. **This happens
automatically on every deploy**: `discord-bot/railway.json` sets
`preDeployCommand` to `node src/registerCommands.js`, so Railway runs the
registration in the freshly built image — with that environment's variables —
between build and start, exactly the way the DB-backed services run
`alembic upgrade head`. Merging to `staging` registers the staging bot; merging
to `main` registers the production bot. It's idempotent, so re-running on a
deploy that changed no commands is harmless.

If registration fails, the deploy fails and the previous deployment keeps
running. `preDeployTimeoutSeconds` caps the step at 120 s (it normally takes a
few seconds) so a hung Discord API call fails the deploy instead of holding it
in progress forever. The success signal is the step's exit code, not a log
line: on staging a clean run prints **both** `Registered N stable commands
globally` and `Registered N beta commands to testing guild …`, and the deploy
proceeds to start the new process. Seeing either line is also proof the
service is reading `railway.json` at all (see the `railwayConfigFile` note in
[`DEPLOYMENT-HISTORY.md`](DEPLOYMENT-HISTORY.md)).

**A failure can leave Discord half-updated.** The script does two sequential
bulk overwrites: global commands first, then the testing guild's. A failure
*before* the first call — a rejected command definition, a bad token — changes
nothing. A failure *between* the two (global succeeded, guild rejected) leaves
the global set already overwritten while the old bot process keeps running,
so members may see new commands the running code can't serve, and the guild
set is stale. The `Registered N stable commands globally` line prints before
the guild step, so it does not by itself mean the step succeeded. To recover,
pick one:

- **Finish the deployment.** Fix the cause and push again; the next successful
  pre-deploy overwrites both sets. This is the normal path.
- **Restore the running revision's command set.** Find the commit the live
  deployment was built from (`railway status`, or the deployment's page in the
  dashboard), check it out, and run the manual wrapper for that environment
  (below). Both overwrites then match the code that is actually serving.

The script partitions commands into **stable** (registered globally — visible
in every server the bot is in) and **beta** (registered only to
`DISCORD_GUILD_ID` if set). Because we set `DISCORD_GUILD_ID` on staging and
leave it blank in production, staging gets `beta` commands in the test guild
only, and production correctly skips them.

Global registrations can take up to ~1 hour to propagate through Discord's
cache. Guild-scoped (staging) commands appear instantly. Because registration
runs *before* the new bot process starts, a brand-new command can be visible for
a few seconds while the old process is still serving — it answers "application
did not respond" in that window, then works once the new deployment is live.

### Registering by hand

You only need this when you want to re-register **without** a deploy — to
restore the running revision's commands after a failed pre-deploy (above), or
to check what a branch would register before merging it. The manual wrappers
run the same script via `railway run`, which injects that environment's
secrets:

```bash
cd discord-bot
npm run register:staging      # or register:production, or register:all
./scripts/register.sh all     # same, but prompts before touching production
```

> **Not** `npm run register` — that hardcodes `--env-file=.env` and only hits
> your **local test bot**.

### Moving the testing guild (changing `DISCORD_GUILD_ID`)

`registerCommands.js` only ever writes to the guild that is *currently*
configured. Changing `DISCORD_GUILD_ID` from guild A to guild B and
re-registering (by deploy or by hand) populates B and leaves A's command set
exactly as it was — stale beta commands in A stay visible to that server and
fail when invoked. Clear A explicitly, with its old id, as a separate step:

```bash
cd discord-bot
OLD_GUILD_ID=<guild A id> railway run --service discord-bot --environment staging -- \
  node --input-type=module -e '
const { REST, Routes } = await import("discord.js");
await new REST({ version: "10" }).setToken(process.env.DISCORD_TOKEN)
  .put(Routes.applicationGuildCommands(process.env.DISCORD_CLIENT_ID, process.env.OLD_GUILD_ID), { body: [] });
console.log(`cleared guild commands for ${process.env.OLD_GUILD_ID}`);'
```

`railway run` injects the environment's `DISCORD_TOKEN` / `DISCORD_CLIENT_ID`
and passes your shell's `OLD_GUILD_ID` through. An empty bulk overwrite
removes every command the bot had registered in that guild and nothing else.
Then set the new `DISCORD_GUILD_ID` on the service and register into B — a
redeploy does it, or `npm run register:staging`.

> **Provision `meeting` before deploying the bot to an environment.** `/record`
> is stable and registers globally, so it becomes visible to every member the
> moment the bot deploys. If `meeting` isn't deployed there (or
> `MEETING_BASE_URL` is unset on the bot) the command answers "not configured"
> — visible but useless. Either provision `meeting` first, or accept the
> degraded state knowingly.

## 6. Seed the first admin
Nobody is a directory admin on a fresh production DB. Seed yourself as a
`superuser` so you can grant others. Two ways:

**Option A — Neon SQL editor** (fastest for a one-off):
```sql
INSERT INTO people (display_name, primary_email, access_level, created_by, updated_by)
VALUES ('Your Name', 'you@example.com', 'superuser', 'manual-seed', 'manual-seed')
ON CONFLICT (primary_email) DO UPDATE
  SET access_level = 'superuser',
      updated_by   = 'manual-seed',
      updated_at   = now()
RETURNING id, display_name, primary_email, access_level;
```
Idempotent (upserts on `primary_email`). Run in the **team-tracking → main
branch** for production, or `staging` branch for staging.

**Option B — the seed CLI** (more auditable / scriptable):
```bash
TT_DB="<team-tracking Neon branch URL for this env>"
DATABASE_URL="$TT_DB" \
  uv --project services/team-tracking run team-tracking-seed seed-person \
    --name "Your Name" --email you@example.com --level superuser
```

After that, use `/link` in Discord (staging test guild or the real server) to
attach your Discord account to the seeded person record. New admins get added
by an existing admin running `/seed` from Discord.

## 7. Verify
- **APIs:** for each of the six services —
  `railway run --service <name> --environment <env> -- bash -c 'curl -s localhost:$PORT/health'`
  → `{"status":"ok"}`. `/health` is unauthenticated on every service, so no key
  is needed. For team-tracking and documentation-system, also request
  `/health/ready` to verify their own database connections; Railway uses that
  path for those two services. For the three DB-backed services, pre-deploy
  logs should also show `alembic upgrade head` ran.
- **Bot:** Railway checks `/health/ready` on its injected `PORT`. It returns
  `503` before Discord is ready or after a disconnect, and 200 once connected.
  The pre-deploy step exits 0 and its log shows `Registered N stable commands
  globally` (and, on staging, `Registered N beta commands to testing guild …`
  — both lines, see step 5); the deploy log shows `Bot ready as …`. Staging bot appears in the test guild and prod bot
  registers globally.
- **End-to-end (directory):** run a bot command (e.g. `/whoami`) in the staging guild → reaches staging team-tracking → staging Neon branch. If it returns "directory is temporarily unavailable," the two most common causes are (1) `DIRECTORY_BASE_URL` template not resolving (see the `PORT=8000` note above), or (2) `DIRECTORY_API_KEY` missing on the consumer (re-run the provisioning script).
- **End-to-end (`/record`):** join a staging voice channel, `/record start
  name:Smoke-Test`, talk for ~30s, `/record stop`. Within roughly 30–60s a
  named PDF should be posted to the text channel. This exercises the whole chain —
  bot → `meeting` (WS) → Transcribe → `llm` → Bedrock → PDF. Failure modes to
  check in order: `MEETING_API_KEY` wrong (WS closes with code 1008),
  `LLM_API_KEY`/`LLM_BASE_URL` wrong on `meeting` (PDF arrives with degraded,
  unsummarized minutes rather than failing), or AWS credentials missing
  (transcript comes back empty).

## Notes
- All six Python Dockerfiles build with the repo root as context and
  `uv sync --frozen --no-dev --package <name>` to install just that workspace
  member (plus the shared `platform_auth` leaf from `packages/auth`) — that's
  why their Railway root directory is `/` while `discord-bot`'s stays
  `discord-bot`.
- **`meeting`'s image needs no `ffmpeg` binary.** Opus decode runs in-process
  via PyAV, which bundles its own ffmpeg libraries, and nothing shells out.
- **Nothing from a meeting outlives it.** No database, no object store, and no
  disk writes at all — audio streams straight to AWS Transcribe and is dropped.
  If the minutes matter, the PDF the bot posted to Discord is the only copy.
- APIs bind **`--host 0.0.0.0`**. Railway's IPv6 private network routes to the
  container's port regardless of the bind family, and `0.0.0.0` is what
  Railway's healthcheck reaches (an IPv6-only bind like `::` fails healthcheck
  on Debian's default kernel config). If service-to-service calls
  (bot → team-tracking) fail, check the `DIRECTORY_BASE_URL` reference next.
- Staging uses the **separate staging Discord application** + private test guild,
  so staging commands never touch the real UTMIST server.
- The `sh -c` wrapper on the API `startCommand` is deliberate — see the second
  bug in [`DEPLOYMENT-HISTORY.md`](DEPLOYMENT-HISTORY.md).
- **`meeting` carries extra WebSocket keepalive flags** on its `startCommand`:
  `--ws-ping-interval 60 --ws-ping-timeout 60`. uvicorn defaults to a 20 s ping
  with a 20 s pong deadline, which closes the socket on a client that is briefly
  slow to answer — mid-recording, and cleanly enough that neither side logs an
  error. 60 s is still well inside any proxy idle timeout. It's the one service
  whose `startCommand` isn't the plain `uvicorn … --port ${PORT}` template, so
  don't "normalize" it, and keep `railway.json` and the Dockerfile `CMD` in step
  with each other. Railway reads `railway.json`, so that file is what actually
  governs the deploy.
