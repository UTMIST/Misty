# Deploy the external gateway

Deploy to staging first, using the [platform branch flow](../../../docs/RAILWAY-DEPLOYMENT.md#branching--auto-deploy-model).
The steps below provision a new service; the repository alone does not create it.

## Database and internal access

1. Provision a separate Neon project for `gateway`, with independent staging and
   production branches. It holds only external API keys. Use the matching branch's
   `postgresql+psycopg://` URL with `sslmode=require` as `DATABASE_URL`.
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
an unavailable or unmigrated gateway key database fails readiness.
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
ID needed to revoke and never shows key material. Revocation takes effect on the
next request and preserves the original revocation audit metadata on repeat calls.
To rotate, issue a new uniquely named key, update the consumer, then revoke the old
key. The CLI rejects missing scopes, unknown scopes, and `admin`.

The compute platform should receive its own key when its first operation is
defined. The currently available `resolve:discord` scope grants only the resolver;
see [future compute access](ARCHITECTURE.md#future-compute-platform-access).

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
