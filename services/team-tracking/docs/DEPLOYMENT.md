# team-tracking — operational reference

Runtime operations for the team-tracking service: security posture, API key
management, audit log, secrets. **Infrastructure** — how the service actually
runs in staging + production — lives in the platform-wide runbook:

- **[`docs/RAILWAY-DEPLOYMENT.md`](../../../docs/RAILWAY-DEPLOYMENT.md)** — Railway + Neon setup and the branching/auto-deploy flow.
- **[`docs/DEPLOYMENT-HISTORY.md`](../../../docs/DEPLOYMENT-HISTORY.md)** — how we got there and the bugs we hit.

This file covers what's specific to *this service* regardless of where it runs.

---

## Security posture (Levels 1 + 2)

Two overlapping layers protect the API:

- **Level 1** (infrastructure): TLS termination, constant-time key comparison, secret handling outside git. On Railway, TLS + routing are provided by the platform; nothing to configure per service.
- **Level 2** (auth model): per-consumer API keys stored hashed with argon2, per-endpoint scope enforcement, revocation without a flag day, structured JSON audit log for every request.

### What this protects against

- **Casual scanning / opportunistic bots.** APIs are private-only on Railway (no public domain) plus API-key auth on every route.
- **Timing attacks on any API key.** Comparisons are constant-time (`secrets.compare_digest` for the env bootstrap key, argon2 for DB-issued keys).
- **A leaked API key.** DB-issued keys are per-consumer, scoped, and revocable in seconds — no flag day, no other consumers affected.
- **A compromised consumer.** Scopes limit blast radius. A leaked `discord-bot` key with only `people:read` can't create memberships or leak person emails via a mutation.
- **Key theft from disk.** Only argon2 hashes are stored in the DB — plaintext is shown once at issuance and never persisted.
- **Silent abuse.** Every request lands in the structured audit log with `key_name` attributed cryptographically (not self-declared).

### What this does not protect against

- **Insider-level abuse.** If someone with prod DB access exfiltrates the whole `people` table, no in-band protection stops it. Managed with access control on Neon.
- **A compromised admin key.** The `admin` scope is a wildcard. Only grant it to keys you trust; rotate promptly if suspicious.

---

## Environment variables (production/staging)

Set in Railway per environment; see the runbook's env-var table for the full list. team-tracking-specific:

| Var | Purpose |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg://…` at the Neon branch for this environment. |
| `API_KEY` | Env bootstrap admin key. Generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. Do NOT prefix with `tt_`. |
| `TT_ENV` | `staging` or `production`. Controls the `dev:spoof` scope safety gates (the CLI refuses to issue such keys and the middleware 403s them when `TT_ENV=production`). |
| `PORT` | `8000` (explicit — required so cross-service refs from consumers resolve). |
| `GOOGLE_GROUPS_CUSTOMER_ID` | Workspace customer id without the `customers/` prefix (`C01yare3k`). Optional; see [Managed Google Groups](#managed-google-groups). |
| `GOOGLE_GROUPS_DOMAIN` | Domain new team groups are created under (`utmist.skule.ca`). |
| `GOOGLE_OAUTH_CLIENT_ID` | OAuth client of the `misty-utmist` GCP project. |
| `GOOGLE_OAUTH_CLIENT_SECRET` | That client's secret. |
| `GOOGLE_OAUTH_REFRESH_TOKEN` | Offline refresh token for the group-owning account. |

The five `GOOGLE_*` variables are all-or-nothing: with none set, Google Groups sync is off and everything else works; with only some set, the service refuses to boot and names the missing ones.

Railway checks `GET /health/ready` before promoting a deployment. This route
queries this service's Postgres database and returns `503` if it is unreachable;
`GET /health` remains a DB-independent liveness check. Neither route needs an
API key. See [API.md](API.md) for response bodies.

> Why no `tt_` prefix on `API_KEY`: the auth layer routes any key shaped like `tt_<prefix>_<secret>` to the DB-issued-key path. A `tt_`-prefixed env key would take that path, find no matching row, and 401. Keep the env bootstrap key in a plain format so it falls through to the env check.

---

## API key management (`team-tracking-keys` CLI)

Every consumer (bot, docs-system, future services) authenticates with its own scoped `tt_…` key. The CLI talks directly to the DB — run it with `DATABASE_URL` pointing at the environment's Neon branch.

### Issue a key

```bash
DATABASE_URL="<neon branch URL>" \
  uv --project services/team-tracking run team-tracking-keys issue \
    --name discord-bot --scopes people:read memberships:read
```

The plaintext key is printed **once** to stdout. Everything else (name, prefix, id, scopes) goes to stderr — so `... issue ... > key.txt` captures just the key.

For the two configured platform consumers, use the provisioning script instead — it mints and wires the keys into Railway in one step:

```bash
TT_DATABASE_URL="<team-tracking Neon branch>" \
  ./scripts/provision-directory-key.sh <staging|production>
```

### Available scopes

| Scope | Grants |
|---|---|
| `people:read` | GET /people, GET /people/{id} |
| `people:write` | POST /people, PATCH /people/{id} |
| `teams:read` | GET /teams, GET /teams/{id}, GET /teams/by-slug/{slug} |
| `teams:write` | POST /teams, PATCH /teams/{id} |
| `role_kinds:read` | GET /role_kinds, GET /role_kinds/{id} |
| `memberships:read` | GET /memberships, GET /memberships/{id} |
| `memberships:write` | POST /memberships, PATCH /memberships/{id}, POST /memberships/{id}/end |
| `providers:read` | GET /providers, GET /providers/{id} |
| `identifiers:read` | GET /people/by-identifier/{provider}/{external_id}, GET /people/{id}/identifiers |
| `identifiers:write` | POST / PATCH / DELETE /people/{id}/identifiers[/{provider}] |
| `channels:read` | GET /channels/{guild_id}/{channel_id}/teams |
| `channels:write` | PUT / DELETE /channels/{guild_id}/{channel_id}/teams |
| `dev:spoof` | Allows spoofing arbitrary Discord users (bot web playground). Refused when `TT_ENV=production`. |
| `admin` | Wildcard — grants every scope. Use only for trusted operators / migrations. |

### List keys

```bash
DATABASE_URL="<neon branch URL>" \
  uv --project services/team-tracking run team-tracking-keys list --active-only
```

Only metadata prints — name, prefix, active flag, scopes. Plaintexts and hashes are never displayed.

### Revoke a key

```bash
DATABASE_URL="<neon branch URL>" \
  uv --project services/team-tracking run team-tracking-keys revoke <api_key_id>
```

Instant — next request with that key returns 401. Revoked keys stay in the table for audit history (soft delete).

### Rotate a key (no flag day)

Each consumer has its own key, so rotation is safe and staged:

1. Issue a NEW key for the same consumer with a suffixed name: `... issue --name discord-bot-v2 --scopes ...`
2. Update the consumer to use the new key (in Railway: `railway variables --service <consumer> --environment <env> --set DIRECTORY_API_KEY=<new>`).
3. Verify via the audit log that the new key is being used (grep for `"key_name":"discord-bot-v2"`).
4. Revoke the OLD key.

Zero downtime, no coordination with other consumers.

### Rotate the env bootstrap `API_KEY`

The env `API_KEY` is a grace-period bootstrap admin key. Rotate it exactly like any Railway env var: `railway variables --service team-tracking --environment <env> --set API_KEY=<new>` and Railway redeploys. Update any consumer still using it. In steady state, only your bootstrap tooling should use the env key — every real consumer should have its own DB-issued key.

---

## Audit log

Every request emits one JSON line to the process's stdout. On Railway, that's captured by the platform log stream — view with `railway service logs --service team-tracking --environment <env>` or the Railway dashboard.

Each line:
```json
{"ts":"2026-07-01T05:41:20.606Z","request_id":"682e63ff-...","method":"POST","path":"/people","status":201,"duration_ms":52,"key_name":"discord-bot","is_bootstrap":false,"remote":"203.0.113.7"}
```

### Useful queries

Using the Railway CLI with the JSON logs endpoint:

```bash
# Everything discord-bot did in the last window
railway service logs --service team-tracking --environment production --json \
  | jq 'select(.message | contains("\"key_name\":\"discord-bot\""))'

# All failed auth attempts (401)
railway service logs --service team-tracking --environment production --json \
  | jq 'select(.message | contains("\"status\":401"))'

# Scope denials (403) — spot a consumer trying to over-reach
railway service logs --service team-tracking --environment production --json \
  | jq 'select(.message | contains("\"status\":403"))'

# Requests using the deprecated env bootstrap key
railway service logs --service team-tracking --environment production --json \
  | jq 'select(.message | contains("\"is_bootstrap\":true"))'
```

That last one is your migration progress bar — as consumers move to DB-issued keys, `is_bootstrap:true` lines should drop toward zero.

---

## Managed Google Groups

team-tracking creates a Google Group for each team and keeps its members equal to the team's current membership. How it decides membership, and why, is in [ARCHITECTURE.md](ARCHITECTURE.md#managed-google-groups); the consumer-facing endpoints are in [API.md](API.md#team-google-groups). The Google-side setup and its verification are recorded in [#236](https://github.com/UTMIST/Misty/issues/236#issuecomment-6001988660).

### Credentials

The integration authenticates **as a user**, `infrastructure@utmist.skule.ca`, with an OAuth refresh token. It doesn't use a service account: user OAuth is the setup that was verified to create groups and manage external members. Groups are created with that account as their initial owner.

1. In [OAuth Playground](https://developers.google.com/oauthplayground/), choose "Use your own OAuth credentials" and enter the `misty-utmist` project's OAuth client (its redirect URIs must include `https://developers.google.com/oauthplayground`).
2. Authorize **only** `https://www.googleapis.com/auth/cloud-identity.groups`, signed in as `infrastructure@utmist.skule.ca`.
3. Exchange the code for tokens and keep the **refresh token**. Set it, the client id, and the client secret as the `GOOGLE_*` variables above, along with the customer id and domain.
4. **Check the OAuth app's publishing status first.** An External app in *Testing* gets refresh tokens that expire after seven days, which would silently stop every sync a week after deploy ([refresh-token lifetime](https://developers.google.com/identity/protocols/oauth2#expiration)).

The client secret and refresh token are credentials: keep them in Railway variables and the maintainers' private storage only. Never paste them into issues, logs, or chat.

**If the grant is revoked or expires**, every sync records `status: failed` with `last_error` `google request failed: RefreshError`. Repeat steps 1–3 as the same account, replace `GOOGLE_OAUTH_REFRESH_TOKEN`, then run the backfill below to catch up.

### Backfill and the scheduled sync

After the variables are set, create groups for all existing teams and reconcile them:

```bash
DATABASE_URL="<team-tracking Neon URL>" GOOGLE_GROUPS_CUSTOMER_ID=... GOOGLE_GROUPS_DOMAIN=... \
GOOGLE_OAUTH_CLIENT_ID=... GOOGLE_OAUTH_CLIENT_SECRET=... GOOGLE_OAUTH_REFRESH_TOKEN=... \
  uv --project services/team-tracking run team-tracking-groups sync   # or: sync --team <slug>
```

It prints one line per team (`slug  status  group_email  last_error`) and exits 1 if any team didn't reach `synced`. Retired teams that never had a group are skipped. `--project` is required for the same reason as with `team-tracking-keys`: both DB services ship a top-level `src` package.

Then run the same command **daily** as a Railway cron service on the team-tracking image (start command `team-tracking-groups sync`, the same variables as the service). API writes sync immediately, but nothing triggers on a date passing, so a membership with a future `started_at` or `ended_at` only takes effect in Google at the next scheduled run. The scheduled run also retries any team left `failed`.

### Onboarding each new group (manual step)

API-created groups reject members outside `utmist.skule.ca`, and most members use personal addresses. After a team's group is created, its mapping shows `status: needs_external_members` as soon as someone external is added:

1. Open [Google Groups](https://groups.google.com/) as a group owner (`infrastructure@utmist.skule.ca`), select the group, open **Group settings**, enable **Allow external members**, and save.
2. Re-sync: `POST /teams/{team_id}/google-group/sync` with a `teams:write` key, or `team-tracking-groups sync --team <slug>`. The status should become `synced`.

Find teams waiting on this step with `GET /teams/google-groups` and filter on `status`. Giving a team lead Google-side ownership is a Google Groups action; a Misty team role doesn't grant it.

### Existing groups and Drive shares

Automatic sync only manages the groups it creates. It never adopts, edits, or deletes UTMIST's existing standalone `@googlegroups.com` groups or Drive shares. A new team group is a separate identity: it gets no access to existing documents until someone shares them with it. Keep the old shares until a separate migration plan has moved and verified access.

## Backups

**Neon handles this** on the managed side — Postgres branches carry point-in-time recovery within Neon's retention window on the current plan. For a manually-restored snapshot, use Neon's dashboard to create a branch at a past timestamp and swap `DATABASE_URL` to it.

For belt-and-suspenders, a periodic off-Neon `pg_dump` is easy:
```bash
pg_dump -Fc "<team-tracking Neon URL>" > tt-$(date +%F).dump
```
Ship somewhere off-platform (S3, Backblaze, another shared drive). Test the restore path at least once.

---

## Verification checklist

After any deploy, from a machine with `railway` linked to the environment:

- [ ] `GET /health/ready` returns `{"status":"ok"}` without an API key; `GET /health` remains available for liveness.
- [ ] Recent deploy logs show `alembic upgrade head` ran cleanly (see the pre-deploy step)
- [ ] Request without `X-API-Key` returns 401 (with an audit line for it)
- [ ] Request with wrong key returns 401
- [ ] Request with a scope-limited DB key to an out-of-scope endpoint returns 403
- [ ] Request with correct scoped key returns 200/201
- [ ] Audit log lines are landing in Railway logs (grep for a recent `request_id`)
- [ ] Revoke a test key and verify 401 on the next request
- [ ] If Google Groups is configured: `GET /teams/google-groups` shows no team stuck at `failed`

## When it's time to level up further

- **Level 3 (context-dependent, currently unnecessary):**
  - OAuth2/OIDC for human users of a web UI backed by this API
  - HMAC request signing or mTLS for the highest-trust callers
  - IP allowlisting per key
  - `api_request_log` DB-backed audit table (in addition to stdout log) for queryable investigations
  - Compliance-driven controls (SOC2 audit trail retention, key rotation SLA, etc.)

Most of these are only worth the work if a compliance requirement forces them.
