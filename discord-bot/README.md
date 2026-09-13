# UTMIST Discord Bot

The Discord frontend for the UTMIST platform — a thin Node.js layer over the
[team-tracking](../services/team-tracking) directory API. It gives UTMIST members
slash commands to link their Discord account, look up teams and rosters, and
(if they're an admin) manage the directory from Discord itself.

The bot holds **no database and no business logic** — every action is an HTTP
call to the directory (the API-only source of truth). It's designed to run in
many Discord servers from a single deployment, with a proper **auth layer**
gating each command.

**Status:** Deployed to Railway (staging + production). Staging runs against a
Neon staging branch in a private test guild; production runs globally against
the prod branch. See [`docs/RAILWAY-DEPLOYMENT.md`](../docs/RAILWAY-DEPLOYMENT.md).

## Complete startup (from cold)

Two modes, one shared foundation. Pick which mode you need:

- **Discord surface** — the bot connects to Discord and users invoke slash
  commands in your test server. Needs a Discord app + token.
- **Web playground** — a browser-based Discord-lookalike that hits the same
  handlers without going through Discord. No Discord token required. Isolated
  ephemeral scratch DB. This is the recommended dev loop.

### First-time setup (once per machine)

```bash
# In services/team-tracking/
cp .env.example .env
docker compose up -d postgres            # named volume — survives reboots
uv sync --extra dev                      # installs the Python env
uv run alembic upgrade head              # migrations against the main DB

# In discord-bot/
cp .env.example .env
npm install
```

Then seed at least one identity into main so `/link` has something to match:

```bash
# Start main team-tracking briefly to seed via HTTP
cd services/team-tracking && uv run uvicorn src.api.app:app --port 8000
# In another shell:
curl -X POST http://localhost:8000/people \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"display_name":"Your Name","primary_email":"you@example.com","access_level":"superuser"}'
# Ctrl+C the uvicorn when done.
```

Also issue an API key for the Discord bot (only if you'll use Discord mode):

```bash
cd services/team-tracking
uv --project . run team-tracking-keys issue --name discord-bot \
  --scopes people:read people:write people:elevate identifiers:read identifiers:write \
           teams:read teams:write memberships:read memberships:write role_kinds:read
# Copy the tt_... key into discord-bot/.env as DIRECTORY_API_KEY.
# IMPORTANT: this key must be issued against MAIN (port 8000), not scratch.
# Verify the token starts with tt_ — a doc_-prefixed key means the shared venv
# resolved documentation-system's CLI instead, and team-tracking will reject it.
# scripts/provision-directory-key.sh is the source of truth for the bot's scopes
# (people:elevate is what lets /seed promote people to admin/superuser).
```

### Daily startup

Depends on which mode you want.

#### Playground mode (browser, recommended for feature dev)

```bash
cd discord-bot
npm run dev:web
```

That single command boots the whole stack: postgres (if not up) → clone main
→ scratch DB → spawn scratch team-tracking on 8001 → issue scratch key →
seed Dev Superuser/Admin/Member → start Fastify on `http://127.0.0.1:3001`.

Open the URL, paste `100000000000000000` into "Acting as", pick a command, run.
Ctrl+C tears the whole thing down and drops the scratch DB.

**You do NOT need main team-tracking (port 8000) running for playground mode.**
The orchestrator manages its own team-tracking on port 8001 against the scratch DB.

#### Discord mode (real bot in a Discord server)

Discord mode reads `.env` and hits main team-tracking, so main needs to be up:

```bash
# Terminal 1
cd services/team-tracking
uv run uvicorn src.api.app:app --reload --port 8000

# Terminal 2
cd discord-bot
npm start                                # or: npm run dev for both surfaces
```

Wait for `Logged in as <bot name>` in terminal 2, then use the slash commands
in whichever Discord server you invited the bot to.

**Requires that `DIRECTORY_API_KEY` in `.env` is a valid key against main
team-tracking**, not a scratch key. If Discord returns "directory is
temporarily unavailable," check the key with:

```bash
curl http://localhost:8000/api-keys/self -H "X-API-Key: <the-key-from-.env>"
```

A 401 means the key was invalidated (probably was originally issued against
a scratch DB that got wiped). Issue a fresh one against main and update `.env`,
then restart the bot process.

#### Both at once (rarely useful)

```bash
cd discord-bot
npm run dev                              # ENABLE_DISCORD=true ENABLE_WEB=true
```

Discord connects to main, the web surface still uses its own scratch DB
managed by the orchestrator. Fine for parallel testing; noisy in one terminal.

### Stopping everything

- **Ctrl+C** in each running terminal. That's it.
- The scratch DB is dropped automatically by `npm run dev:web` on shutdown.
- The main DB in the postgres volume is durable — it survives Docker restarts
  and machine reboots. It's only wiped by `docker compose down -v`.

## Multi-server model

The bot is meant to run across **many Discord servers** from a single deployment.
There is no per-server allowlist: anyone can add it to a server, and access is
gated purely by the auth layer — an unlinked user can't run gated commands, a
linked user can. Identity is a Discord user's **global** account (snowflake), so
a linked person is recognized in every server the bot shares with them.

### Release channels (beta vs stable)

Commands are split into two channels by an `export const beta` flag:

- **Stable** (`beta = false` or omitted) — registered **globally**, available in
  every server the bot is in (production).
- **Beta** (`beta = true`) — registered **only** to the dedicated testing guild
  (`DISCORD_GUILD_ID`), so a new command can be trialed in the test server without
  ever reaching production. Promote it by flipping `beta` to `false`.

`npm run register` sends stable commands globally and beta commands to the test
guild exclusively (re-running also clears stale/promoted beta commands from the
guild). If any command is beta and `DISCORD_GUILD_ID` is unset, registration warns
and skips them.

> **Local vs. Railway.** `npm run register` uses `--env-file=.env`, so it targets
> your **local test bot** only. To register the deployed bots, use the
> Railway-targeted wrappers — `npm run register:all` (staging then production),
> or `npm run register:staging` / `npm run register:production` for one
> environment. There's also a guarded `./scripts/register.sh <staging|production|all>`
> that confirms before touching production. See
> [RAILWAY-DEPLOYMENT.md §5](../docs/RAILWAY-DEPLOYMENT.md).

## Commands

- `/link email:<your UTMIST email>` (public) — starts linking your Discord account
  to your directory record. Your email must already be in the directory (execs seed
  members). It emails a one-time code to that address; run `/verify-code` with the
  code to finish linking. Backed by the verification service via `src/linkService.js`.
- `/verify-code code:<6-digit>` (public) — confirms the code emailed by `/link` and
  completes the link.
- `/whoami` (linked) — shows which directory record you're linked to. Requires you
  to be linked; the auth layer handles the "not linked" case.
- `/seed email:<> name:<> [level:member|admin|superuser]` (admin) — add a member to
  the directory. Gated to admins; you can only grant a level at or below your own
  (`superuser` > `admin` > `member`). The bot's directory key needs `people:write`.
- `/team create slug:<> label:<> [description:<>]` (admin) — create a team.
- `/team list [active_only:<bool>]` (linked) — list teams (default active only).
- `/team rename slug:<> new_label:<>` (admin) — rename a team.
- `/team add user:<@mention> team:<slug> [role:<...>] [team_admin:<bool>]` (admin) — add a member. The mentioned user must have already run `/link`.
- `/team remove user:<@mention> team:<slug>` (admin) — soft-end a membership as of today.
- `/team roster team:<slug> [as_of:<YYYY-MM-DD>]` (linked) — show a team's current roster.
- `/my-teams` (linked) — list your active memberships.
- `/doc <add|list|show|remove>` (linked; `remove` is admin) — catalog and look up UTMIST documents and links.

- `/record start` (**linked**) — joins your current voice channel and starts
  recording the meeting. `/record status` (**public**) — shows elapsed recording
  time. `/record stop` (**public**) — ends the recording and, within roughly
  30–60s, posts a branded `meeting-minutes.pdf` (LLM-generated title, summary,
  decisions, action items, full transcript) back into the text channel,
  @-mentioning whoever started the recording. Recording also stops
  **automatically** once everyone leaves the voice channel (after a short grace
  period), with a 4h hard backstop. The meeting service persists nothing: it
  streams audio straight to AWS and never writes audio or transcript to disk.
  The posted PDF does contain the full transcript, and that lives in Discord
  like any other attachment.

  When the first human enters an empty voice channel, the bot @-mentions them
  via direct message and prompts them to run `/record start`.

  > `start` is gated at `'linked'` because it consumes a voice connection and a
  > live session; `status`/`stop` are deliberately `'public'` so a directory
  > outage can't strand a running recording. See
  > [`docs/MEETING-RECORDING.md` → Authorization](../docs/MEETING-RECORDING.md).

Every command is on the **stable** channel (`beta = false`), so they all register
globally in every server the bot is in. There are currently no beta commands.

> `/record` requires the `meeting` service to be deployed in the target
> environment, with `MEETING_BASE_URL` + `MEETING_API_KEY` set on the bot. It was
> promoted from beta in the staging → main release; provision `meeting` in an
> environment **before** registering commands there, or `/record` will be visible
> and fail.

### Meeting recording (`/record`) infra

As of v2, recording is split across two services: the bot only joins voice and
streams raw Opus audio over a WebSocket to the separate `meeting` service,
which owns Opus decoding, transcription (AWS Transcribe), and minutes
generation, and returns the resulting PDF for the bot to post. See
[`docs/MEETING-RECORDING.md`](../docs/MEETING-RECORDING.md) for the full
architecture.

The bot itself needs only:

- `MEETING_BASE_URL` — the `meeting` service's base URL (HTTP; the bot derives
  the WebSocket URL from it).
- `MEETING_API_KEY` — the API key the bot authenticates to the `meeting`
  service with.
- The `GuildVoiceStates` gateway intent and Connect permission in the voice
  channel (already required to join voice at all).

The bot does **not** need ffmpeg, AWS credentials, or `MAX_RECORDING_MS` /
`RECORDING_SILENCE_MS` — those are requirements of the `meeting` service
(`services/meeting`), not the bot. See `services/meeting/README.md` and its
`.env.example` for that service's infra requirements.

## Auth layer

The bot's gate is its authentication/authorization layer — the Discord-side
parallel to team-tracking's scoped-key auth.

- **Principal** (`src/auth/principal.js`) — the authenticated subject, `{ person }`
  in v1. Extension point for roles/memberships later.
- **Policy** (`src/auth/policy.js`) — each command declares `auth: 'public' | 'linked'`
  (default `'linked'`, fail-secure). Extends to role/team-admin policies without
  touching handlers.
- **Router** (`src/router.js`) — the single Policy Enforcement Point:
  authenticate → authorize → dispatch. Fails **closed** if the directory is
  unreachable.

## Setup

1. **Node 20+** required.
2. `cp .env.example .env` and fill in:
   - Discord app credentials (`DISCORD_TOKEN`, `DISCORD_CLIENT_ID`).
   - `DISCORD_GUILD_ID` — dedicated testing guild ID. **Beta** commands register
     exclusively here; stable commands are always global. Required only if you
     have beta commands (see "Release channels" above).
   - `DIRECTORY_BASE_URL` (e.g. `http://localhost:8000`).
   - `DIRECTORY_API_KEY` — issue one from team-tracking:
     ```bash
     cd ../services/team-tracking
     uv --project . run team-tracking-keys issue --name discord-bot \
       --scopes people:read people:write people:elevate identifiers:read identifiers:write \
                teams:read teams:write memberships:read memberships:write role_kinds:read
     # The token must start with tt_ (a doc_-prefixed key means the shared venv
     # resolved documentation-system's CLI). scripts/provision-directory-key.sh
     # is the source of truth for these scopes — people:elevate is required so
     # /seed can promote people to admin/superuser (without it, /seed to a
     # privileged level returns 403).
     ```
     **Important:** the CLI writes to whichever DB `.env`'s `DATABASE_URL` points
     at, which is your main `team_tracking` DB by default. Do NOT issue this key
     against the scratch playground DB (`team_tracking_playground`) — the scratch
     DB gets dropped every time the playground shuts down or resets, taking any
     keys issued against it with it. If your bot suddenly starts replying
     "directory temporarily unavailable" after you ran the playground, check
     with `curl http://localhost:8000/api-keys/self -H "X-API-Key: $KEY"` — a
     401 means the key was on scratch and got wiped; issue a fresh one against
     main and update `.env`.
3. `npm install`
4. `npm run register` — registers the slash commands (run once, or whenever
   command definitions change). **Stable** commands go global (can take ~1h to
   propagate); **beta** commands go only to the testing guild. See "Release
   channels" above.
5. `npm start` — then invite the bot to any server via its OAuth2 URL.

## Web playground (local dev)

For iterating on commands without going through Discord, the bot ships a
Discord-shaped web playground. It runs against an **ephemeral scratch copy** of
your local team-tracking DB — commands you run in the playground never touch
your working directory data.

### One command to start

```bash
npm run dev:web
```

That boots an orchestrator (`scripts/dev-web.js`) which:

1. Ensures the team-tracking docker-compose Postgres is up (starts it if not).
2. Clones your main dev DB (`team_tracking`) into a scratch DB
   (`team_tracking_playground`) via `pg_dump | psql` (with `set -e -o pipefail`
   so a failed dump surfaces immediately instead of silently leaving the scratch
   DB empty).
3. Logs the row count: `scratch has N people from main` — if that's 0 but you
   expected people, your main DB is empty (see "Seeding your main DB" below).
4. Spawns a second team-tracking `uvicorn` on port 8001 against the scratch DB.
5. Issues a `dev:spoof`-scoped API key against that scratch instance.
6. **Seeds three default personas into scratch** so the playground is usable
   even if your main DB is empty:
   | Acting as | Role | Purpose |
   |---|---|---|
   | `100000000000000000` | Dev Superuser | Test admin-of-admin flows |
   | `100000000000000001` | Dev Admin | Test admin-gated commands |
   | `100000000000000002` | Dev Member | Baseline user |
7. Starts the web server on `http://127.0.0.1:3001` pointed at the scratch DB.
8. On Ctrl-C, tears everything down and drops the scratch DB.

### Using the playground

Open `http://127.0.0.1:3001`, paste one of the three Dev IDs above into
"Acting as" (or pick from the datalist), click a command in the sidebar, fill
the form, and run. Replies stream into the transcript above.

Click **Reset DB** in the top strip to re-clone from your main DB whenever you
want a clean slate — no restart required. The default personas are re-seeded
after every reset.

### Picker only shows *linked* people

The "Acting as" datalist only surfaces people whose Discord identifier is
already linked. A person you've seeded via `POST /people` but never `/link`ed
lives in the DB but doesn't appear in the dropdown. To bring them in:

1. Type any placeholder Discord snowflake into "Acting as" (any numeric string
   works — e.g., your real snowflake with Developer Mode on).
2. Run `/link email:<their-email>`.
3. Refresh (Cmd+R). The person now appears in the datalist tied to that
   Discord ID.

### Seeding your main DB

The playground clones from `team_tracking`. If that's empty, the picker will
only show the three Dev personas. To seed a durable identity (yours), use the
env-bootstrap API key that ships in `services/team-tracking/.env`:

```bash
curl -X POST http://localhost:8000/people \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "display_name": "Your Name",
    "primary_email": "you@example.com",
    "access_level": "superuser"
  }'
```

That requires the **main** team-tracking uvicorn to be running on port 8000:

```bash
cd ../services/team-tracking && uv run uvicorn src.api.app:app --port 8000
```

Once seeded, click **Reset DB** in the playground and your new identity shows
in the scratch clone. Note: only the `people` row is durable; a `/link` inside
the playground writes to scratch and gets wiped on reset — so you'll need to
`/link` again after each reset, or seed the Discord identifier directly:

```bash
# Get the person's id first
PERSON_ID=$(curl -s http://localhost:8000/people/by-email/you@example.com \
  -H "X-API-Key: dev-api-key-change-me" | python3 -c "import sys, json; print(json.load(sys.stdin)['id'])")

# Then attach a discord identifier
curl -X POST http://localhost:8000/people/$PERSON_ID/identifiers \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"provider":"discord","external_id":"YOUR_DISCORD_SNOWFLAKE","handle":"you"}'
```

Now every scratch clone includes you AND your Discord link — no `/link` step
needed.

### Prerequisites

- **`docker compose`** — the Postgres container.
- **`uv`** — spawns the scratch team-tracking Python process.
- **`services/team-tracking/`** — expected at `../services/team-tracking` relative to `discord-bot/`.

If any are missing the orchestrator errors out at startup with a clear message.

### DB persistence

The `team_tracking` main DB lives in the `team_tracking_pg` Docker named
volume. It survives `docker compose down`, `docker compose restart`, machine
reboots, and Docker Desktop restarts. It's only wiped by `docker compose down -v`
or `docker volume rm`.

The `team_tracking_playground` scratch DB is dropped on Ctrl-C by the
orchestrator; on next `npm run dev:web` it's re-cloned from main.

### When to use `dev:web:plain` instead

If you've already provisioned a team-tracking instance manually (specific
version, remote host, whatever) and just want to run the bot's web server
against it, use `npm run dev:web:plain` — same web UI, no orchestration, no
scratch DB, reads `DIRECTORY_BASE_URL` and `DIRECTORY_API_KEY` from your
local `.env`.

## Architecture

| File | Responsibility |
|---|---|
| `src/config.js` | Load + validate env. |
| `src/context.js` | Wire application services once. |
| `src/directoryClient.js` | The only module that knows the team-tracking HTTP shape. |
| `src/linkService.js` | Orchestrates `/link` + `/verify-code` — email one-time-code verification via the verification service. |
| `src/auth/principal.js` | Authentication: Discord id → Principal. |
| `src/auth/policy.js` | Authorization: policy + principal → allow/deny. |
| `src/router.js` | Policy Enforcement Point: authN → authZ → dispatch. |
| `src/messages.js` | Pure reply-string rendering. |
| `src/commands/*.js` | Thin discord.js interaction handlers + registry. |
| `src/index.js` | Client setup + interaction routing. |
| `src/registerCommands.js` | One-shot slash-command registration (stable → global; beta → testing guild only). |
| `src/defineCommand.js` | Neutral, surface-agnostic command factory. |
| `src/adapters/discord.js` | The ONLY module that imports from discord.js — turns interactions into intents. |
| `scripts/dev-web.js` | Orchestrator: ephemeral scratch DB + scratch team-tracking + web server. |
| `scripts/lib/snapshotDb.js` | Pipes `pg_dump` from main into `psql` on scratch, under `set -e -o pipefail`. |
| `scripts/lib/spawnTeamTracking.js` | Spawns a scratch uvicorn subprocess; polls `/openapi.json` for readiness. |
| `scripts/lib/issueDevSpoofKey.js` | Runs `team-tracking-keys issue --scopes ... dev:spoof` against scratch. |
| `scripts/lib/seedDefaultPersonas.js` | Idempotently seeds Dev Superuser/Admin/Member into scratch. |
| `src/web/server.js` | Fastify web playground (see "Web playground" above). |
| `src/web/public/mentions.js` | Client-side `<@id>` → user pill rendering. |
| `src/startupGuard.js` | Refuses web-mode boot if the directory key lacks `dev:spoof`. |

## Testing

```bash
npm test    # node --test — unit tests for config, client, service, auth, router, render
```

The auth layer (principal, policy, router) and the service layer are fully unit
tested against a mocked directory client. discord.js handlers are kept thin.

## Deferred

Discord role sync, `/unlink`, and any bot-side persistence. (Email verification is
now implemented — `/link` + `/verify-code` via the verification service.)

Team archive/unarchive, `/team info`, editing role/team-admin on existing memberships, team-admin delegation authority, dynamic role_kinds fetch for slash choices, LLM adapter.
