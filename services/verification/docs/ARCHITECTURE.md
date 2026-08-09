# verification — architecture

Why this service is shaped the way it is. For *what it does* and how to run it, see the [README](../README.md); for *how to change it*, see [CONTRIBUTING.md](CONTRIBUTING.md).

## The scope boundary

verification answers exactly one question: **does whoever holds this `subject` control this email address?**

It does not answer, and must never learn to answer:

- *Who* that subject is. `subject` is an opaque string — `discord:12345`, `person:<uuid>`, a pending-signup token. The service never parses it.
- What to *do* with the proof. Binding a verified email to a directory person is team-tracking's job; the bot orchestrates the two.

That narrowness is the design. The service holds no identity data, so a compromise leaks at most "this email was recently proven for this opaque subject" — not the org's identity graph.

## Layering

```
contracts/types.py       Pydantic DTOs (RequestCodeIn/Out, ConfirmCodeIn/Out) + VerificationCode
contracts/storage.py     VerificationStore Protocol
       ▲
src/api/                 FastAPI — routing, auth, the confirm state machine
       │  depends on
       ▼
src/storage/             InMemoryVerificationStore | PostgresVerificationStore
src/email/               FakeSender | ResendSender | GmailSender  (EmailSender Protocol)
src/codes.py             generate / hash / verify — HMAC-SHA256, constant-time
src/policy.py            the four numbers that define the security posture
```

`contracts/` imports nothing from `src/`. Storage and email adapters know nothing about FastAPI. `src/api/deps.py` is the single wiring point, which is what lets tests swap both via `app.dependency_overrides`.

## Policy lives in one file

`src/policy.py` holds four constants, and they *are* the security posture:

| Constant | Value | What it bounds |
|---|---|---|
| `CODE_LENGTH` | 6 | Search space: 10⁶ |
| `CODE_TTL` | 10 min | How long a proof window stays open |
| `RATE_LIMIT_WINDOW` | 60 s | Minimum spacing between codes to one email |
| `MAX_ATTEMPTS` | 5 | Guesses before lockout |

They're centralized because they only make sense together. A 6-digit code is only safe *because* of the 5-attempt cap — 5 guesses against 10⁶ is a 1-in-200,000 shot. Raising `MAX_ATTEMPTS` without lengthening the code silently weakens the whole thing. Changing any of these is a security decision, not a tuning knob.

## The code is never stored

`generate_code()` uses `secrets`. Only an **HMAC-SHA256 digest** under `CODE_HMAC_SECRET` is persisted; verification is a constant-time compare. The plaintext exists in exactly two places: the outbound email, and process memory for the duration of one request.

HMAC rather than a plain hash matters here: the search space is only 10⁶, so an unkeyed digest of a 6-digit code is trivially rainbow-tabled from a database dump. The keyed construction means a dump without `CODE_HMAC_SECRET` is useless.

**Rotating `CODE_HMAC_SECRET` invalidates every in-flight code.** Given a 10-minute TTL that's a 10-minute blast radius, which is why it's safe to rotate freely.

## Send before persist

`request-code` emails the code **before** writing the row. If delivery fails, the endpoint returns 502 and stores nothing.

The ordering is deliberate. Persist-then-send would leave two bad states on a mail failure: an unsendable code sitting in the table, and — worse — the user rate-limited out of an immediate retry by a code they never received. Send-first means a failed send is a clean no-op the user can retry at once.

The cost is a genuine (small) race: mail delivered, then the write fails. The user gets a code that was never stored and sees `no_pending_code` on confirm. That's the better failure — visible, retryable, and it doesn't lock anyone out.

## One live code per subject

`verification_codes.subject` is **unique**. A fresh request for an existing subject replaces that subject's row via `INSERT ... ON CONFLICT (subject) DO UPDATE` — an atomic upsert, safe under concurrent `request-code` calls.

So only the newest code is ever live: reissuing invalidates the previous one implicitly, with no cleanup pass and no way for two valid codes to coexist for one subject.

Note the asymmetry: the **rate limit is keyed by email**, the **row is keyed by subject**. That's on purpose — the limit protects the *mailbox* from being flooded across whatever subjects an attacker invents, while the row models "the one proof in flight for this subject."

## The confirm state machine

The most security-sensitive code in the platform. Every branch of `confirm_code` exists for a reason:

| State | Result |
|---|---|
| No row for subject | **404** `no_pending_code` |
| Consumed, past TTL | **404** `no_pending_code` |
| Consumed, within TTL → attempts ≥ max | **429** `too_many_attempts` |
| Consumed, within TTL → correct code | **200** `{verified, subject, email}` — idempotent replay |
| Consumed, within TTL → wrong code | **400** `invalid_code`, attempt counted |
| Unconsumed → past TTL | **410** `expired` |
| Unconsumed → attempts ≥ max | **429** `too_many_attempts` |
| Unconsumed → wrong code | **400** `invalid_code`, attempt counted |
| Unconsumed → correct code | **200**, row marked consumed |

The arrows are evaluation order, and it matters: **on both branches the attempt cap is checked before the code is verified.** A locked-out subject returns 429 even for the correct code — idempotent replay does not survive lockout. That's deliberate; the alternative would let an attacker who has exhausted the cap keep testing guesses and learn from a 200.

Two further subtleties worth understanding before touching it:

**Idempotent replay is gated on the code still matching.** The obvious implementation — "if consumed and unexpired, return success" — would let *any* holder of the `verification:write` scope replay an arbitrary subject and harvest its verified email without ever knowing the code. So the consumed branch re-verifies, and a wrong code there returns the same `400 invalid_code` as the normal path. No email leaks.

**The consumed branch enforces the same attempt cap.** Otherwise it would be an unlimited brute-force oracle against a consumed record — the attacker would just wait for a legitimate verification to complete, then grind the 10⁶ space against it.

**410 vs 404 is a real distinction.** `expired` (410) means "there was a code, it timed out, ask for a new one." `no_pending_code` (404) means "nothing to confirm." A consumed-and-expired row returns 404 rather than 410 because the proof window is closed, not merely stale.

## Atomic increments

`increment_attempts` is a single `UPDATE ... RETURNING attempts` — an atomic read-and-increment, not a read-then-write.

This is not a micro-optimization. With a read-modify-write, N parallel invalid confirms all read the same count and all write count+1, so the counter advances by 1 instead of N and the lockout never trips. The attempt cap *is* the security control; a lost-update race defeats it entirely.

Two caveats worth knowing rather than assuming:

- **No test actually exercises concurrency.** The increment tests in both adapter suites are sequential, so a read-modify-write regression in `postgres.py` would pass them. The atomicity here rests on the single `UPDATE ... RETURNING` statement, not on test coverage — which is exactly why the statement shape matters and shouldn't be "simplified".
- **`InMemoryVerificationStore` is a read → `model_copy` → write**, i.e. the shape this section calls disqualifying. That's acceptable because it backs single-threaded tests, never production — but don't read it as the reference implementation of this invariant. `postgres.py` is.

## Auth: env-key-only, on purpose

verification is the one service with a database but **no key table**. `NullApiKeyStore` short-circuits every key lookup, so only the bootstrap `API_KEY` authenticates. The `vf_` envelope is reserved but unused — which means the env key must be a plain opaque string, *not* shaped like that envelope.

The reasoning: there is exactly one consumer (the Discord bot). Issuing per-consumer keys would be ceremony without benefit — a CLI, a table, a migration, and a revoke path, all to manage one key. If a second consumer ever appears, a real `ApiKeyStore` drops in behind the same protocol with no route changes.

## Email backends

Three implementations behind the `EmailSender` Protocol, chosen by `EMAIL_BACKEND` and wired in `deps.py`. Any provider failure normalizes to `EmailSendError` → a clean 502.

- **`fake`** (default) — captures in memory. Used by tests and local dev. **Refused outside `local`** by the boot check, because a backend that silently drops mail would 202 every request while delivering nothing.
- **`resend`** — Resend HTTP API. Plain HTTPS, works on any Railway plan (no SMTP ports needed).
- **`gmail`** — Gmail API via a service account with domain-wide delegation, impersonating `GMAIL_SENDER`. The Google client is imported lazily so the dependency tree never loads unless it's the selected backend.

## Boot-time guard

`verify_production_secrets()` refuses to start a non-`local` deploy holding any of: the dev `API_KEY`, the dev `CODE_HMAC_SECRET`, a `dev_password@localhost` `DATABASE_URL`, `EMAIL_BACKEND=fake`, or a real backend missing its credentials.

The failure mode this prevents is the nastiest kind: a service that looks perfectly healthy — 202 on every `request-code`, green healthcheck — while no mail is delivered and every confirm 404s. Better to not start.
