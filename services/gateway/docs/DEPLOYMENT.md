# Deploy the external gateway

Deploy to staging first, using the [platform branch flow](../../../docs/RAILWAY-DEPLOYMENT.md#branching--auto-deploy-model).
The steps below provision a new service; the repository alone does not create it.

## Database and internal access

1. Provision a separate Neon project for `gateway`, with independent staging and
   production branches. It holds external API keys, login challenges, and abuse counters. Use the matching branch's
   `postgresql+psycopg://` URL with `sslmode=require` as `DATABASE_URL`.
   Keep any URL-encoded credentials as supplied (for example `%40` for `@`).
   The migration runner handles Alembic's interpolation escaping internally.
2. Issue a team-tracking key against the matching environment's directory DB.
   With that service's `DATABASE_URL` loaded securely, run from the repo root:

   ```bash
   uv --directory services/team-tracking --project . run python -m src.cli issue \
     --name gateway --scopes identifiers:read
   ```

   Capture the one-time `tt_` key as the gateway's `DIRECTORY_API_KEY` in Railway.
   Use a separate key in each environment. A duplicate name means the key already
   exists; reuse the securely stored value or rotate it deliberately.

## Railway service

Add `gateway` to the existing Misty project. Configure each environment:

| Setting | Value |
|---|---|
| Source branch | `staging` for staging; `main` for production |
| Root Directory | `/` |
| Railway Config File | `/services/gateway/railway.json` |
| `DATABASE_URL` | Gateway's own Neon branch URL |
| `DIRECTORY_BASE_URL` | `http://${{team-tracking.RAILWAY_PRIVATE_DOMAIN}}:${{team-tracking.PORT}}` |
| `DIRECTORY_API_KEY` | Issued internal `tt_` key above |
| `GATEWAY_ENV` | `staging` or `production` |
| `PORT` | `8000` |
| `TRUST_PROXY_HEADERS` | `true` behind Railway's public edge, which overwrites `X-Real-IP` |
| Replicas / workers | One replica, one Uvicorn worker |

Ensure team-tracking explicitly sets `PORT=8000` so the variable reference resolves.
Deploy in the same region as the directory. `railway.json` runs migrations before
starting and checks `/health/ready`. Empty/default internal keys fail startup;
an unavailable or unmigrated gateway database fails readiness.
The image runs the API, migrations, and key CLI as the unprivileged `gateway` user.

Generate a public HTTPS domain for **gateway only**, targeting port 8000. Private
services retain private networking. Enable proxy-header trust only for ingress
that overwrites `X-Real-IP`. The limiter ignores `X-Forwarded-For`; private-network
requests bypass Railway's header sanitization. See [proxy trust](ARCHITECTURE.md).
Do not configure `API_KEY`: it is ignored.

The local commands pin the service directory and invoke its Python module because
workspace console scripts share the `src.cli` import name. In the isolated deployed
container, `gateway-keys` and `team-tracking-keys` work directly.

## External consumer keys

No current integration consumes the resolver. For a deployment check, use a
temporary test consumer and revoke its key afterward. When integrating a real
consumer, replace `example-consumer` with its name. With the gateway environment's
`DATABASE_URL` loaded securely, use these key-management commands (revoke only
after verification):

```bash
uv --directory services/gateway --project . run python -m src.cli issue \
  --name example-consumer --scopes resolve:discord
uv --directory services/gateway --project . run python -m src.cli list --active-only
uv --directory services/gateway --project . run python -m src.cli revoke <key-id>
```

Store the one-time `gw_` token in the consumer's secret store. `list` includes the
ID needed to revoke plus `created_at`, `updated_at`, `created_by`, and `updated_by`;
it never shows key material. To record the operator, put `--actor <operator-name>`
before `issue` or `revoke`; otherwise the actor is `cli`. This is operator-supplied
metadata for a CLI that already requires direct database access, not HTTP identity.
Revocation takes effect on the
next request and preserves the original revocation audit metadata on repeat calls.
To rotate, issue a new uniquely named key, update the consumer, then revoke the old
key. The CLI rejects missing scopes, unknown scopes, and `admin`.

The compute backend receives `auth:email` and `auth:status` for email login.
`resolve:discord` remains independent; never grant unused scopes.

## Verify the deployment

Check that the deployment reports success and that `/health/ready` returns 200.
Readiness caches successes and failures for five seconds; retry after that window
following a migration or recovery. See the [health contract](API.md#health).
From a test client or the intended consumer, call the resolver with its external
key and a synthetic or approved linked login; expect only `discord_id`. Verify a
missing key returns 401 and an unknown login returns the generic 404. Confirm the
audit records the consumer name and status without response data or key material.

Readiness verifies only the gateway DB. A resolver 503 means the private URL,
internal key, directory availability, or upstream response needs investigation.
Never downgrade a live key database as a deployment check; the migration round trip
belongs to disposable test databases only.

## Enable email login

The feature is off by default; existing resolver deployments continue to work.
Use the same environment for all three services and keep verification private.
Set `GATEWAY_ENV=staging` or `production` explicitly: successful auth responses
attest this value, and compute requires the matching `MISTY_ENVIRONMENT`.
Use separate gateway keys and compute databases for staging and production.
Set `LOGIN_STATUS_PER_MINUTE` (default 600) and `EDGE_REQUESTS_PER_MINUTE`
(default 1200) for expected status refreshes and total traffic from each backend
IP. Keep at least 120 requests/minute of edge headroom above the status budget.
The default status budget accommodates roughly 300 continuously active members
with a 30-second cache; allow margin for issuer checks and monitor upstream load.
See [the capacity contract](API.md#limits-and-failures).

1. Deploy verification with the cooldown `Retry-After` response before rolling
   out the gateway retry. Older verification deployments are safe but cannot
   recover a deferred send. Provision a dedicated `vf_` consumer key named `gateway`, scoped
   only `verification:write`, using its [provisioning guide](../../verification/docs/DEPLOYMENT.md#scoped-consumer-keys).
   Keep the bot's existing bootstrap credential until it is separately migrated.
2. Issue a new team-tracking key with `people:read` and `identifiers:read`, using a
   fresh name (for example `gateway-email-login`). Update `DIRECTORY_API_KEY` and
   revoke the replaced key after verification. Never broaden an external key into
   a team-tracking key or pass it to compute.
3. Apply gateway migration **002** via the normal pre-deploy step. It adds two
   tables without changing existing external keys. Readiness checks all tables.
4. Set `VERIFICATION_BASE_URL=http://${{verification.RAILWAY_PRIVATE_DOMAIN}}:${{verification.PORT}}`
   and set gateway `VERIFICATION_API_KEY` to the dedicated plaintext `vf_` key.
   Ensure verification explicitly sets `PORT=8000`. Then set `EMAIL_LOGIN_ENABLED=true`.
   Non-local startup rejects blank/dev verification keys when enabled.
5. Issue the compute backend's external key:

   ```bash
   uv --directory services/gateway --project . run python -m src.cli issue \
     --name compute --scopes auth:email auth:status
   ```

6. Store the `gw_` key only in compute's backend secret store. Implement the
   [compute integration responsibilities](API.md#compute-integration-responsibilities),
   including an authenticated `/v1/auth/issuer` startup check and durable issuer/origin
   binding, explicit UUID mappings for existing accounts, trusted client-IP
   derivation, browser-session challenge binding,
   secure sessions, and fail-closed active-status checks. Keep one gateway replica
   and worker. Login counters are durable; the edge flood guard still is not.
7. Verify with an approved active test member: request a code, confirm once,
   reject replay, retire/reactivate the test member and check status. Verify that
   inactive/unknown emails deliver no code and that a resolver-only key is refused.
   Do not log codes or enable an inbox endpoint. The local Docker smoke check uses
   a fake sender and a deterministic generator only inside its disposable container.

Monitor static `email login delivery failed` messages and confirm/status 503s.
Generic start responses deliberately cannot diagnose mail failures. A timed-out
send is not retried automatically. Cooldown and delivery failures retain quotas;
replacement failure leaves the previous code usable until expiry.

The verification table gets a new opaque subject per delivered login. Schedule
its documented retention cleanup before enabling sustained traffic; gateway
pruning does not delete another service's rows. Gateway prunes expired login
records/counters on authenticated auth requests, so idle environments retain them
until activity resumes.

To disable new authentication, set `EMAIL_LOGIN_ENABLED=false` and redeploy; all
auth routes then fail closed. Revoke the compute key for immediate API access
removal. Compute must end/reject its own sessions when status checks fail. Do not
reset counters during routine deploys. For rollback, disable login and stop
in-flight requests first; migration002 can be downgraded on a backup-protected
maintenance window, discarding pending challenges/counters but preserving keys.
Do not downgrade a live database merely to test reversibility.
