# Gateway API

The public data API is versioned under `/v1`. `/docs` serves Swagger and
`/openapi.json` is the generated contract.

## Discord resolver

`GET /v1/resolve/discord/{github_login}` requires an issued `X-API-Key` with
`resolve:discord`. The [README's resolver reference](../README.md#the-resolver-endpoint)
documents its one-field response, errors, rate limits, and case sensitivity.
The scope permits lookup of any linked GitHub login; it does not restrict a
consumer to a particular person or team.

There are no general person, membership, document, or database-query routes.
External identity headers do not grant delegated user access to private services.

## Health

| Route | Authentication | Behavior |
|---|---|---|
| `GET /health` | None | Process liveness; `200 {"status":"ok"}` even if a dependency is unavailable. |
| `GET /health/ready` | None | Checks all migrated gateway tables; caches either result for five seconds. Returns 200 when ready, otherwise `503 {"detail":"gateway database unavailable"}`. |

Both health routes are exempt from the IP limit. Readiness permits only one
database probe at a time per app, with at least five seconds between a completed
probe and the next one. Concurrent callers receive a valid cached result or 503
while an expired result is being refreshed; they never wait for the probe.
After a migration, outage, or recovery, allow the cache to expire before expecting
the response to change. `/health` does no database work.

Readiness checks only the gateway's own database, not team-tracking. A resolver
smoke request separately verifies the internal URL, internal key, and directory data.

## Email login

These are server-to-server routes for the compute backend. They require an issued
`gw_` key and `EMAIL_LOGIN_ENABLED=true`. Use `auth:email` for both POSTs and
`auth:status` for issuer and status checks. A resolver-only key cannot use them. No auto-signup
occurs. Only `Person.primary_email` is accepted (trimmed and lowercased); secondary
identifiers are not login addresses. Eligibility requires `active=true`.

All responses, including errors, use `Cache-Control: no-store`. Input models
reject extra fields. Login bodies are limited to 4096 bytes; errors never echo
submitted codes. Codes must be exactly six ASCII digits. Successful auth responses include an
`issuer` derived from `GATEWAY_ENV`: `misty:local`, `misty:staging`, or
`misty:production`. Consumers must check the expected issuer on every success.

### Issuer

`GET /v1/auth/issuer` returns `{"issuer":"misty:production"}` in production.
It requires `auth:status`, shares the status quota, and fails closed when login
is disabled. Compute checks this at startup and pins both issuer and gateway
origin to its database; staging and production need separate databases and keys.

### Start

`POST /v1/auth/email/start`

```json
{"email":"member@example.com","client_ip":"192.0.2.1"}
```

Returns **202**, with an opaque UUID `challenge_id`, `expires_in: 600`, and
`message: "If an active Misty account matches, a code will be sent."`, plus `issuer`.
The same shape is returned for unknown/inactive emails and mailbox throttling.
A throttled request returns a nonredeemable ID and leaves the previous challenge
usable. Every delivered challenge remains independently valid until its expiry,
attempt exhaustion, or redemption. A new request cannot invalidate another
browser's code; the UI must retain the corresponding request ID.

Eligibility lookup and code delivery run after the response is sent, to keep
account-dependent provider latency out of the public response. Delivery is best
effort: an upstream failure or restart may leave an unusable challenge. There is
no automatic resend after ambiguous delivery. An explicit verification cooldown
that guarantees no email was sent permits one deferred retry, at most 60 seconds
later, on the same challenge and reservation. Eligibility and expiry are checked
again before sending; waiting does not occupy an HTTP worker thread. Repeated
throttling or process loss can still prevent delivery. The user may request another code
after the cooldown; 202 means accepted, not proof of delivery or membership.

### Confirm

`POST /v1/auth/email/confirm`

```json
{"challenge_id":"<UUID from start>","code":"123456","client_ip":"192.0.2.1"}
```

On success, **200** returns only `{"person_id":"<Misty UUID>","email":"member@example.com","issuer":"misty:production"}`.
The code must belong to this challenge, and the authenticated application must
match the starter. Misty rechecks the original person UUID, active flag, and
primary email immediately before atomic redemption. Retiring the person,
changing the email, or reassigning it to a different person prevents login.
A challenge can succeed only once, including concurrent confirms. A lost success
response cannot be replayed: request a new code. Verification outages fail closed.

Unknown, pending, expired, exhausted, already-used, wrong-app, and
incorrect-code challenges all return **400** with `invalid or expired login challenge`.
Neither a person ID nor email is returned on failure.

### Current eligibility

`GET /v1/auth/people/{person_id}/status` returns **200** with
`{"active":true,"issuer":"misty:production"}` or the same shape with `active:false`. Missing and retired people both return
false. Directory failures return 503, never an assumed active result. This scope
permits status checks by UUID; it is not a public email lookup.

### Limits and failures

Login limits are persistent and atomic across concurrent requests and restarts.
Windows start on the first admitted request and expire after the duration below.

| Dimension | Limit |
|---|---|
| App start requests | 30/minute, 300/hour |
| App confirm requests | 60/minute |
| App status/issuer requests | `LOGIN_STATUS_PER_MINUTE` (default 600/minute), separate from login admission |
| Backend-attested client IP | 5 starts/minute, 30 confirms/minute, shared across apps |
| Mailbox sends | 1/minute, 5/hour, 20/day, shared across apps |
| Challenge guesses | 3, reserved before calling verification |
| Challenge lifetime | 10 minutes from start, including delivery time |

Five deliveries per mailbox window with three guesses each bound guesses against
the codes issued in that window to fifteen, across all apps. There is no shared
mailbox guess lockout: exhausting one challenge never spends another's budget.
Codes issued near a window boundary may remain valid into the next window for
the remainder of their ten-minute TTL. Send quotas still limit delivery abuse;
an attacker can exhaust new-send capacity, so edge abuse protection and monitoring
remain necessary. An already-delivered code remains usable through send throttling.

IPv4-mapped IPv6 addresses normalize to IPv4; native IPv6 addresses share a /64
bucket. Mailbox quotas include unknown/inactive addresses. Delivery failure does
not refund them. App/IP exhaustion returns **429** with `Retry-After: 60` (a retry
may still hit the hourly limit). Mailbox limits remain hidden behind generic
202/400 responses. Status requests have a separate app quota, but all endpoints
still share the pre-authentication edge-IP flood guard (`EDGE_REQUESTS_PER_MINUTE`,
default 1200/minute). Configure the two limits together: startup rejects edge
capacity below the status budget plus 120 requests/minute. With 30-second compute
status caching, the default status budget covers roughly 300 continuously active
identities; leave room for issuer checks and size shared-IP traffic separately.
This is a request budget, not a measured throughput guarantee. Increasing it does
not extend how long a retired member can retain access.

Missing/invalid key → 401; missing scope → 403; oversized body → 413;
invalid input → 422; disabled login or unavailable upstream → 503.

### Compute integration responsibilities

1. Keep the `gw_` key exclusively on the backend. Serve the login page over HTTPS.
   Never provide internal service keys or database credentials to compute.
2. Derive `client_ip` from the backend's trusted ingress/socket configuration;
   discard browser-supplied values and forwarded headers from untrusted peers.
   This is an attestation by the authenticated backend, not a gateway header
   override. Gateway edge-IP and app/mailbox limits still apply independently.
3. Bind each returned challenge to the initiating browser session, protect POSTs
   against CSRF, and forward the submitted code to confirm. Do not create a session
   based on the browser's claim that confirmation succeeded.
4. Explicitly map existing compute accounts to reviewed Misty UUIDs before
   cutover; email possession alone must not claim an existing account. On
   confirmed success, map the stable Misty UUID to the existing compute account
   and establish/rotate a server-side session with Secure, HttpOnly cookies. Keep
   existing ownership IDs for jobs/files. Do not automatically merge accounts from
   a browser-supplied email or silently grant compute roles/admin rights.
5. Check status on protected compute requests (or adopt a documented short cache
   window). Fail closed on false, 429, timeout, or 5xx. An active Misty identity is
   eligibility; compute still owns its authorization and resource permissions.

This is an email-code backchannel contract, not OIDC and not a bearer-token issuer.
There are no refresh tokens, public browser callbacks, or compute sessions here.
