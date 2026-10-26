# verification — deployment

Service-specific deploy notes. The full platform runbook is [`docs/RAILWAY-DEPLOYMENT.md`](../../../docs/RAILWAY-DEPLOYMENT.md). This page covers only what's different about verification.

## Shape

| | |
|---|---|
| **Database** | Neon Postgres, its own project. Alembic head **001**. |
| **Pre-deploy** | `alembic upgrade head` as Railway's `preDeployCommand` — **not** the container `CMD` |
| **Builder** | Dockerfile, build context = **repo root** (needs the uv workspace + `packages/auth`) |
| **Install** | `uv sync --frozen --no-dev --package verification` |
| **Healthcheck** | `/health` |
| **Port** | Binds Railway's injected `${PORT}`; 8003 locally |
| **Exposure** | Private. Reached by the bot and gateway as `verification.railway.internal:${PORT}`. |
| **State** | One table of persistent rows containing short-lived codes. See [Data retention](#data-retention). |

## Variables

| Var | Staging / production | Notes |
|---|---|---|
| `VF_ENV` | `staging` / `production` | Anything but `local` turns on the boot guard |
| `DATABASE_URL` | Neon connection string | Must not be the `dev_password@localhost` default |
| `API_KEY` | a real random string | **Plain opaque string** — not shaped like `vf_<prefix>_<secret>` |
| `CONSUMER_KEYS` | optional scoped `vf_` entries | JSON with `name`, `prefix`, `key_hash`, `scopes`; validated at boot |
| `CODE_HMAC_SECRET` | a real random string | Must differ from the dev default |
| `EMAIL_BACKEND` | `resend` or `gmail` | **`fake` is refused outside `local`** |
| `EMAIL_FROM` | the sending address | Required by `resend` |
| `RESEND_API_KEY` | — | Required if `EMAIL_BACKEND=resend` |
| `GMAIL_SENDER`, `GMAIL_CREDENTIALS_JSON` | — | Required if `EMAIL_BACKEND=gmail` |

### The boot guard is unusually strict here

`verify_production_secrets()` refuses to start a non-`local` deploy holding **any** of:

- the dev `API_KEY`,
- the dev `CODE_HMAC_SECRET`,
- a `dev_password@localhost` `DATABASE_URL`,
- `EMAIL_BACKEND=fake`,
- a selected real backend missing its required credentials.

The `fake` check prevents a service from passing health checks and returning 202
for accepted requests while delivering no code to the user. The fake still
stores a code row, and confirmation follows the normal state machine; missing
delivery prevents normal user completion. Real-backend checks validate presence
of credentials, not their contents or provider access; see
[request-code errors](API.md#errors).

## Scoped consumer keys

Set `CONSUMER_KEYS` to a JSON array of `{name,prefix,key_hash,scopes}` entries.
For the gateway, generate a `vf_` token using the shared library's
`generate_key("vf_")` from the verification service environment. Store the returned
plaintext only in gateway `VERIFICATION_API_KEY`; add the returned prefix/hash
with `name: gateway` and `scopes: ["verification:write"]` to verification's array.
Follow the [shared config-backed key procedure](../../../packages/auth/README.md#key-store-wiring)
for the format. Use a secure operator script/secret store; do not paste token or
hash material into chat, logs, or source control.

There is no key database or CLI here. Rotation is: add a uniquely named second
entry, redeploy verification, update gateway, verify, then remove the old entry
and redeploy verification. This allows overlap. Keep keys separate per environment.

The existing bot bootstrap path remains: verification `API_KEY` equals the bot's
`VERIFICATION_API_KEY`, and the bot's base URL is
`http://${{verification.RAILWAY_PRIVATE_DOMAIN}}:${{verification.PORT}}`.
Do not give this bootstrap key to new consumers. Its rotation still requires
coordinated updates because it has no overlap window.

## Migrations

Schema is versioned with Alembic; `001_initial_schema` creates `verification_codes` plus the email index. Migrations run as Railway's **`preDeployCommand`** on every deploy — they are not in the container `CMD`, so a migration failure blocks the release rather than producing a half-started service.

To reverse a migration that shipped:

```bash
railway run --service verification uv run alembic downgrade -1
```

Then `git revert` the code change and push.

## Data retention

`verification_codes` holds one row per subject with a successfully stored
request, whether confirmed or not. Rows are superseded on reissue (unique
`subject` + upsert), so the table grows with distinct subjects that have had
stored requests. Code expiry does not remove the email/subject association.

**Nothing automatically prunes consumed or expired rows.** Gateway login uses a
new subject per delivered challenge, so schedule the cleanup below before sustained
external login traffic. There is no cleanup job. Each row holds an email address and an HMAC — no plaintext code — but it is still personal data sitting there indefinitely. If retention becomes a concern, a periodic `DELETE FROM verification_codes WHERE expires_at < now() - interval '30 days'` is safe: the confirm path treats a missing row and an expired-consumed row identically (both 404 `no_pending_code`).

`CODE_HMAC_SECRET` rotation invalidates every in-flight code. With a 10-minute TTL the blast radius is 10 minutes, so it's safe to rotate whenever you like — no coordination needed.

## Rollback

`git revert` + push. If a migration shipped with the change, `alembic downgrade -1` first (see above).

There is no in-flight state to worry about beyond codes issued in the last 10 minutes, which expire on their own.

## Troubleshooting

- **Container dies at boot.** Read the error — it names which guard tripped. Most often `EMAIL_BACKEND` is still `fake`, or `CODE_HMAC_SECRET`/`API_KEY` is still a dev default.
- **Every `request-code` returns 502 `email_send_failed`.** The mail provider is rejecting the send. For `resend`: check `RESEND_API_KEY` and that `EMAIL_FROM` is on a verified domain. For `gmail`: check domain-wide delegation is granted and `GMAIL_SENDER` is impersonable. Note that nothing is stored on this path, so users can retry immediately.
- **Codes are sent but every confirm 404s.** Check that the subject matches between the two calls. `subject` is opaque and compared exactly after trimming — `discord:12345` and `email:12345` differ, while trailing whitespace alone does not. Nothing else is normalized.
- **Users report "code expired" immediately.** Check clock skew on the instance; `expires_at` is computed server-side from `datetime.now(timezone.utc)`.
- **Legitimate users hitting 429 `rate_limited`.** The 60 s window keys on **email**, not subject. A user retrying `/link` quickly, or two subjects using the same address, will collide. This is working as designed — the window is in `src/policy.py` if it genuinely needs revisiting.
- **429 `too_many_attempts` on a fresh code.** The attempt counter is per row, and a reissue replaces the row — so this shouldn't survive a new `request-code`. If it does, the reissue didn't happen (check for a 429 `rate_limited` on that call instead).
- **Alembic reports an unknown revision.** You're pointed at the wrong database — almost always the documentation-system dev Postgres, since both bind host port **5434** locally.
