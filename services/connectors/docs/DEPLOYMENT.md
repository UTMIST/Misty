# connectors — deployment

Service-specific deploy notes. The full platform runbook — creating Railway services, wiring variables, promoting staging → production — is [`docs/RAILWAY-DEPLOYMENT.md`](../../../docs/RAILWAY-DEPLOYMENT.md). This page covers only what's different about connectors.

## Shape

| | |
|---|---|
| **Database** | None. No Alembic, no `preDeployCommand`. |
| **Builder** | Dockerfile, build context = **repo root** (needs the uv workspace + `packages/auth`) |
| **Install** | `uv sync --frozen --no-dev --package connectors` |
| **Healthcheck** | `/health` |
| **Port** | Binds Railway's injected `${PORT}`; 8005 locally |
| **Exposure** | Private. Reached as `connectors.railway.internal:${PORT}`. |
| **State** | Stateless. Safe to scale horizontally and to restart at any time. |

Because there is no database and no state, a redeploy is a plain restart — nothing to migrate, nothing in flight to drain.

## Variables

| Var | Staging / production | Notes |
|---|---|---|
| `CONNECTORS_ENV` | `staging` / `production` | Anything but `local` turns on the boot check |
| `API_KEY` | a real random string | **Must** differ from `dev-api-key-change-me` or the app refuses to boot |
| `CONSUMER_KEYS` | JSON array | See below. Malformed → boot failure. |
| `GOOGLE_CREDENTIALS_JSON` | base64 of the service-account JSON | May be empty — see [Deploying before Google exists](#deploying-before-google-exists) |
| `MAX_CONTENT_CHARS` | leave at default | Keep **above** documentation-system's (1,000,000) |
| `REQUEST_TIMEOUT_S` | leave at default (30) | Per-request timeout to Google |

### What the boot check does and does not cover

`verify_production_secrets()` runs in `create_app()` outside `local` and refuses to start unless `API_KEY` is overridden. `CONSUMER_KEYS` is separately validated eagerly — `get_key_store()` is called in `create_app()`, so a malformed array kills the container at startup rather than on the first `/fetch`.

`GOOGLE_CREDENTIALS_JSON` is **deliberately excluded** from that check. See below.

## Consumer keys

connectors has no `api_keys` table. Keys live in the `CONSUMER_KEYS` env var, and the CLI only *prints* — it cannot write to any store:

```bash
uv --project services/connectors run connectors-keys \
  --name documentation-system --scopes fetch
```

- **stdout** — the plaintext key, shown **once**. Set it as documentation-system's `CONNECTORS_API_KEY`.
- **stderr** — the JSON object. Append it to connectors' `CONSUMER_KEYS` array.

Then redeploy connectors. **Revoking is the same operation in reverse**: drop the entry from `CONSUMER_KEYS` and redeploy. There is no revoke command, because there is no database.

> Always pass `--project`. Every service declares a top-level `src` package, so in the shared workspace venv a bare invocation can resolve the wrong service's CLI and mint a key with the wrong envelope prefix. (The entry points differ — `src.mint_key:main` here, `src.cli:main` in team-tracking and documentation-system — but the `src` collision is what bites.) Verify the token starts with `connectors_`.

## Deploy order

connectors is a **soft** dependency of documentation-system. Deploying it first is recommended but not required:

- documentation-system boots fine with `CONNECTORS_API_KEY` unset and `CONNECTORS_BASE_URL` unset.
- Google-source fetches then degrade to a warning on the ingested doc; the catalog itself works normally.

So the safe order is connectors → documentation-system, but getting it backwards costs you snapshot content on Google docs ingested in the gap, not a broken deploy.

## Deploying before Google exists

`GOOGLE_CREDENTIALS_JSON` empty is a supported production state. The service boots, `/health` is green, auth works, and `/fetch` returns **503** for Google sources.

This is intentional so connectors can be stood up and wired to its consumer before anyone has created the Google Cloud project. Turning Google on later is: create the project, enable the APIs, share the folders, set the variable, redeploy. No code change.

**Consequence for monitoring:** a green `/health` does not mean fetches work. If you want to know that Google access is live, exercise `/fetch` against a known-shared file — the healthcheck cannot tell you.

## Google service-account setup

The full runbook is in the [README](../README.md#google-service-account-setup-runbook). Summarized:

1. Create or reuse a Google Cloud project.
2. Enable **Drive, Docs, Slides, Sheets, and Forms** APIs on it.
3. Create a service account, then create and download a JSON key.
4. Share every Drive folder/file connectors must read with the service account's `…@….iam.gserviceaccount.com` address, as **Viewer**. Nothing is readable until explicitly shared — there is no folder allowlist in the service because Drive's sharing *is* the access control.
5. `base64 -i service-account.json | tr -d '\n'` → set as `GOOGLE_CREDENTIALS_JSON`.

If you add an extractor that needs a new Google API, both the API and its OAuth scope must be enabled/granted on that project — code alone won't do it. See [CONTRIBUTING.md](CONTRIBUTING.md#walkthrough-add-an-extractor-for-a-new-mime-type).

## Rollback

`git revert` + push. There is no schema to reverse and no state to reconcile — a connectors rollback is genuinely just the previous image.

## Troubleshooting

- **All fetches 503.** `GOOGLE_CREDENTIALS_JSON` is empty or not valid base64 of a service-account JSON. `SourceNotConfigured` covers both; check the variable decodes to JSON with a `private_key` field.
- **All fetches 403 for one file, fine for others.** That file isn't shared with the service account's address. This is Drive's sharing setting, not a connectors config.
- **Fetch 502 with a status in the log.** Upstream Google failure. Check whether the required API is enabled on the Cloud project — a disabled API surfaces as an upstream error, not as 503.
- **Container dies at boot with a `CONSUMER_KEYS` error.** It must be a JSON **array** (`[{...}]`), not a bare object or a comma-separated string. This is by design — fail at boot, not per request.
- **Container dies at boot complaining about `API_KEY`.** `CONNECTORS_ENV` is non-`local` and `API_KEY` is still the dev default.
- **`no discovery API version mapped for '<name>'`.** An extractor declared a service with no `_API_VERSIONS` entry. Code bug, not config — see CONTRIBUTING step 3.
