# Deployment Design & Lessons

Why the UTMIST ops platform is deployed the way it is, and the non-obvious lessons we learned standing it up. For the operational **runbook** (how to redeploy, provision keys, verify, roll back), see [`RAILWAY-DEPLOYMENT.md`](RAILWAY-DEPLOYMENT.md). This document explains the *choices*.

---

## The shape

```
                                Railway staging environment        Railway production environment
GitHub                          ├── team-tracking                   ├── team-tracking
├── staging branch  ──deploys──▶├── documentation-system            ├── documentation-system
└── main branch     ──deploys──▶├── verification                    ├── verification
                                ├── llm          (no DB)            ├── llm          (no DB)
                                ├── meeting      (no DB, stateful)  └── discord-bot
                                ├── connectors   (no DB)
                                └── discord-bot
                                        │                                   │
                                        ▼                                   ▼
                                  Neon: team-tracking, docs-system,   Neon: same three projects,
                                  verification — `staging` branch     `main` branch of each
                                  of each
```

Two environments (staging, production). Seven shared services (each is one Railway
entity; variable *values* differ per environment). Three Neon projects — one per
DB-owning service — each with two branches (one per environment). `llm`,
`meeting`, and `connectors` own no database. One repo, two long-lived branches,
feature → staging → main.

**`meeting` and `connectors` are staging-only for now.** They are the two newest
services and neither has been promoted to production. `/record` is registered
globally but answers "not configured" in production until `meeting` is
provisioned there and `MEETING_BASE_URL` is set on the production bot; and until
`connectors` is provisioned, production documentation-system can catalog docs
normally but cannot fetch Google source content — each attempt is recorded as a
per-doc ingest warning.

---

## The choices, and why we made them

### Railway over a self-hosted VPS

UTMIST has volunteer turnover: the person who set up a box will graduate, and the next cohort inherits a mystery server they didn't provision. Railway removes the "who owns the box" question — deploys are `git push`, there's no OS to patch, and the next volunteer inherits a dashboard, not a Linux system. Cost is ~$5–20/mo; the turnover-proofing is worth more than that.

### Neon over self-hosted Postgres

Same reasoning, plus Neon's **branching** feature. `staging` is a copy-on-write branch of prod — realistic data, isolated writes, and reset-able in seconds. A separately-seeded staging DB would drift from prod forever and require ongoing sync work.

### One Neon project per service

Each service owns its DB. team-tracking's DB never contains a doc catalog entry; docs-system's DB never contains a person record. Services talk over HTTP, not shared tables. Matches the "each service is a source of truth for one domain" principle — and Neon's free-tier limits mean each service gets its own storage + compute budget.

### Shared services with per-environment variables

Each service exists once at the Railway project level and is exposed in *both* environments with different variable values. This is Railway's idiomatic pattern: adding a variable later is one dashboard change, not two. Cross-service references (`${{team-tracking.RAILWAY_PRIVATE_DOMAIN}}`) resolve to the right environment automatically.

### Three services deliberately have no database

`llm` is a thin stateless proxy over Bedrock; `meeting` holds only ephemeral
in-memory sessions; `connectors` is a stateless outbound adapter that returns
what it fetches and keeps nothing. None owns a domain worth persisting, so none
got a Neon project, an `api_keys` table, or Alembic. Their API keys are seeded
from a `CONSUMER_KEYS` JSON env var parsed at boot.

The trade: **key rotation is a redeploy**, not a CLI call, and there is no
revoke command — you drop the entry and redeploy. That's acceptable for three
services with exactly one internal consumer each, and it avoids provisioning a
database purely to store one row apiece.

### `meeting` is the one stateful service

Everything else is stateless and scales freely. `meeting` keeps each live
meeting's session in process memory, so it **must run as a single replica**
unless sticky routing on `session_id` is added — a given meeting's WebSocket,
`/transcript` polls, and `/stop` must all reach the same process. This was a
considered trade (live transcription needs *somewhere* to hold a rolling
transcript, and the bot is deliberately processing-free), not an oversight. See
[`MEETING-RECORDING.md`](MEETING-RECORDING.md).

### Branching model: feature → staging → main

- **`staging` is the default branch.** Feature PRs auto-target it. Merges auto-deploy to the Railway staging environment.
- **`main` is the release branch.** Only PRs from `staging` can merge — enforced by the `main-source-guard` workflow. Merges auto-deploy to production.
- Both branches require **all 10** CI jobs as status checks; `main` additionally requires the source-guard.

This gives you the loop of `push to feature → PR → staging → real staging deploy → validate → promote to main → production deploy`, with no branch or environment able to skip validation.

### Per-consumer scoped API keys

The bot and docs-system authenticate to team-tracking with their **own** scoped `tt_…` keys — not a shared admin bootstrap. A leaked bot key can't be used by docs-system, each is revocable independently, and the audit log records who did what by name.

Keys are minted by `scripts/provision-directory-key.sh <env>`, which runs `team-tracking-keys issue` against that environment's Neon branch and writes each key onto its Railway consumer with `railway variables --set`. One command per environment.

Scopes:
- **discord-bot:** `people:read people:write people:elevate identifiers:read identifiers:write teams:read teams:write memberships:read memberships:write role_kinds:read` (needs identity + membership management; `people:elevate` lets `/seed` promote people to `admin`/`superuser`)
- **documentation-system:** `people:read teams:read` (only needs to look up an owner's label)

The DB-free services follow the same *principle* with a different mechanism —
scoped, per-consumer, argon2-hashed keys, just seeded from `CONSUMER_KEYS`
rather than a table, and so **not** covered by `provision-directory-key.sh`:
- **`llm`** issues a `chat`-scoped key to `meeting` (its only consumer today).
- **`meeting`** issues a `meetings`-scoped key to `discord-bot`.
- **`connectors`** issues a `fetch`-scoped key to `documentation-system`.

Minting them is a manual per-environment step — see the runbook's
[step 4b](RAILWAY-DEPLOYMENT.md).

### CI on every PR to `staging` or `main`

`.github/workflows/ci.yml` runs **10 jobs**:
- **`python-test`** — team-tracking, Postgres 16 service container, applies migrations, runs the full pytest suite
- **`python-lint`** — team-tracking ruff check + format
- **`auth-lib-test`** — the shared `packages/auth` (`platform_auth`) pytest suite + ruff check/format
- **`verification-test`** — services/verification, Postgres 16 service container, `alembic upgrade head`, then `pytest` + ruff check/format
- **`llm-test`** — services/llm pytest suite + ruff check/format
- **`meeting-test`** — services/meeting pytest suite + ruff check/format. No Postgres container and no AWS credentials: the service has no database, and its Transcribe/LLM clients are faked via `app.dependency_overrides`, so the suite runs fully offline
- **`connectors-test`** — services/connectors pytest suite + ruff check/format. No container and no Google credentials: the Google API clients are faked, so the suite runs fully offline
- **`documentation-system-test`** — Postgres 16 service container, runs `alembic upgrade head`, then `pytest` with `RUN_PG_TESTS=1` + ruff check/format
- **`node-test`** — the bot's `node --test` suite
- **`docker-build`** — builds *and boot-smoke-tests* every service image (`python -c "import src.api.app"` for the six APIs, `node --check src/index.js` for the bot)

**All ten are required status checks** in branch protection on both `staging`
and `main`; `main` additionally requires `main-source-guard`. A red job on any
of them blocks the merge — there is no "runs but doesn't gate" tier.

> `meeting-test` and the meeting `docker-build` steps were added late — the
> service shipped to staging with **no CI coverage at all** for a couple of
> weeks. If you add a service, add its CI job in the same PR.

Three further workflows fire on PRs but gate nothing:
- **`pr-zone-check`** — warns (non-blocking) when a PR spans more than one
  CODEOWNERS zone. Flip its trailing `exit 0` to `exit 1` to enforce.
- **`discord-pr-notify`** — posts to Discord when a PR needs attention; the
  target channel is a repo secret (`DISCORD_STAGING_WEBHOOK` /
  `DISCORD_PROD_WEBHOOK`), so changing it needs no code change. Unset secret =
  no-op.
- **`blocked-ready-automation`** — keeps `blocked`/`ready` issue labels in sync
  from `Blocked by: #N` lines in issue bodies, and drops `ready` when an issue
  is assigned.

---

## Non-obvious things worth knowing (lessons)

Three sharp edges bit us during the first live deploy. Each is documented so nobody has to learn them from a failed deploy.

### Wrap shell-variable start commands in `sh -c`

Railway executes a string `startCommand` in `railway.json` directly — no shell interpretation. So `uvicorn ... --port ${PORT}` passes the literal string `${PORT}` to uvicorn, which rejects it with `Error: Invalid value for '--port': '${PORT}' is not a valid integer.`

Wrap it explicitly:
```json
"startCommand": "sh -c 'uvicorn src.api.app:app --host 0.0.0.0 --port ${PORT}'"
```
The `sh -c` runs a shell that expands `$PORT` (which Railway injects at container start) before uvicorn sees it.

### Bind APIs to `0.0.0.0`, not `::`

On the default `python:3.11-slim` kernel, binding to `::` binds **IPv6-only**. Railway's healthcheck reaches the container over IPv4 and finds no listener, so every deploy fails healthcheck even though uvicorn started fine and migrations succeeded.

Bind to `--host 0.0.0.0`. Railway's private network routes to the container's port regardless of bind family, so internal service-to-service calls still work.

### If you cross-reference a service's `PORT`, set it explicitly

Railway's cross-service template `${{team-tracking.PORT}}` resolves from the **variable store**. `PORT` as Railway injects it at container runtime is *not* in the variable store — so the template resolves to an empty string, and consumers try to hit `http://team-tracking.railway.internal:` with no port, which fails.

Set `PORT=8000` (or whatever you like) as an **explicit** Railway variable on any service being referenced. Railway respects your value at container start, *and* the template ref now resolves correctly on consumers.

### Both branches require an approving review

`staging` and `main` are both branch-protected with `required_approving_review_count: 1`. Auto-merging your own PR isn't possible; use the standard review flow or an admin merge for genuinely trivial changes (e.g. docs-only). Removing this requirement would speed things up but weaken the safety net — don't do it without a real reason.

---

## Release log

### 2026-08-08 — meeting reliability + platform hardening (staging)

On `staging`, not yet promoted. The user-visible half is a `meeting` fix; the
rest is platform hygiene with no runtime behavior change.

- **A dropped WebSocket no longer loses a recording (#190).** Two fully
  transcribed production meetings were lost in one morning because the
  disconnect path called `discard()` immediately, so the bot's follow-up
  `POST /stop` 404'd. A disconnect now **holds** the session for
  `DISCONNECT_GRACE_S` (default 60 s) and the bot answers an unexpected
  `onClose`/`onError` by running the normal finalize. Both halves are required.
  Set `DISCONNECT_GRACE_S=0` for the old behavior. See
  [`MEETING-RECORDING.md`](MEETING-RECORDING.md).
- **`meeting`'s WebSocket keepalive was widened** to
  `--ws-ping-interval 60 --ws-ping-timeout 60` in its `railway.json`
  `startCommand` (and the Dockerfile `CMD`). uvicorn's 20 s/20 s default closed
  the socket on a briefly-slow client mid-recording — a leading suspect for the
  same two lost meetings. **`meeting` is the one service whose `startCommand`
  is not the plain template**; keep the two in step if you edit either.
- **Credentials are `SecretStr` platform-wide (#162).** Every service's
  `Settings` credential fields, plus `packages/auth`'s documented convention. A
  plain `str` had once printed a real Google private key into a transcript.
- **`ruff format --check` is enforced for every Python service.** The last
  deferrals (documentation-system, meeting, `packages/auth`) are gone; the
  services were reformatted in their own commits, separate from behavior changes.
- **Dockerfile port fixes (#179) and compose build contexts (#180)** — `meeting`
  exposed 8003 and documentation-system 8000, rather than 8004 and 8001; `llm`
  and `connectors` pointed their compose build context somewhere the uv
  workspace wasn't reachable. Both only ever affected local/compose runs —
  Railway's `startCommand` overrides the Dockerfile `CMD`.
- **Per-service doc sets completed (#187), agent guides added.** Every service
  now has the full `README` + `docs/{API,ARCHITECTURE,CONTRIBUTING,DEPLOYMENT}.md`
  set, and `AGENTS.md` is the single source of agent instructions.
- **No migrations.** Nothing in this train touches a schema.

**Promoting this train does not deploy `meeting` or `connectors` to
production** — see "Current state" above for what's still manual.

### 2026-08-01 — connectors (staging)

A **sixth service**, `services/connectors`: a stateless outbound adapter that
fetches Google Docs/Sheets/Slides/Drive content as text on behalf of internal
consumers, via a service account. No database, no Alembic — keys come from
`CONSUMER_KEYS`.

- **One consumer today:** documentation-system calls `POST /fetch` for Google
  source content, with a `fetch`-scoped key. The dependency is **soft** —
  documentation-system boots and works without `CONNECTORS_API_KEY` set or
  connectors reachable; Google fetches degrade to per-doc ingest warnings. That
  is why `verify_production_secrets()` only *warns* about `CONNECTORS_API_KEY`
  rather than failing the deploy.
- **Deliberately not a gateway and not an authorization boundary** — it fetches
  what it is asked for and lets the caller decide who may see it. See its
  [ARCHITECTURE](../services/connectors/docs/ARCHITECTURE.md).
- **`MAX_CONTENT_CHARS` sits above documentation-system's own clamp**
  (1,200,000 vs 1,000,000) so the *consumer's* limit is the one that trips and
  reports a truncation warning.
- **CI:** added the `connectors-test` job (pytest + ruff check/format, fully
  offline against faked Google clients).
- **Keys:** a third manually-minted consumer key per environment — see
  [step 4b](RAILWAY-DEPLOYMENT.md).

**Not in production.** Provisioned on staging only.

### 2026-07-26 — meeting recording v2 (staging)

The `/record` feature landed on `staging` as a **new fifth service** plus a
dedicated voice surface in the bot. This is the largest structural change since
the platform was first deployed.

- **New service `services/meeting`.** Stateful HTTP + WebSocket service: ingests
  live per-speaker Discord Opus over one WS per meeting, decodes in-process via
  PyAV, transcribes with Amazon Transcribe streaming, summarizes via the `llm`
  service, and returns minutes as a branded PDF. No database — keys come from
  `CONSUMER_KEYS`, and there is no Alembic directory.
- **New bot surface.** `/record start|status|stop` bypasses the neutral
  command router entirely and drives a dedicated adapter path, because a
  long-lived stateful recording that posts attachments doesn't fit the
  request/response reply contract. It re-runs the router's authorization check
  itself so the two can't drift. See [`MEETING-RECORDING.md`](MEETING-RECORDING.md).
- **Auto-stop.** Recording ends automatically once the voice channel empties
  (debounced, re-checked at fire time, timers bound to `sessionId` so a stale
  timer can't kill a later recording), with a **4h `max_meeting_ms` backstop**
  so a forgotten meeting can't grow memory without bound. Head-count is read
  from `voiceStates`, not `channel.members` — the latter needs the privileged
  `GuildMembers` intent and silently miscounted, which made auto-stop never fire
  in an early version.
- **`@discordjs/voice` 0.18 → 0.19.2**, required for Discord's now-mandatory
  DAVE voice E2EE.
- **`/record` promoted beta → stable** (#131), so it registers globally.
- **No migrations.** `meeting` owns no schema; nothing to apply.

**Follow-up, same release train (#136).** Per-speaker Transcribe streams were
made **persistent**: audio is pushed in as it arrives and never replayed, so a
second of speech is billed once and polling `GET /transcript` is free. The
earlier model re-transcribed each speaker's whole buffer on every poll, making
AWS cost grow with the *square* of meeting length. The same change **dropped
audio output entirely** — no MP3 mix, no `audio_b64`, no `ffmpeg` binary in the
image, and no disk writes at any point. Only the minutes PDF is returned.

**Not in production.** `meeting` is provisioned on staging only. `/record` is
globally registered, so it is *visible* in production but answers "not
configured" there until the service is deployed and `MEETING_BASE_URL` is set on
the production bot.

**Keys:** two new manually-minted consumer keys per environment — `llm` issues a
`chat`-scoped key to `meeting`, and `meeting` issues a `meetings`-scoped key to
`discord-bot`. These are *not* handled by `provision-directory-key.sh`.

### 2026-07-14 — security-review remediation (staging)

The remediation from the 2026-07-13 security review merged to `staging`. In one release:

- **team-tracking:** an SSRF egress guard on outbound fetches; a `PATCH /people`
  privilege-escalation fix backed by a new **`people:elevate`** scope (required to
  set a non-`member` `access_level` on `POST`/`PATCH /people`; plain `people:write`
  can no longer escalate, `admin` still satisfies it); a membership temporal
  no-overlap constraint, `400`s on bad foreign-key references, and `active_only`
  filtering.
- **documentation-system:** URL dedup hardened via a partial unique index on
  `url_normalized WHERE active`.
- **verification:** `confirm-code` replay hardening — an idempotent replay on a
  consumed-but-unexpired code now re-checks the submitted code (no email leak on a
  wrong code) and enforces the same attempt limit (`429`).
- **llm:** `POST /chat` now requires the dedicated **`chat`** scope (was: any valid key).

**Migrations applied on staging** — team-tracking **007** (membership no-overlap via a
`btree_gist` exclusion constraint; the migration creates the `btree_gist` extension) and
documentation-system **004** (the partial unique index above). Both run automatically as
Railway's `preDeployCommand` (`alembic upgrade head`).

**CI:** added the **`documentation-system-test`** job (Postgres 16 service +
`alembic upgrade head` + `pytest` with `RUN_PG_TESTS=1`; ruff deferred).

**Keys:** the discord-bot directory key was rotated to add `people:elevate`
(`scripts/provision-directory-key.sh` updated) so its `/seed` can promote people to
`admin`/`superuser`.

**Discord:** `/doc` was promoted from beta to stable and now registers globally.

## Current state

- Both environments deployed and healthy, with one gap: **the two newest services, `meeting` and `connectors`, are staging-only** (see the 2026-07-26 and 2026-08-01 release notes). Promoting `staging → main` ships their *code* to production; it does not create their Railway services, set their variables, or mint their production consumer keys. Those are manual steps — see [`RAILWAY-DEPLOYMENT.md`](RAILWAY-DEPLOYMENT.md) steps 2, 3, and 4b.
- APIs are **private-only** on Railway (no public domains). Only in-project services reach them, over Railway's internal network. Add a public domain later if an external caller ever needs one — every service already has API-key auth.
- **Discord commands.** All stable commands (`/link`, `/whoami`, `/seed`, `/team`, `/my-teams`, `/doc`, `/record`, plus the email-verification set `/add-email`, `/verify-email`, `/verify-code`, and `/help`) are registered globally on the production bot; **0 beta commands** remain guild-scoped (every command in `discord-bot/src/commands/index.js` is `beta: false`). To ship a future beta command, add it with `beta: true`, validate it in the staging test guild, then flip `beta: false` in its module + re-run `registerCommands` to promote it globally.
- **Migrations run automatically** as Railway's `preDeployCommand` on the three DB-backed services (team-tracking, documentation-system, verification) — `alembic upgrade head` against the environment's Neon branch before every deploy. Idempotent. `llm`, `meeting`, and `connectors` have no `preDeployCommand` because they own no schema.
- **Migration counts:** team-tracking **007**, documentation-system **006**, verification **001**.

### Known gaps

- **`/record` is visible but non-functional in production** until `meeting` is provisioned there. Deliberate (the command degrades gracefully rather than erroring), but it *is* user-visible.
- **Google source content can't be fetched in production** until `connectors` is provisioned there. Not user-visible as an error: documentation-system boots fine without `CONNECTORS_API_KEY` and records each failed Google fetch as a per-doc ingest warning, so the catalog keeps working with empty content snapshots for `gdocs`/`gsheets`/`gslides`/`gdrive` sources.
- **`documentation-system` and `verification` dev Postgres both bind host 5434**, so they can't run locally at the same time as configured. Affects local dev only, not deployments — each has its own Neon project in Railway.
- ~~**`ruff format` is not enforced** for `documentation-system` or `meeting`.~~ **Closed.** Both services were formatted and their deferrals removed; `packages/auth` gained the missing step at the same time. Every Python CI job now gates `ruff check` and `ruff format --check`.

---

## Redeploy and rollback

**Deploying is merging.** Merge to `staging` → Railway rebuilds and deploys staging. Merge `staging → main` → Railway does the same for production. No other trigger needed.

**Manual redeploy** (rare — after a Railway hiccup):
```bash
railway service redeploy --service <name> --environment <staging|production> --yes
```

**Rolling back:**
```bash
# Roll back staging:
git checkout staging && git revert <bad-commit> && git push
# Roll back production: revert on staging first, then promote via a staging → main PR.
```

If the bad change included a schema migration, downgrade it against the environment's Neon branch:
```bash
railway run --service team-tracking --environment <env> -- alembic downgrade -1
```
Every migration ships with a working `downgrade()` — that's a review requirement.

Neon also offers point-in-time recovery: reset a branch to an earlier snapshot from the Neon dashboard. A lifeline if a bad migration *and* data corruption happen together.

---

## Access

- **Railway project (`Bot-Deploy`)** — invited via the Railway workspace.
- **Neon projects** — invited via each project's settings.
- **GitHub repo** — standard PR flow; branch protection enforces review + CI.
- **Discord bots** — tokens live *only* in Railway (per-environment); the developer portal shows the app, but you need to be invited as a team member to see it there.

**Superuser seeding** is a controlled action — see the runbook's "seed the first admin" section. Only existing superusers/admins can promote more people from Discord (`/seed`).

---

## Explicitly out of scope (for now)

- A local `docker-compose.yml` for the whole platform. The existing native flows (`npm run dev:web` for the bot playground, per-service quick-starts for the APIs) cover local dev better than a compose file would.
- Any CD beyond Railway's built-in auto-deploy. Railway watches the branches; no extra pipeline needed.
- Custom domains for the APIs — they're private-only until an external caller appears.
- Observability beyond Railway's built-in logs and metrics — sufficient for current scale.

## Nice-to-haves if the itch strikes

- **SHA-pin CI actions** (currently on major tags) for supply-chain hardening.
- **Add `concurrency:` blocks to CI** so superseded PR pushes auto-cancel.
- **Custom domain** for team-tracking or docs-system if a dashboard ever wants to hit them from outside Railway.
