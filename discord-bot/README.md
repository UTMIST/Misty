# UTMIST Discord Bot

The Discord frontend for the UTMIST platform — a thin Node.js layer over the
[team-tracking](../services/team-tracking) directory API. It gives UTMIST members
slash commands to link their Discord account, look up teams and rosters, and
(if they're an admin) manage the directory from Discord itself.

The bot holds **no database**. Directory data remains in the backend API;
helper-bot request allowances are transient, per-user state in this process.
It runs in many Discord servers from a single deployment, with an auth layer
gating commands and helper mentions.

**Status:** Deployed to Railway (staging + production). Staging runs against a
Neon staging branch in a private test guild; production runs globally against
the prod branch. See [`docs/RAILWAY-DEPLOYMENT.md`](../docs/RAILWAY-DEPLOYMENT.md).

For real Discord and recording tests against a PR, the optional persistent
`dev` preview slot runs one selected PR using the same dev bot identity.
Use `npm run preview -- <pr>` or GitHub Actions → **Preview Discord PR** after
the [one-time setup](../docs/pr-previews.md). Automatic Railway PR copies keep
Discord disabled, skip command registration, and use a liveness health check.

Preview configuration (bot credentials belong only in `dev`; the selector
override is optional in your shell):

| Variable | Purpose |
|---|---|
| `MISTY_PREVIEW_ENVIRONMENT_ID` | Literal UUID of the persistent `dev` environment; copies cannot inherit ownership. |
| `DISCORD_TOKEN_DEV` | Dedicated dev application's bot token. |
| `DISCORD_CLIENT_ID_DEV` | Dedicated dev application's application ID. |
| `DISCORD_GUILD_ID_DEV` | Test guild for the dev application, including guild-scoped testing commands. |
| `MISTY_PREVIEW_PROJECT_ID` | Optional selector-only project override; export in the shell. Defaults to Misty's project and is overridden by `--project`. The selector does not load `.env`. |

See the [preview runbook](../docs/pr-previews.md) for selection, preflight checks,
shutdown, timeout options, and GitHub Actions setup.

## Complete startup (from cold)

Two modes, one shared foundation. Pick which mode you need:

- **Discord surface** — the bot connects to Discord and users invoke slash
  commands in your test server. Needs a Discord app + token.
- **Web playground** — a Misty-branded browser workspace that hits the same
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
           teams:read teams:write memberships:read memberships:write role_kinds:read \
           channels:read channels:write
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

Before starting Discord mode, enable **Message Content Intent** under Discord
Developer Portal → your application → **Bot** → **Privileged Gateway Intents**.
The bot requests this intent so it can include recent user questions in helper
requests, including messages that did not mention Misty. Old bot answers are
omitted because they have no verifiable source provenance; document follow-ups
retrieve their evidence again.

Wait for `Logged in as <bot name>` in terminal 2, then use the slash commands
in whichever Discord server you invited the bot to.

Discord mode always starts a health listener; it does not require `PORT` or
production configuration. It serves unauthenticated `GET /health/ready` on
`0.0.0.0:$PORT` (default `3002` locally). It returns `503` until the Discord
gateway is ready (and again after a disconnect), or `{"status":"ok"}` with
HTTP 200 while connected. Railway supplies `PORT` and uses this route for
deployment health checks. Locally, request
`http://localhost:3002/health/ready` to inspect the gateway state. This
listener is separate from the playground's default `WEB_PORT=3001`.
`GET /health/live` returns 200 while the process serves requests, including an
idle PR copy (`{"status":"ok","discord":"disabled"}`). Railway's ephemeral
PR override checks this route; persistent environments keep `/health/ready`.

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

### Registration scope (global vs testing guild)

The internal `beta` flag selects registration scope independently of feature
status. A feature labeled **in progress** can still register globally; `/qna`
does so with `beta = false`:

- **Stable** (`beta = false` or omitted) — registered **globally**, available in
  every server the bot is in (production).
- **Testing-guild only** (`beta = true`) — registered **only** to the dedicated testing guild
  (`DISCORD_GUILD_ID`), so a new command can be trialed in the test server without
  ever reaching production. Promote it by flipping `beta` to `false`.

`npm run register` sends global commands to every server and testing commands to the test
guild exclusively (re-running also clears stale/promoted testing commands from the
guild). If any command is testing-guild only and `DISCORD_GUILD_ID` is unset, registration warns
and skips them.

> **Local vs. Railway.** `npm run register` uses `--env-file=.env`, so it targets
> your **local test bot** only. The deployed bots register themselves: Railway
> runs `node src/registerCommands.js` as the bot's `preDeployCommand` (see
> `railway.json`) on every deploy to staging or production. To re-register
> *without* deploying, use the Railway-targeted wrappers — `npm run register:all`
> (staging then production), or `npm run register:staging` /
> `npm run register:production` for one environment. There's also a guarded
> `./scripts/register.sh <staging|production|all>` that confirms before touching
> production. See [RAILWAY-DEPLOYMENT.md §5](../docs/RAILWAY-DEPLOYMENT.md).

## Commands

- `/qna question:<text>` (**public policy**, **in progress: available globally**) —
  privately answers from the current recording's last 15 minutes of finalized
  transcript. Run it in the exact text channel where recording started; threads
  are separate channels. Replies identify the meeting using the exact minutes PDF
  filename: `<sanitized name>_YYYY-MM-DD_HHMM.pdf`, or
  `meeting_YYYY-MM-DD_HHMM.pdf` when no usable name is supplied. Both use the same
  filename function and recording start time in America/Toronto, followed by
  references and the time range used. An accepted question keeps its original meeting name
  even if a new recording starts before its answer arrives.
  Dense windows use less recent history and explicitly disclose
  the shorter range. No account linking, directory lookup, or Q&A history is used.
  The window and size/error contract are documented in the
  [meeting API](../services/meeting/docs/API.md#post-meetingssession_idqna).
  It works in every server with an active recording; `DISCORD_GUILD_ID` is not
  required and no testing-guild restriction is applied.
  The bot defers replies privately and gives the backend request a 75-second
  deadline, including response-body reading. If the dedicated Q&A capacity is
  full, it replies privately that Q&A is busy and asks you to retry shortly.
  The playground only explains where
  to invoke the command; it does not simulate an active recording.

- `/link email:<your UTMIST email>` (public) — starts linking your Discord account
  to your directory record. Your email must already be in the directory (execs seed
  members). It emails a one-time code to that address; run `/verify-code` with the
  code to finish linking. Backed by the verification service via `src/services/linkService.js`.
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
- `/team google-status team:<slug>` (admin) — show the team's managed Google Group sync status. Settings failures offer automatic retry and a manual recovery fallback.
- `/team google-sync team:<slug>` (admin) — retry the same group's automatic settings verification and membership reconciliation. See [recovery and rollout](../services/team-tracking/docs/DEPLOYMENT.md#external-member-setup-and-recovery).
  - `/team create` probes sync after directory create, separating team creation from Google status. The probe waits at most two seconds before reporting provisioning in progress; use `/team google-status` afterward. Aborting the probe does not cancel backend provisioning.
  - The backend verifies settings even for empty active groups before recording `synced`. Success needs no manual toggle. An actual external addition still needs live acceptance; an older stored `synced` result requires a new sync after backend rollout.
  - Recovery links appear only for confirmed managed groups with settings/external-member failures, using the stored email/domain. UTMIST's own Google administrator handles authorization or policy recovery. Generic directory outages are not reported as disabled Google integration.
  - Deploy the automatic-settings backend and authorize both OAuth scopes before rolling out these replies. Keep the existing status/retry commands; manual instructions are recovery guidance, not the normal onboarding flow.
- `/my-teams` (linked) — list your active memberships.
- `/channel-access show` (admin) — show which teams' documents Misty may use in this
  channel. In a thread, the setting applies to the parent channel. Drive access stays
  personal; this only scopes Misty's answers. Backed by team-tracking's
  `/channels/{guild}/{channel}/teams` API. The playground uses fixed guild/channel
  snowflakes (`111…` / `222…`) so the command works without Discord.
- `/channel-access set team:<> [team2:<> … team5:<>]` (admin) — replace the channel's
  team list (up to five). Team options use slug autocomplete.
- `/channel-access append team:<> [team2:<> … team5:<>]` (admin) — add teams to the
  existing list without removing ones already configured (union, then the same PUT).
- `/channel-access clear` (admin) — clear the channel's team list (no document access).
- `/doc <add|list|remove>` (linked; `remove` is admin) — catalog and look up UTMIST documents and links.
- `/doc search query:<text>` (linked) — search the documents you can access and get the top matches
  as titles and links. Replies are always private, because results include your own Drive shares.
  Needs the bot's doc key to carry the literal `act-as-user` scope (an `admin` key does not imply it).

- `@Misty <question>` (linked) — opens a helper thread and answers using the
  thread's recent user questions. In a thread Misty created, replying directly to one of
  Misty's messages continues the conversation when the reply ping is enabled.
  Ping-off replies, replies to other users, replies in regular channels, and replies
  in threads Misty did not create do not trigger the helper. A typed leading
  `@Misty` remains an independent trigger, including inside a Discord reply.

- `/record start [name:<meeting name>] [save:false]` (**linked**) — joins your current voice channel and starts
  recording the meeting (one recording at a time **per guild** — sessions are
  keyed by `guildId`). `/record status` (**public**) — shows elapsed recording
  time and the voice channel Misty is in; when idle, it explains which voice
  channel Misty will join when recording starts. `/record stop` (**public**) —
  ends the recording when you are in Misty's recorded voice channel and, within roughly
  30–60s, posts a branded PDF named from the optional meeting name and
  America/Toronto start timestamp (or `meeting_<timestamp>.pdf` when unnamed).
  The name becomes the PDF title; otherwise it uses the LLM-generated title,
  summary, decisions, action items, and full transcript back into the text channel,
  @-mentioning whoever started the recording. Recording also stops
  **automatically** once everyone leaves the voice channel (after a short grace
  period), with a 4h hard backstop. Starting while a recording is already
  active in the guild, or stopping from outside the recorded channel, is
  refused with the active channel and auto-stop guidance — except stopping
  from outside is still allowed once that channel is empty of humans, the
  escape hatch for a runaway recording auto-stop failed to catch. The meeting
  service streams audio straight to AWS and never writes audio to disk.
  By default, documentation-system also saves a Google Doc containing minutes
  and the full transcript. Misty posts its link in the exact originating text
  channel or thread, mentioning the original requester even on automatic stop.
  You need your own Drive access; posting the link does not change permissions.
  Missing team/group coverage produces an explicitly admin-only record.
  `save:false` keeps only the existing PDF attachment. Delayed saves and failed
  link deliveries are reported separately and recovered automatically. See
  [delivery and recovery](docs/meeting-delivery.md).

  When the first human enters an empty voice channel, the bot @-mentions them
  via direct message and prompts them to run `/record start`.

  > `start` is gated at `'linked'` because it consumes a voice connection and a
  > live session; `status`/`stop` are deliberately `'public'` so a directory
  > outage can't strand a running recording. See
  > [`docs/MEETING-RECORDING.md` → Authorization](../docs/MEETING-RECORDING.md).

- `/bug` (public) — where to report a bug (GitHub issue link and optional
  infrastructure contact).

Every command is on the **stable** channel (`beta = false`), so they all register
globally in every server the bot is in. There are currently no beta commands.
This includes `/qna`; its **in progress** label is informational. The handler uses the invoking
server's active meeting and exact recording text channel, with no testing-guild
restriction or user-level permission check.

> `/record` requires the `meeting` service to be deployed in the target
> environment, with `MEETING_BASE_URL` + `MEETING_API_KEY` set on the bot. It was
> promoted from beta in the staging → main release; provision `meeting` in an
> environment **before** deploying the bot there (the deploy registers the
> commands), or `/record` will be visible and fail.

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
- `DOC_API_KEY` also needs `meetings:read` and `meetings:write` for saved-record
  lookup and delivery receipts. Discord needs View Channel, Read Message History
  and Send Messages (Send Messages in Threads for a thread) in the original
  destination. No Google credentials are added to the bot.

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
     have testing-guild-only commands (see "Registration scope" above).
   - `DIRECTORY_BASE_URL` (e.g. `http://localhost:8000`).
   - `DIRECTORY_API_KEY` — issue one from team-tracking:
     ```bash
     cd ../services/team-tracking
     uv --project . run team-tracking-keys issue --name discord-bot \
       --scopes people:read people:write people:elevate identifiers:read identifiers:write \
                teams:read teams:write memberships:read memberships:write role_kinds:read \
                channels:read channels:write
     # The token must start with tt_ (a doc_-prefixed key means the shared venv
     # resolved documentation-system's CLI). scripts/provision-directory-key.sh
     # is the source of truth for these scopes — people:elevate is required so
     # /seed can promote people to admin/superuser (without it, /seed to a
     # privileged level returns 403). channels:read/write power /channel-access.
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
   propagate); **testing-guild-only** commands go only to that guild. See
   "Registration scope" above.
5. `npm start` — then invite the bot to any server via its OAuth2 URL.

## Helper document tools

| Configuration | Default | Meaning |
| --- | --- | --- |
| `HELPER_TOOLS_ENABLED` | `false` | Literal `true` enables authorized document retrieval and command tools for Discord mentions and DMs |

Enable only after retrieval and the destination's channel configuration are
ready. `DOC_API_KEY` needs `docs:read`, literal `act-as-user` for DMs, and literal
`act-as-channel` for shared channels. Existing command write scopes still apply.
The bot checks source access during generation and immediately before sending,
appends source links, and withholds an answer if those checks fail. Previous user
questions remain available for follow-ups; earlier bot answers are not replayed,
even after the flag is disabled. The web playground remains text-only.

Search tools supply the returned source passages, rather than cutting each to a
400-character preview. Each passage is capped at 48 KiB of UTF-8 text and marked
when truncated. JSON escaping and metadata can reduce that text allowance so a
single hit fits the 60 KiB serialized result budget. Hits are admitted in rank
order; one that exceeds the remaining budget is skipped, allowing later smaller
hits to fit. Only retained hits are cited and revalidated. DM document bodies
use the same raw-text and serialized-result limits.

Channel answers can use these authorized passages, while opening a full document
by id remains available only in DMs. The channel grant authorizes returned text
under the [retrieval contract](../services/documentation-system/docs/API.md#authorized-retrieval-post-search),
not just links. Publication revalidation covers the text supplied to the model.

See the [helper tool contract](docs/CONTRIBUTING.md#the-authorized-helper-tool-loop-71)
for replay, cost limits, revalidation tradeoffs, and offline coverage.

### Commands through mentions and DMs

With helper tools enabled, ask `@Misty show my teams`, `@Misty show the robotics
roster`, or `@Misty remove @Alex and @Sam from infrastructure`. Misty prepares
all command calls in the same model response for one review, within the shared
limit of four tool calls per request (including document lookups). The same
tools work in DMs where the underlying command does not need a server or channel.

| Capability | Available actions |
| --- | --- |
| Directory and help | List teams, view a roster, my teams, who am I, help, bug-report link |
| Documents | List, add, remove; search and read use the retrieval rules below |
| Team administration | Create, rename, add/remove members, seed a person |
| Google Groups | View sync status, request a sync |
| Channel access | Show, set, append, clear the current channel's team access |
| Helper limits | Show or change request limits |
| Meetings | Recording status, start, stop, and questions about the current recording |

Every tool requires the requesting Discord user's active linked directory
account. Each command uses its existing permission policy, checked again before
execution; admin tools still require an admin. User, server, channel, and meeting
context come from Discord, never from model arguments. Backend calls continue to
use the bot's scoped service credentials; this does not mint personal API keys.
Account linking and email verification remain explicit slash-command flows.

Command results are rendered directly and do not enter model history. In server
conversations, team lists, team rosters, help and bug-report links reply publicly
without a button. Public help is a shared guide that does not reveal the
requester's access level; use `/help` privately for personalized command help.
Other command results stay private by default, including
personal lookups, document lists, meeting Q&A and administrative operations.
Document search retains the channel-authorized retrieval rules above; full
document reads remain DM-only.

For private results, Misty posts a **Review privately** button for writes or
**View privately** for reads. Only the requester can use it; the
preview and results appear as ephemeral replies in that same conversation.
No DM is opened for a server request. Discord requires the button interaction
before it can show an ephemeral reply to a mention. Requests made in DMs stay
there. Private reads and meeting Q&A start only after the requester opens the
private response (or a private acknowledgement is delivered for a DM request). A later
result-delivery failure never reruns the action.
Every write, including recording start/stop, first shows its arguments and
destination in a private preview with **Confirm** and **Cancel**. Member changes
show the directory names, Discord mentions and team label/slug. If an optional
name lookup fails, the preview still identifies the member by mention and the
team by slug. A multi-action
request lists every proposed action; one confirmation approves the whole list.
Commands run sequentially and report each result while access and expiry checks
still permit delivery; they are not an atomic transaction. If a later action
loses permission, earlier results are still delivered when their own checks
pass. A result whose access or expiry check fails is withheld even if its action
completed. Inspect the current state before asking again.
A mixed public/private batch uses the private interaction, then
delivers each result with its own visibility. An unexpected execution error stops
the remaining actions without retrying earlier ones. If any tool call cannot be
prepared, the entire proposal is withheld and Misty explains that nothing was
changed. Only the requester can confirm. The confirmation window lasts two
minutes from the first successful private preview; generation time does not
shorten it, and reopening
the preview does not extend it. Access changes or exhausted evidence checks can
still block an action. Unopened actions expire after two minutes. Actions are
single-use and disappear on restart. A batch occupies one pending slot. There
can be at most eight pending actions per user and 500 per process. Capacity is
reserved before delivery or execution;
a full queue asks you to cancel an action or wait. A failed action is never retried
automatically; inspect the current state before asking again if its result could
not be delivered.

Channel-access commands in a thread target its parent. Meeting questions use
the exact channel or thread where recording was started. Recording starts in
the requester's voice channel at confirmation time and posts minutes to the
original request channel. Its preview states whether the transcript and minutes
will be saved as a Google Doc (default yes, matching `/record start`; ask for
`save=false` to opt out). A stop confirmation applies only to the recording
that was active when requested. Recording status replies publicly only when
requested in that meeting's exact origin text channel or thread. If that recording
ends and no new recording has started before the status runs, Misty reports that
no recording is in progress; it rechecks the session before posting. A replacement
recording in another channel cannot publish its status here. Successful
start/stop actions also announce there after private confirmation, while keeping
the full result in the requester's ephemeral response. These public messages
omit voice-channel locations and requester-specific details. Requests
elsewhere and unsuccessful recording actions keep their results private. An
auto-created helper thread is not substituted for the meeting's origin, and a
thread never publishes into its parent. Source-channel access is checked again
before execution and every public or private message part.

The prepared command batch ends the helper turn. Document research must finish
in earlier tool rounds; a model response mixing document tools and commands is
rejected before either runs, with a request to ask separately.
Existing slash commands retain
their current reply visibility. Enabling this feature uses the same flag and
rollout acceptance below; no new environment
variables, backend endpoints, or slash registrations are required.

### Channel scope and activation

A helper reply in a thread uses its **parent channel's** configuration. A
mention in an ordinary channel uses that channel's configuration, including
when Misty creates a thread for the reply. Configuration commands in
[#239](https://github.com/UTMIST/Misty/issues/239) must use the same rule; the
[directory API](../services/team-tracking/docs/API.md#channel-team-access)
looks up the exact guild/channel pair and does not resolve thread parents.
Pass Discord snowflakes as their original decimal strings throughout. Do not
convert them to JavaScript numbers, which can lose precision.

An unconfigured or cleared channel returns an empty search result, without
embedding the query or searching a wider scope. Missing assertion scopes and
unavailable channel settings are errors, not empty-channel results; see the
[retrieval contract](../services/documentation-system/docs/API.md#authorized-retrieval-post-search).
An empty result gives the model no document evidence and adds no source links.

Activation is coordinated through
[#278](https://github.com/UTMIST/Misty/issues/278): William leads helper/retrieval
integration and acceptance; Guneev owns channel configuration and its change
cases. Keep the flag off until #239 and the real retrieval integration are ready.
The maintainer with access to the target environment provisions the bot's doc
key with the scopes above, preserving existing command scopes, and installs it
as `DOC_API_KEY`; coordinate that operator with the #278 lead. Enable the flag
in staging for the controlled DM/channel, follow-up, and revocation acceptance.
Production activation follows recorded acceptance, including the automatic
sharing flow required by #278. Merging this executor does not flip the flag or
provision credentials. Disable the flag to stop new tool-enabled answers.

## Helper-bot request limits

| Configuration | Default | Meaning |
| --- | --- | --- |
| `HELPER_USER_MAX_REQUESTS` | `10` | Admitted helper requests per Discord user per window |
| `HELPER_USER_WINDOW_SECONDS` | `3600` | Fixed window duration, starting at that user's first admitted request |

Both settings are optional positive integers. Omit them to use defaults; blank,
zero, negative, fractional, non-numeric, or unsafe values fail startup. There is
no unlimited/disabled sentinel. Set them on the **discord-bot** deployment, not
the LLM service. Administrators can change the live values without a restart
with `/helper-limits set`; `/helper-limits show` displays the current values.
Changing the values preserves each user's existing usage.

An exhausted user gets a slowdown reply with the seconds until their next
allowance. Other users remain independent, including in the same thread.
See [helper limits](docs/helper-limits.md) for accounting, failure behavior,
reset semantics, deployment limitations, and offline verification.

## Web playground (local dev)

For iterating on commands without going through Discord, the bot ships a
Misty-branded web playground. It runs against an **ephemeral scratch copy** of
your local team-tracking DB — commands you run in the playground never touch
your working directory data.

### One command to start

```bash
npm run dev:web
```

The launcher loads `discord-bot/.env` itself. Set `LLM_BASE_URL` and
`LLM_API_KEY` there (or start the local LLM service on port 8002) before using
**DM Misty**; the scratch directory settings are created automatically. The
launcher carries the same LLM settings through every **Reset DB**
reconfiguration, so a reset does not silently disable helper answers.

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

The **DM Misty** composer exercises the same linked-only helper flow as a
Discord DM. Its history is kept separately per selected Discord ID, so switching
the Acting as picker cannot leak one person's conversation into another's.

Open `http://127.0.0.1:3001`, paste one of the three Dev IDs above into
"Acting as" (or pick from the datalist). Choose a quick-start task or browse
commands under Conversations, Your account, Teams & people, Documents,
Meetings, Administration, and Help & support. Search matches command names, descriptions,
and categories. New uncategorized commands remain available under Other commands.

Forms share labeled controls, help text, and Required/Optional indicators.
Leaving an optional select at **Use default** preserves the command's server
default; selecting **No** explicitly sends false. Permission badges describe
the command policy; the server still authorizes every request. Run a command
to see its reply in **Activity**. Pending requests disable repeat submission.

The layout adapts to narrow screens and supports keyboard navigation. On phones,
**Commands** opens the category menu; selecting a command closes it.
**Clear activity** only clears the visible replies; it does not reset Misty's
thread context. UTMIST's existing astronaut logo and cobalt accent are reused
locally, with no external font, icon, or image requests.

Click **Reset DB** beside the identity picker to re-clone from your main DB
whenever you want a clean slate — no restart required. The default personas are re-seeded
after every reset.

Choose **Reply to Misty** under **Conversations** to simulate a Discord reply. Toggle
**Ping @Misty** to test both reply-ping settings: Misty answers only when it is on,
while ping-off messages remain part of the simulated thread history. The playground
sends that history with every triggered follow-up. **Reset thread** clears only this
helper transcript; **Reset DB** also clears it.

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
| `src/runtimeMode.js` | Shared Railway environment ownership and dev credential selection for startup, configuration, and command registration. |
| `src/context.js` | Wire application services once. |
| `src/clients/*.js` | Backend HTTP clients and shared request handling. |
| `src/services/*.js` | Application orchestration over injected clients. |
| `src/meeting/*.js` | Recording client, session orchestration, and voice recorder. |
| `src/clients/directoryClient.js` | The only module that knows the team-tracking HTTP shape. |
| `src/services/linkService.js` | Orchestrates `/link` + `/verify-code` — email one-time-code verification via the verification service. |
| `src/auth/principal.js` | Authentication: Discord id → Principal. |
| `src/auth/policy.js` | Authorization: policy + principal → allow/deny. |
| `src/router.js` | Policy Enforcement Point: authN → authZ → dispatch. |
| `src/messages/*.js` | Pure reply rendering grouped by feature; shared fallback strings in `common.js`. |
| `src/commands/*.js` | Surface-neutral command handlers + registry. |
| `src/index.js` | Client setup + interaction routing. |
| `src/helperFlow.js` | Shared helper-bot authorization and answer handling used by Discord and the web playground. |
| `src/toolExecutor.js` | Bounded document tool loop, requester revalidation, and terminal command-action handoff. |
| `src/commandTools.js` | Explicit command-tool allowlist, strict arguments, current-user authorization, and private action previews. |
| `src/helperActions.js` | Bounded, expiring, single-use actions bound to their requester. |
| `src/helperChecks.js` | Share duplicate authorization work within one validation step; never cache across operations or messages. |
| `src/registerCommands.js` | Slash-command registration (global by default; testing-guild only when `beta = true`). Runs as Railway's persistent-environment `preDeployCommand`; ephemeral PR copies use the [preview override](../docs/pr-previews.md#automatic-railway-pr-environments). `npm run register` runs it locally. |
| `src/defineCommand.js` | Neutral, surface-agnostic command factory. |
| `src/adapters/discord/index.js` | Discord event wiring; dispatches to the router or dedicated recording handler. |
| `src/adapters/discord/interactions.js` | Convert interactions to neutral intents and deliver replies. |
| `src/adapters/discord/messages.js` | Shared Discord text splitting, embed limits, and channel/DM delivery. |
| `src/adapters/discord/mentions.js` | Mention and reply helper with thread history. |
| `src/adapters/discord/helperActions.js` | Private command delivery and requester-only confirmation buttons. |
| `src/adapters/discord/recording.js` | Shared recording runner for slash commands and authenticated helper actions. |
| `src/adapters/discord/qna.js` | Shared in-progress meeting Q&A runner; the helper adds requester authentication before invoking it. |
| `src/adapters/discord/voice.js` | Voice occupancy, meeting prompts, and recording auto-stop. |
| `src/adapters/discord/meetingPosts.js` | Meeting notifications and minutes attachments. |
| `scripts/dev-web.js` | Orchestrator: ephemeral scratch DB + scratch team-tracking + web server. |
| `scripts/preview.js` | Compose the preview selector with CLI access and operator confirmation. |
| `scripts/lib/previewCli.js` | Parse selector options and adapt GitHub/Railway CLI output to metadata, with command and API error handling. |
| `scripts/lib/preview.js` | Validate and sequence preview deployments through injected API, clock, and confirmation functions; independent of bot runtime and backend internals. |
| `scripts/lib/snapshotDb.js` | Pipes `pg_dump` from main into `psql` on scratch, under `set -e -o pipefail`. |
| `scripts/lib/spawnTeamTracking.js` | Spawns a scratch uvicorn subprocess; polls `/openapi.json` for readiness. |
| `scripts/lib/issueDevSpoofKey.js` | Runs `team-tracking-keys issue --scopes ... dev:spoof` against scratch. |
| `scripts/lib/seedDefaultPersonas.js` | Idempotently seeds Dev Superuser/Admin/Member into scratch. |
| `src/web/server.js` | Fastify web playground (see "Web playground" above). |
| `src/web/public/catalog.js` | Presentation labels, categories, search, and option serialization; command definitions stay in the registry. |
| `src/web/public/utmist-logo.png` | Unmodified copy of the meeting service's UTMIST brand asset, served within the bot image. |
| `src/web/public/mentions.js` | Client-side `<@id>` → user pill rendering. |
| `src/startupGuard.js` | Refuses web-mode boot if the directory key lacks `dev:spoof`. |

`discord.js` imports belong in the Discord adapter modules and the startup and
registration entrypoints. Commands, reply renderers, and application services
remain surface-neutral. Import the module that owns a function directly;
`adapters/discord/index.js` wires events rather than re-exporting its siblings.

All Discord replies use the shared message helpers: text splits within the
2,000-character limit, preferring Markdown section starts (`#` through `######`)
before paragraph breaks, then sentence endings, then whitespace (line breaks
before spaces or tabs). Headings stay with their following text where possible;
heading-like lines inside fenced code are ignored. A word is split only when no
suitable boundary fits. Fenced code blocks close and reopen with their language
across parts. Slash replies edit the deferred message first, then send follow-ups
with the original visibility. Embeds are
shortened with `…` to fit their property, field-count, and shared 6,000-character
limits; files and embeds appear only on the first text part. Failed sends try a
short error reply; if even that cannot be delivered, deferred replies attempt to
clear the thinking indicator. Delivery errors are logged without reply content.

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

## Durable meeting records

`/record start` saves the finalized transcript and minutes to one Google Doc by
default, through meeting → documentation-system → connectors. A consent/visibility
notice is posted before capture. `save:false` keeps the existing PDF-only flow.
Explicit stop, automatic empty-channel stop and disconnect salvage all send the
same record context. Failed hand-off still posts the PDF and a delayed-save
notice; keep that PDF and contact an admin with the meeting ID for recovery.
Google Docs remain admin-only when complete voice-channel team coverage is
unavailable. Saved Doc links are delivered and recovered independently of the
PDF; see [link delivery and recovery](docs/meeting-delivery.md).
Moving the bot to another voice channel ends a saved recording immediately;
start a new recording there to use its audience. `save:false` recordings continue
to follow moves.
See [setup and recovery](../docs/RAILWAY-DEPLOYMENT.md#meeting-record-persistence).
