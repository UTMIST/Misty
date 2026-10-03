# Access and credentials

Who to ask for what, and — more usefully — **how much you can do without asking
anyone.**

[`DEVELOPMENT.md`](DEVELOPMENT.md) explains what each environment variable
*means*. This page answers the different question a new contributor actually
has: *where do I get the values, and what happens if I can't?*

---

## The short version

**You do not need any UTMIST credentials to contribute.** Not AWS, not Railway,
not Neon, not a Discord bot token. Clone, `make install`, `make check`, and you
can write and test a real change on day one.

That is a deliberate property of the codebase, not an accident:

- Every test suite runs **offline against fakes** injected via
  `app.dependency_overrides`. No suite is allowed to make a real AWS, Google, or
  LLM call — see [`AGENTS.md`](../AGENTS.md).
- Every service ships a `.env.example` with **working local defaults**. Copy it
  and the service boots.
- The bot's playground (`npm run dev:web`) runs the full command surface in a
  browser with **no Discord token**.

If someone tells you that you need production access to get started, they are
wrong, and the friction is worth pushing back on.

---

## What works with nothing

Verified by running it, not by reading config:

| You want to | Credentials needed | Notes |
|---|---|---|
| Run any test suite | **none** | Docker only for the Postgres-adapter suites (`make test` skips those) |
| Boot `team-tracking`, `documentation-system`, `verification` | **none** | Docker for the local Postgres. `verification` defaults to `EMAIL_BACKEND=fake`, which drops mail |
| Boot `llm`, `meeting`, `connectors` | **none** | They start and answer `/health` without AWS or Google. Only the outbound calls fail |
| Develop and test bot commands | **none** | `npm run dev:web` — needs Docker, not a token |
| Add an endpoint, a migration, a storage method, a command | **none** | The entire normal contribution loop |

The degradation is designed, not accidental. `connectors` with no Google
credentials boots and returns 503 for Google sources — [a supported running
state, not a broken one](DEVELOPMENT.md). `/record` with no `MEETING_BASE_URL`
reports "not configured" instead of crashing.

---

## What genuinely needs access

Only once you're past local development.

| To do this | You need | Ask |
|---|---|---|
| Push a branch, open a PR | GitHub write on `UTMIST/Misty` | @qiuethan (step 1 of [`ONBOARDING.md`](ONBOARDING.md)) |
| Run a **real** Discord bot (not the playground) | Your own bot token + client id | Nobody — [make your own app](https://discord.com/developers/applications). See below |
| Test slash commands in a shared server | Invite to the testing guild + its `DISCORD_GUILD_ID` | @qiuethan — he'll invite you and give you the guild id |
| Exercise `/chat` or live transcription | AWS creds with **Bedrock** and **Transcribe** enabled | @qiuethan — there's a shared UTMIST AWS account, and he issues scoped IAM credentials per dev. Expect to need this if you own the `services/llm` or `services/meeting` zone |
| Send real verification email | A Resend API key, or Gmail service-account JSON | @qiuethan — rarely needed; `EMAIL_BACKEND=fake` covers local work |
| View logs, restart, or set variables on a deploy | Railway project member | @qiuethan — added on request; say what you're debugging |
| Inspect or branch a database | Neon project member | @qiuethan — added on request; say what you're debugging |
| Merge to `main` (production release) | Approval per [`RAILWAY-DEPLOYMENT.md`](RAILWAY-DEPLOYMENT.md) | @qiuethan signs off on every `staging → main` promotion |

Yes, every row says @qiuethan. That's the current reality of a small org, not
a policy — as zones get owners, expect some of these to delegate.

### Making your own Discord app

You don't need UTMIST's bot to develop against Discord — make your own, and
you'll get a token you fully control:

1. [Discord Developer Portal](https://discord.com/developers/applications) →
   **New Application**.
2. **Bot** → copy the token into `DISCORD_TOKEN`. **General Information** → copy
   the Application ID into `DISCORD_CLIENT_ID`.
3. Invite it to a server you own (OAuth2 → URL Generator → `bot` +
   `applications.commands`).
4. Set `DISCORD_GUILD_ID` to that server so beta commands register instantly
   instead of waiting on Discord's global propagation.

Do this before asking anyone for shared access. It's faster and it can't break
anything the club depends on.

### The one key you mint locally

`DIRECTORY_API_KEY` is not something anyone gives you — you issue it yourself
against your own local `team-tracking`:

```bash
uv --project services/team-tracking run team-tracking-keys issue \
  --name discord-bot \
  --scopes people:read people:write people:elevate \
           identifiers:read identifiers:write \
           teams:read teams:write memberships:read memberships:write \
           role_kinds:read
```

Always pass `--project`, and check the token starts with `tt_` — see the
`src`-collision gotcha in
[DEVELOPMENT.md → Troubleshooting](DEVELOPMENT.md#troubleshooting).

---

## When someone leaves

The reason this platform exists is that UTMIST turns over every year, so
offboarding is part of the design rather than an afterthought.

When a contributor steps away, whoever holds admin should:

- **Revoke their directory keys.** `team-tracking-keys revoke` — keys are
  per-consumer and independently revocable by design.
- **Rotate any `CONSUMER_KEYS` entry they held** for `llm`, `meeting`, or
  `connectors`. These have no revoke command: drop the entry from the JSON array
  and redeploy. See [`DEPLOYMENT-HISTORY.md`](DEPLOYMENT-HISTORY.md).
- **Remove them from Railway, Neon, AWS, and the GitHub repo.**
- **Retire their directory record** — `active=false`, memberships closed with
  `ended_at`. Never hard-delete; the audit history has to stay readable.

Deliberately *not* in scope here: rotating the bootstrap `API_KEY` on every
service is disruptive and is a decision for whoever operates the deploy, not a
routine offboarding step.

---

## See also

- [`ONBOARDING.md`](ONBOARDING.md) — joining the team: zones, getting work, PR norms
- [`DEVELOPMENT.md`](DEVELOPMENT.md) — what each variable does, and local setup
- [`RAILWAY-DEPLOYMENT.md`](RAILWAY-DEPLOYMENT.md) — the deploy/operate runbook
- [`DEPLOYMENT-HISTORY.md`](DEPLOYMENT-HISTORY.md) — why key storage differs per service
