# team-tracking — operational reference

Runtime operations for the team-tracking service: security posture, API key
management, audit log, secrets. **Infrastructure** — how the service actually
runs in staging + production — lives in the platform-wide runbook:

- **[`docs/RAILWAY-DEPLOYMENT.md`](../../../docs/RAILWAY-DEPLOYMENT.md)** — Railway + Neon setup and the branching/auto-deploy flow.
- **[Platform architecture](../../../docs/ARCHITECTURE.md)** — service boundaries, key storage and deployment constraints.

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
| `DATABASE_URL` | `postgresql+psycopg://…` at the Neon branch for this environment. Managed Google Groups requires the direct endpoint; see [database connection](#database-connection). |
| `API_KEY` | Env bootstrap admin key. Generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. Do NOT prefix with `tt_`. |
| `TT_ENV` | `staging` or `production`. Controls the `dev:spoof` scope safety gates (the CLI refuses to issue such keys and the middleware 403s them when `TT_ENV=production`). |
| `PORT` | `8000` (explicit — required so cross-service refs from consumers resolve). |
| `GOOGLE_GROUPS_CUSTOMER_ID` | Managed Google customer id without the `customers/` prefix. The new `utmist.ca` organization is `C00kwr2qe`; see [Google identity setup (#236)](https://github.com/UTMIST/Misty/issues/236). |
| `GOOGLE_GROUPS_DOMAIN` | Domain new team groups are created under: `staging.groups.utmist.ca` for nonproduction, `utmist.ca` for the new production setup. |
| `GOOGLE_OAUTH_CLIENT_ID` | Internal OAuth client from the matching environment's GCP project. |
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
| `people:elevate` | Additional scope for non-`member` creation and any non-null `access_level` in PATCH; see [access-level rules](../README.md#behavior-notes) |
| `teams:read` | GET /teams, GET /teams/{id}, GET /teams/by-slug/{slug} |
| `teams:write` | POST /teams, PATCH /teams/{id} |
| `role_kinds:read` | GET /role_kinds, GET /role_kinds/{id} |
| `memberships:read` | GET /memberships, GET /memberships/{id} |
| `memberships:write` | POST /memberships, PATCH /memberships/{id}, POST /memberships/{id}/end |
| `providers:read` | GET /providers, GET /providers/{id} |
| `identifiers:read` | GET /people/by-identifier/{provider}/{external_id}, GET /people/{id}/identifiers |
| `identifiers:write` | POST / PATCH / DELETE /people/{id}/identifiers[/{provider}] |
| `channels:read` | GET /channels/{guild_id}/{channel_id}/teams |
| `channels:write` | PUT / DELETE /channels/{guild_id}/{channel_id}/teams, POST .../teams/append |
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

team-tracking creates a Google Group for each team and keeps its members equal to the team's current membership. How it decides membership, and why, is in [ARCHITECTURE.md](ARCHITECTURE.md#managed-google-groups); the consumer-facing endpoints are in [API.md](API.md#team-google-groups). [Google identity setup (#236)](https://github.com/UTMIST/Misty/issues/236) preserves the setup history.

### Credentials

The integration authenticates **as a user** with an OAuth refresh token. Groups
are created with that account as their initial owner. The replacement identities
are `misty@staging.groups.utmist.ca` for nonproduction and `misty@utmist.ca` for
production; consult [Google identity setup (#236)](https://github.com/UTMIST/Misty/issues/236)
for the migration status before replacing any existing deployment.

1. Use the matching project's **Internal**, **Desktop app** OAuth client with an
   operator-run local loopback authorization flow. Follow Google's
   [installed-app flow](https://developers.google.com/identity/protocols/oauth2/native-app)
   with state verification, PKCE, and offline access. These clients do not use
   OAuth Playground's web redirect.
2. Enable the **Cloud Identity API**, **Groups Settings API**, and **Admin SDK API**
   in that project. Google's [Settings API prerequisites](https://developers.google.com/workspace/admin/groups-settings/limits)
   also require the Directory API to be enabled; this does not add a Directory
   OAuth scope. Sign in as the environment's group-owning account and authorize:
   `https://www.googleapis.com/auth/cloud-identity.groups` and
   `https://www.googleapis.com/auth/apps.groups.settings`. Adding a scope in code
   does not extend an existing grant; reauthorize older membership-only tokens.
3. Exchange the code privately, retain the refresh token, and verify token refresh
   and reads from both APIs on a disposable managed group before setting the five
   `GOOGLE_*` variables. Confirm the account can update that group's settings under
   UTMIST's policy; OAuth consent alone does not grant Google administrative rights.
4. Keep the audience **Internal**. An External app in *Testing* has a seven-day
   refresh-token lifetime for this scope. Internal avoids that testing limit,
   but grants can still expire or be revoked; see
   [refresh-token lifetime](https://developers.google.com/identity/protocols/oauth2#expiration).

The client secret and refresh token are credentials: keep them in Railway variables and the maintainers' private storage only. Never paste them into issues, logs, or chat.

**If the grant is revoked or expires**, sync records `status: failed`. Membership
access failures report `google request failed: RefreshError`; settings-only grant
failures report `group settings: RefreshError` with recovery guidance and still
allow membership removals using the existing scope. Repeat steps 1–3 as the same
account, replace `GOOGLE_OAUTH_REFRESH_TOKEN`, and retry a targeted sync before
backfilling. Boot validates the five settings' presence, not remote permissions;
remote authorization is verified during sync.

### Database connection

When Google Groups is enabled, both the API and sync cron must use Neon's **direct** connection string for `DATABASE_URL` (hostname without `-pooler`). The per-team sync lock spans several transactions on one database session. Neon's transaction pooler cannot preserve that session; see [the concurrency design](ARCHITECTURE.md#managed-google-groups). The existing SQLAlchemy connection pool can remain enabled.

### Backfill and the scheduled sync

After the variables are set, create groups for all existing teams and reconcile them:

```bash
DATABASE_URL="<team-tracking Neon URL>" GOOGLE_GROUPS_CUSTOMER_ID=... GOOGLE_GROUPS_DOMAIN=... \
GOOGLE_OAUTH_CLIENT_ID=... GOOGLE_OAUTH_CLIENT_SECRET=... GOOGLE_OAUTH_REFRESH_TOKEN=... \
  uv --project services/team-tracking run team-tracking-groups sync   # or: sync --team <slug>
```

It prints one line per team (`slug  status  group_email  last_error`) followed by
a structured `groups_sync_finish` summary. Recorded provider failures do not
stop other membership changes or teams: the command completes the pass and
exits 0, with a warning if any team did not reach `synced`.
Retired teams that never had a group are skipped. `--project` is required for
the same reason as with `team-tracking-keys`: both DB services ship a top-level
`src` package.

Run `team-tracking-groups sync` **daily** as a Railway cron
service on the team-tracking image, with the same variables as the service.
One rejected member does not mark the scheduled process as crashed. The
summary has `level: warning`, `outcome: incomplete`, and an `incomplete_teams`
count when any mapping remains unsynced. Inspect those warnings and
`/team google-status` or `GET /teams/google-groups`; Railway process completion
alone is not proof that Google membership converged. Failed mappings keep
their error, pending changes and previous `last_synced_at`, and remain excluded
from synced-group access lookups. Missing configuration, invalid CLI arguments,
database errors and unexpected exceptions still fail the process.

API writes sync immediately, but nothing triggers on a date passing, so a
membership with a future `started_at` or `ended_at` only takes effect in Google
at the next scheduled run. The scheduled run also retries any team left `failed`.

### Pending membership changes

Apply migration **010** before deploying the API and cron with pending-change tracking. Keep both on the same image version: older writers cannot preserve this state. Downgrade to 009 refuses to run while any pending change remains.

If `last_error` says a change is awaiting completion, inspect `pending_changes` via `GET /teams/{id}/google-group`, then retry the normal sync command. It checks whether pending writes have taken effect and retries a pending removal against its exact membership resource if the person still needs removal. An unresolved addition is never replayed; a removal for someone who rejoined must finish before they can be added again. The mapping stays `failed` until all pending changes resolve and current directory membership is applied. Other addresses are still reconciled. A pending addition may finish after someone leaves; the next sync removes it, and the mapping stays excluded from synced-group lookups until that reconciliation succeeds. Run the recovery sync promptly for revocations; the daily cron is the fallback.

Connection-establishment timeouts and OAuth refresh failures leave no new pending intent because the membership request was not accepted. A normal sync retries these failures automatically.

Cloud Identity exposes no operation-polling endpoint. A pending write whose effect never appears and cannot be safely retried requires operator investigation. Use the recorded operation name, address, and exact membership resource with the Google owner/support to establish that the request has terminated. Only after that confirmation may a maintainer remove the corresponding entry from `team_google_groups.pending_changes`, while API and cron writers are stopped, then run a normal sync. Do not clear entries based only on elapsed time or an empty member list: that recreates the delayed-addition race. Lost responses may have no operation name and require the same investigation.

### External-member setup and recovery

Normal sync configures and verifies external-member settings automatically before
adding members. It changes only **Allow external members** and **Who can add
external members**, preserving other privacy settings. UTMIST owns the organization;
no external Workspace administrator is required. Groups Settings authorization
and organization policy still have to permit these changes.

If `last_error` starts with `group settings`, no new additions are attempted;
removals still run and pending membership writes remain tracked. Check API
enablement, the OAuth grant's `apps.groups.settings` scope, and the owning
account's Google privileges. OAuth scopes and group ownership alone do not prove
that Workspace policy permits settings writes. Verify settings reads and writes
using the configured account and an approved administrative role before testing
external-member additions; allow time for privilege changes to propagate.
Retry normal sync after recovery. It reads settings first, so a lost PATCH response
or a previous successful configuration does not cause blind repeated writes.

If Google still refuses an external address after verification
(`needs_external_members`), ask UTMIST's Google administrator to check group and
organization policy. For manual recovery, open the confirmed group's settings as
this environment's Misty owner, check **Allow external members** and **Who can add
external members**, then retry. Settings links use the stored group email/domain,
not a renamed team's slug. Misty admin/team roles do not confer Google ownership.
Manual changes do not bypass the backend's settings verification.

Use `POST /teams/{team_id}/google-group/sync` with a `teams:write` key or
`team-tracking-groups sync --team <slug>`. The companion bot flow in
[PR #279](https://github.com/UTMIST/Misty/pull/279) exposes `/team google-status`
and `/team google-sync` for the same state and retry path. Generic service 503s
are temporary outages; only the explicit `google groups not configured` response
means the integration is disabled.

### Rollout and fresh-group acceptance

1. Enable the APIs and authorize both scopes above. Deploy the backend and any
   existing sync cron on the same version before rolling out the bot guidance.
   No schema migration or additional runtime variable is needed for settings.
2. In staging, create one disposable team with no document shares. Confirm its
   recorded group identity and both settings through the Settings API, then add
   an approved external test member and verify membership. No manual group toggle
   should be needed. The empty team's successful sync verifies settings, but does
   not by itself prove that an actual external member can be added.
3. Retry `/team google-sync`; confirm it reuses the same group. End the test
   membership, sync, and verify removal. Check the bot's create/status/retry replies
   against the resulting mapping. Retire the disposable team afterward; normal
   retirement empties its group and preserves the directory history.
4. Run a targeted sync for each existing managed team before treating older stored
   `synced` results as settings-verified. Review mappings before a global backfill:
   it creates missing groups. Daily cron activation remains a separate operation.

Offline tests cover authorization failures, lost responses, settings read-back,
removals despite settings failure, and pending-write recovery. Record live
acceptance in the rollout PR or issue, including the deployed revision, configured
identity, external-member addition/removal and repeat-sync results. Exercise the
Discord commands in the target environment as well as the Google/API path;
an in-memory directory or local HTTP check does not verify the deployed bot.

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

## Registry cleanup (migration `011`)

Deletes the retired provider's current-state identity rows before removing its registry row.
Downgrade restores only the registry entry; it does not undo the data cleanup.
Use a database backup to recover the previous assignments.
