# Gateway architecture

External consumers call the gateway with per-consumer keys. The gateway calls
private services over authenticated HTTP and returns explicitly selected fields.
Each private service remains responsible for its own database and business rules.
See the platform's [access architecture](../../../docs/ARCHITECTURE.md#access-architecture-two-doors-not-one-gateway).

The gateway's Postgres database contains its external key registry, short-lived
login challenges, and abuse counters. Its outbound `DIRECTORY_API_KEY` needs
`identifiers:read` for resolution and `people:read` for email login/status.
Inbound keys use `gw_`; the internal key uses `tt_`. The shared auth library
verifies the envelope, hash, revocation state, and scope. No environment bootstrap
key is accepted, and the CLI allows only explicitly supported external scopes.

The resolver path is audit middleware → IP limit → authentication → consumer quota
→ endpoint scope → upstream client → curated response. Login adds a bounded body
reader and no-store headers, then scope checks and durable login quotas. `deps.py` supplies the
pooled HTTP client through the `DirectoryClient` Protocol. Each app lifespan owns
its client, constructed lazily after scope checks under a lock so concurrent first
requests share one pool. Shutdown closes that pool after requests drain; an unused
app creates no client, and separate apps never share or close each other's pool.
JSON and response-shape
failures become `DirectoryUnavailable`, mapped to a static 503 without upstream
content. Authentication failures never construct the directory dependency.

The rate-limit counter synchronizes concurrent worker threads, has bounded
capacity, and lives in one process. Run one replica and one worker; a shared
counter is required before horizontal scaling. At capacity it evicts the oldest
window, which can reset that identity's quota under pressure. These limits are
application safeguards, not a replacement for edge traffic protection.

When `TRUST_PROXY_HEADERS=true`, the IP limiter validates and normalizes one
`X-Real-IP` address. Railway's public edge overwrites this header; its
`X-Forwarded-For` chain can include additional proxy hops and is ignored. Missing,
invalid, or duplicated `X-Real-IP` falls back to the socket peer. Enable trust only
for ingress that overwrites client-supplied headers, not private-network callers.
See [Railway's public networking specifications](https://docs.railway.com/networking/public-networking/specs-and-limits#technical-specifications)
and [its proxy-header clarification](https://station.railway.com/questions/need-authoritative-client-ip-proxy-tru-090eac86).

Readiness has a per-app cache that bounds database work independently of client
identity or request rate. It caches both outcomes for five seconds after a probe
completes. A nonblocking lock permits at most one database probe at a time;
concurrent requests use an unexpired result or receive 503 without waiting.
This keeps probes accessible after quota exhaustion without letting a public
readiness flood occupy the database pool or fill worker threads with lock waiters.
The [health contract](API.md#health) describes the resulting detection delay.

## Email login ownership and concurrency

The [email-login contract](API.md#email-login) implements compute's sign-in
requirements. The gateway owns eligibility orchestration and app-bound challenges;
verification owns code generation, HMAC storage, email delivery, and code checking.
Directory records remain in team-tracking. Communication is scoped private HTTP.

A start atomically reserves mailbox quotas and a pending challenge before any
lookup or delivery. A background task looks up the active primary email, requests
verification for the opaque `gateway-login:<challenge UUID>` subject, then binds
the original person ID. Background failures log only a static message; they never
change the already-sent generic response. Tasks are not a durable delivery queue.
On process loss, the user retries after the cooldown; budgets are retained and no
ambiguous send is retried automatically. Monitor delivery-failure logs.

Before confirmation reaches verification, the gateway atomically reserves a
challenge attempt. Three attempts per challenge and five deliveries per mailbox
window bound guesses against codes issued in that window to fifteen; no shared
guess counter lets an attacker block a different challenge. New delivery does
not invalidate an earlier challenge. It then rechecks the original person's
active flag and email. A final compare-and-set permits exactly one redemption.
Private verification's existing correct-code replay remains available for the
Discord flow; the public login boundary is strictly single-use. There is no
cross-service transaction with directory changes: status checks on compute requests
close the eligibility gap after authentication.

Both adapters implement the same storage protocol. Postgres serializes short
login-state transactions with a transaction-scoped advisory lock, never held during
HTTP. This intentionally simple design fits the one-worker, low-volume deployment.
Expiry indexes support pruning on each authenticated login/status request. Gateway
challenges retain email/UUID until expiry and the next request; rate buckets retain
hashes of email/IP (pseudonymous, not anonymous) for at most their one-day window
and the next request. Verification retains separate HMAC/email rows; follow its
[data-retention procedure](../../verification/docs/DEPLOYMENT.md#data-retention).

The authenticated compute backend attests `client_ip`; it must derive that value
from trusted ingress. This does not alter edge `X-Real-IP` handling. Mailbox and
application quotas do not depend on the attestation. Keep the existing one-replica,
one-worker limit because the pre-authentication flood guard and resolver quotas
are still process-local. Persistent login quotas alone do not authorize scaling.

Successful auth responses attest `misty:<GATEWAY_ENV>`. The authenticated issuer
endpoint supports compute's startup check; compute also checks each successful
response and durably pins issuer plus gateway origin to its database. Explicit
operator mappings are required before existing compute accounts can be claimed;
email equality alone does not establish continuity of account ownership.

## Future compute-platform access

Compute receives only the scopes it uses (`auth:email`, `auth:status`). Login
proves identity and current eligibility, not compute roles or general directory
access. Additional data access still requires explicit operations and permissions,
following the [contribution guide](CONTRIBUTING.md). Never expose database access
or allow a claimed user identity to bypass a private service's authorization.
