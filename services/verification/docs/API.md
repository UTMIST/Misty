# verification — API reference

Base URL (local): `http://localhost:8003` · Swagger UI: `/docs` · Schema: `/openapi.json`

| Method | Path | Scope | Success |
|---|---|---|---|
| POST | `/verification/request-code` | `verification:write` | **202 Accepted** |
| POST | `/verification/confirm-code` | `verification:write` | **200 OK** |
| GET | `/health` | — | 200 |

## Authentication

```
X-API-Key: <the API_KEY env value>
```

verification is **env-key-only**. There is no key-issuing CLI and no DB-backed keys — `NullApiKeyStore` short-circuits key lookups, so the only accepted credential is the bootstrap `API_KEY`, which carries the `admin` scope. The `vf_` envelope is reserved but unused, so **the env key must be a plain opaque string, not shaped like `vf_<prefix>_<secret>`.**

Both endpoints are gated by `require_scope("verification:write")`; `admin` satisfies it. `/health` is unauthenticated.

`dev:spoof` is disabled.

> `build_auth` is configured with `bootstrap_honors_x_actor=True`, but that setting only takes effect through the `get_actor` dependency — which this service exports and **never attaches to either route**. `AuditLogMiddleware` does no `X-Actor` handling of its own, so **sending `X-Actor` currently changes nothing.** Treat the audit actor as always being the key's own name.

## Field validation

Both request bodies set `extra="forbid"` — an unexpected field is a 422, not a silent no-op.

- **`email`** — must contain `@` and a dotted domain; normalized to trimmed-lowercase before storage and comparison.
- **`subject`** — trimmed, must be non-empty. Otherwise opaque: the service never parses it. Callers use shapes like `discord:12345` or `person:<uuid>`.

---

## `POST /verification/request-code`

Generate a one-time code, email it, and store its HMAC.

**Request** (`RequestCodeIn`):

| Field | Type | Notes |
|---|---|---|
| `subject` | string | Required, non-empty after trim |
| `email` | string | Required, validated + lowercased |

**Response** — `202 Accepted`:

```json
{"status": "sent"}
```

202 rather than 200 is accurate: the code has been handed to the mail provider, not confirmed delivered to the inbox.

**Example:**

```bash
curl -sS -X POST http://localhost:8003/verification/request-code \
  -H "X-API-Key: dev-api-key-change-me" -H "Content-Type: application/json" \
  -d '{"subject": "discord:12345", "email": "alex@example.com"}'
```

### Behavior

- **Rate limited per email.** If an unconsumed code for that *email* was created within `RATE_LIMIT_WINDOW` (60 s), the request is rejected with **429 `rate_limited`**. Note the limit keys on email, not subject — it protects the mailbox regardless of how many subjects a caller invents.
- **Send before persist.** The email goes out first. On `EmailSendError` the endpoint returns **502 `email_send_failed`** and stores nothing, so the caller isn't rate-limited out of an immediate retry.
- **Supersede on reissue.** A new request for an existing subject atomically replaces that subject's row. Only the newest code is ever live.
- The code is 6 digits, generated with `secrets`, expires after `CODE_TTL` (10 min), and is stored only as an HMAC-SHA256 digest.

### Errors

| Condition | Status | Detail |
|---|---|---|
| Another code for this email within 60 s | 429 | `rate_limited` |
| Mail provider failed | 502 | `email_send_failed` |
| Invalid email / empty subject / extra field | 422 | Pydantic validation error |
| Missing/invalid `X-API-Key` | 401 | — |

---

## `POST /verification/confirm-code`

Submit the code the user typed back.

**Request** (`ConfirmCodeIn`):

| Field | Type | Notes |
|---|---|---|
| `subject` | string | Required — must match the one used at request time |
| `code` | string | Required — the 6 digits from the email |

**Response** — `200 OK`:

```json
{"verified": true, "subject": "discord:12345", "email": "alex@example.com"}
```

The returned `email` is the verified address. That's the payload the caller acts on — bind it to a person, complete the signup, etc.

**Example:**

```bash
curl -sS -X POST http://localhost:8003/verification/confirm-code \
  -H "X-API-Key: dev-api-key-change-me" -H "Content-Type: application/json" \
  -d '{"subject": "discord:12345", "code": "123456"}'
```

### The full state machine

Every outcome, in the order `confirm_code` evaluates them:

| State | Status | Detail |
|---|---|---|
| No row for this subject | 404 | `no_pending_code` |
| Consumed, past TTL | 404 | `no_pending_code` |
| **Consumed, within TTL** — evaluated in this order: | | |
| &nbsp;&nbsp;1. attempts ≥ 5 | 429 | `too_many_attempts` — **checked before the code is verified** |
| &nbsp;&nbsp;2. correct code | **200** | Idempotent replay — returns the same success |
| &nbsp;&nbsp;3. wrong code | 400 | `invalid_code` — attempt counted (429 if that attempt hits the cap) |
| **Unconsumed** — evaluated in this order: | | |
| &nbsp;&nbsp;1. past TTL | 410 | `expired` |
| &nbsp;&nbsp;2. attempts ≥ 5 | 429 | `too_many_attempts` |
| &nbsp;&nbsp;3. wrong code | 400 | `invalid_code` — attempt counted (429 if that attempt hits the cap) |
| &nbsp;&nbsp;4. correct code | **200** | Row marked consumed |

Note the precedence on both branches: **the attempt cap is checked before the code is verified.** Once a subject is locked out, submitting the correct code returns 429, not 200 — idempotent replay does not survive lockout. Request a new code.

Three things worth knowing as a caller:

1. **Retrying the correct code is safe** within the TTL *and under the attempt cap* — you get the same `{verified: true, ...}` back. Build retries freely, but don't burn attempts guessing first.
2. **A wrong code against a consumed subject is indistinguishable from a wrong code against a live one** — both 400 `invalid_code`, both count toward the same cap. No email is leaked to someone who never knew the code.
3. **410 vs 404.** `expired` means "there was a code, it timed out — request a new one." `no_pending_code` means "nothing to confirm here." A consumed-and-expired row gives 404: the window is closed, not merely stale.

### Errors

| Condition | Status | Detail |
|---|---|---|
| No pending code, or consumed and expired | 404 | `no_pending_code` |
| Wrong code | 400 | `invalid_code` |
| Expired (unconsumed) | 410 | `expired` |
| Attempt cap reached | 429 | `too_many_attempts` |
| Empty `subject`, or an extra field | 422 | Pydantic validation error |
| Missing/invalid `X-API-Key` | 401 | — |

> **An empty `code` is not a 422.** `ConfirmCodeIn.code` has no validator and no `min_length` — only `subject` is normalized and checked. `{"subject": "…", "code": ""}` validates, reaches the HMAC compare, returns **400 `invalid_code`**, and **burns an attempt**. Callers should reject empty input client-side rather than spending one of the five tries on it.

---

## `GET /health`

Unauthenticated liveness probe. Railway's healthcheck path.

```json
{"status": "ok"}
```

---

## Typical flow

```
caller                          verification                mail provider
  │  POST /request-code             │                            │
  │  {subject, email}               │                            │
  ├────────────────────────────────►│  send code ───────────────►│
  │                                 │  then persist HMAC         │
  │◄──────── 202 {"status":"sent"} ─┤                            │
  │                                                              │
  │                          (user reads the code) ◄─────────────┘
  │                                 │
  │  POST /confirm-code             │
  │  {subject, code}                │
  ├────────────────────────────────►│  constant-time HMAC compare
  │◄──── 200 {verified, subject, email}
  │
  └─► caller binds the proven email to an identity (team-tracking's job, not this service's)
```

## What this service does not do

- **No identity binding.** It proves reachability; associating the email with a directory person is the caller's job.
- **No listing or pagination.** There is no endpoint to enumerate codes. Rows are looked up by subject or email internally only.
- **No resend endpoint.** Call `request-code` again — it supersedes the previous code, subject to the 60 s per-email window.
- **No per-IP limiting or backoff** beyond the 60 s window and the 5-attempt cap.
