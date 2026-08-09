# connectors

Thin, stateless HTTP API that fetches document content from external sources on behalf of UTMIST's internal consumers: one `POST /fetch` endpoint, scoped API-key auth, no database.

## What this service does

Consumers like documentation-system want the text of a Google Doc (or, later, other source types) without holding source credentials themselves. connectors is an **outbound adapter**: it owns the Google service-account key and the source-specific fetch/export logic, and returns plain text.

Two things this service deliberately is **not**:

- **Not a gateway.** It does not sit in front of other UTMIST services or route traffic to them.
- **Not an authorization boundary.** It authenticates the *caller* (via a scoped API key) but never learns *who the caller is fetching on behalf of* — there is no per-end-user identity in the request. Access control for the underlying document is whatever the source system enforces (for Google, Drive's own sharing settings on the service account's email). A consumer that needs to enforce "can this particular user see this doc" must do that itself before calling `/fetch`.

Like `llm`, it is stateless — no database, no migrations, no persisted key store. API keys are seeded from a config env var at boot.

## Quick start

Prerequisites: Python 3.11+, [uv](https://github.com/astral-sh/uv). No Docker or database needed for local dev; Google credentials are optional (see below).

```bash
# 0. From the repo root, enter the service directory (all commands below run here)
cd services/connectors

# 1. Copy environment config
cp .env.example .env

# 2. Install dependencies (including dev tools)
uv sync --extra dev

# 3. Start the API server
uv run uvicorn src.api.app:app --reload --port 8005
```

> The repo is a single [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/) (root `pyproject.toml` with `[tool.uv.workspace] members = ["services/*", "packages/*"]`, one root `uv.lock`). connectors depends on the shared `platform-auth` package (`[tool.uv.sources] platform-auth = { workspace = true }`) but the commands above are unchanged — `uv sync` and `uv run pytest` work exactly as shown when run from this directory.

The API is now at `http://localhost:8005`. Interactive Swagger UI is at `http://localhost:8005/docs`; the machine-readable schema is at `http://localhost:8005/openapi.json`.

The default dev bootstrap key is `dev-api-key-change-me` (set in `.env`, carries the `admin` wildcard scope). Pass it as `X-API-Key` on every request:

```bash
curl -sS http://localhost:8005/fetch \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"source_id": "gdocs", "url": "https://docs.google.com/document/d/<file-id>/edit"}' | python3 -m json.tool
```

**Ports:** this service runs on **8005** (team-tracking 8000, documentation-system 8001, llm 8002, verification 8003, meeting 8004) — chosen to avoid colliding when running the services locally.

### Environment tiers (`CONNECTORS_ENV`)

`CONNECTORS_ENV` declares which environment this instance represents: `local` (default), `staging`, or `production`. Outside `local`, `create_app()` calls `verify_production_secrets()` and **refuses to start** unless `API_KEY` is overridden from the dev default — a misconfigured deploy dies at boot, not on first request. `CONSUMER_KEYS` is also validated at boot (`get_key_store()` is called eagerly in `create_app()`), so a malformed value fails the container immediately rather than on the first `/fetch` call.

Google credentials are deliberately **not** part of that boot check: `GOOGLE_CREDENTIALS_JSON` may be empty in any tier. A connectors deploy with no Google account yet still starts, serves `/health`, and returns 503 for Google source fetches.

### Configuration

All settings load from the environment (`src/config.py`, `.env` in dev):

| Var | Default | Purpose |
|-----|---------|---------|
| `CONNECTORS_ENV` | `local` | Environment tier (`local` / `staging` / `production`). |
| `API_KEY` | `dev-api-key-change-me` | Env-bootstrap key; carries `admin` scope. Must be overridden outside `local`. |
| `CONSUMER_KEYS` | `""` | JSON array of per-consumer keys (see auth model). |
| `GOOGLE_CREDENTIALS_JSON` | `""` | Base64 of the Google service-account JSON key file. Empty is a valid running state — see the runbook below. |
| `MAX_CONTENT_CHARS` | `1200000` | Transport guard: truncates fetched content above this length. Deliberately set above documentation-system's own `MAX_CONTENT_CHARS` (1,000,000) so that clamp — not this one — is the one that actually trips and reports a truncation warning. |
| `REQUEST_TIMEOUT_S` | `30` | Per-request timeout to upstream sources (seconds). |

## Auth model

Auth is scoped API keys via the shared `platform_auth` package, matching `llm`, team-tracking, and documentation-system — but **DB-free**. There is no `api_keys` table.

- Every request needs `X-API-Key`. A key is either the shared bootstrap env key (`API_KEY`, scope: `admin`) or a per-consumer key seeded from the `CONSUMER_KEYS` env var.
- **`CONSUMER_KEYS` is a JSON array**, each entry `{"name", "prefix", "key_hash", "scopes"}`. At boot, the key store is built from it into an in-memory store that satisfies `platform_auth`'s `ApiKeyStore` protocol. Keys are stored only as Argon2 hashes; a malformed `CONSUMER_KEYS` fails fast at startup.
- Keys use the **`connectors_` envelope** (`connectors_<prefix>_<secret>`). There is no `X-Actor` header — this is a service-to-service API, so the audit actor is always the authenticated key's own name (attested actor).

### Minting consumer keys

Use the `connectors-keys` CLI. It prints the plaintext key **once** to stdout and the `CONSUMER_KEYS` JSON entry to stderr — it does not (and cannot) write to any store:

```bash
uv run connectors-keys --name documentation-system --scopes fetch
# stdout: connectors_<prefix>_<secret>   (the key — give it to the consumer, shown ONCE)
# stderr: {"name": "documentation-system", "prefix": "...", "key_hash": "$argon2id$...", "scopes": ["fetch"]}
```

Append the printed JSON object to the service's `CONSUMER_KEYS` array and redeploy. To **revoke** a key, drop its entry from `CONSUMER_KEYS` and redeploy — there is no revoke command because there is no database.

## API at a glance

Every endpoint except `/health` requires `X-API-Key`.

| Method | Path | Scope | Description |
|--------|------|-------|-------------|
| POST | `/fetch` | `fetch` | Fetch a document's text content from a source. |
| GET | `/health` | none | Liveness probe → `{"status": "ok"}`. |

`POST /fetch` requires the `fetch` scope (`require_scope("fetch")`); the `admin` wildcard satisfies it, so the bootstrap key and any admin key also work.

**Request body** (`FetchRequest`, `contracts/fetch.py`):

| Field | Type | Notes |
|-------|------|-------|
| `url` | string | Required, non-empty. The document URL. |
| `source_id` | string | Required, non-empty. Which source to use (e.g. `gdocs`, `gsheets`, `gslides`, `gdrive`). Supplied by the caller — the caller (e.g. documentation-system) already resolves this during ingest, so it is not re-derived here. |

`gdrive` covers everything that isn't a native Google Doc/Slides/Sheet: uploaded `text/*` files (via Drive export), and — per the extractor registered for the file's MIME type — uploaded PDFs, uploaded `.docx` files, and Google Forms.

| MIME type | Extractor | Output |
|-----------|-----------|--------|
| `application/vnd.google-apps.document` (Google Doc) | Docs (native API) | Markdown: headings, list bullets, tables |
| `application/vnd.google-apps.presentation` (Google Slides) | Slides (native API) | Markdown, one section per slide |
| `application/vnd.google-apps.spreadsheet` (Google Sheet) | Sheets (native API) | Markdown table per tab |
| `application/pdf` (uploaded PDF) | PDF (`pypdf`, local parse) | One `## Page N` section per page |
| `.docx` (uploaded, `application/vnd.openxmlformats-officedocument.wordprocessingml.document`) | Docx (`python-docx`, local parse) | Markdown mirroring the Docs extractor: headings, list bullets, tables |
| `application/vnd.google-apps.form` (Google Form) | Forms (native API) | Markdown: title, description, section headings, and each question with its multiple-choice options where present. **Responses are never fetched** — they duplicate the linked responses spreadsheet and are applicant personal data. |
| `text/*` (uploaded) | Drive export fallback | Raw text |

Legacy `.doc` (`application/msword`, the pre-2007 binary Word format) is a different format from `.docx` and stays unsupported — `python-docx` cannot read it.

**Response** (`FetchResponse`): `{ "title", "content", "warnings": [] }`.

### Error → status table

| Condition | Status |
|-----------|--------|
| `source_id` not registered | 422 |
| Source not configured (e.g. `GOOGLE_CREDENTIALS_JSON` empty, or malformed) | 503 |
| Source denied access to the file (not shared with the service account) | 403 |
| File not found for the given URL | 404 |
| File's MIME type has no text form the service can extract | 422 |
| Upstream source error (Drive/Docs API failure) | 502 |
| Missing/invalid `X-API-Key` | 401 |
| Valid key without the `fetch` scope | 403 |
| Malformed request body | 422 |

## Google service-account setup runbook

connectors reads Google Docs/Sheets/Slides/Forms/Drive files as a **service account** — a robot identity, not a user login. The account only sees files explicitly shared with it; there is no folder allowlist inside this service, because Drive's sharing settings *are* the access control.

1. **Create (or reuse) a Google Cloud project** for UTMIST connectors.
2. **Enable all five required APIs** on that project:
   - **Google Drive API** — always required. Used for file metadata (name, MIME type) on every fetch; as the export fallback for uploaded `text/*` files; and as the byte-download path for uploaded PDFs and `.docx` files, which are parsed locally rather than via a native Google API.
   - **Google Docs API** — required for the native Docs extractor.
   - **Google Slides API** — required for the native Slides extractor.
   - **Google Sheets API** — required for the native Sheets extractor.
   - **Google Forms API** — required for the native Forms extractor.
3. **Create a service account** in that project (IAM & Admin → Service Accounts).
4. **Create a JSON key** for the service account and download it. Treat this file as a secret — do not commit it.
5. **Share each Drive folder/file connectors needs to read** with the service account's email address (looks like `name@project-id.iam.gserviceaccount.com`), granting **Viewer** access. Nothing is readable until it's explicitly shared.
6. **Base64-encode the key file** and set it as `GOOGLE_CREDENTIALS_JSON`:
   ```bash
   base64 -i service-account.json | tr -d '\n'
   ```
   Paste the result into `GOOGLE_CREDENTIALS_JSON` (in `.env` locally, or as a Railway variable in staging/production).

### Scopes

The service account's credentials are built with these OAuth scopes:

- `https://www.googleapis.com/auth/drive.readonly` — always required (file metadata on every fetch; the Drive-export fallback for uploaded `text/*` files; and the byte-download path for uploaded PDFs and `.docx` files — see below).
- `https://www.googleapis.com/auth/documents.readonly` — required for the native Docs extractor.
- `https://www.googleapis.com/auth/presentations.readonly` — required for the native Slides extractor.
- `https://www.googleapis.com/auth/spreadsheets.readonly` — required for the native Sheets extractor.
- `https://www.googleapis.com/auth/forms.body.readonly` — required for the native Forms extractor.

**Uploaded PDFs and `.docx` files need no new scope.** Both download raw bytes through the same Drive media path already used for uploaded `text/*` files (`drive.readonly` covers it) and parse the file locally (`pypdf`, `python-docx`) — there is no separate PDF or Word API. This is the non-obvious part: a new *supported file type* does not automatically imply a new scope, only a new native Google API does.

Both `required_scopes()` and `required_services()` (`src/sources/google.py`) union across the registered extractors automatically — a new extractor just declares the scopes/services it needs, and those functions pick it up without being edited.

Adding a native extractor for an existing Google API (Docs/Slides/Sheets/Forms) is one new file plus one registry entry — true today only because `_API_VERSIONS` in `google.py` already maps that API's name to its discovery version. Wiring up a genuinely new Google API additionally needs one line added to `_API_VERSIONS`; `_build_services` raises `SourceNotConfigured` for any service name not in that map, so a missing entry fails loudly rather than silently. Forms was the first extractor to actually hit this: it added `"forms": "v1"` to `_API_VERSIONS` alongside its registry entry, so the caveat above is no longer hypothetical — it's checkable by diffing that one line.

### Known limitations

Two cases are lossy by design:

- **A spreadsheet tab larger than `MAX_ROWS_PER_TAB` (2000 rows).** Every tab of a spreadsheet is read (there is no first-tab-only limitation), but a tab over the cap is truncated to the first 2000 rows and a warning naming the tab and its real row count is added to the response's `warnings` list. Slides and Docs have no equivalent size cap.
- **A scanned PDF with no text layer.** `pypdf` extracts embedded text; it does not run OCR. If every page of a PDF yields no text (the telltale sign of a scanned-image-only document), the extractor emits the `## Page N` headers but no page content, plus a warning that no text layer was found. OCR is out of scope.

### Running without Google access

`GOOGLE_CREDENTIALS_JSON` empty is a valid, supported running state: `/fetch` returns **503** for any Google source, and every other part of the service (health check, auth, non-Google sources if added later) works normally. This means connectors can be deployed and wired up to consumers **before** the Google Cloud project and service account exist — flip on Google support later by setting the variable and redeploying, no code change required.

## Repo layout

```
connectors/
├── contracts/
│   └── fetch.py            Pydantic request/response models (FetchRequest, FetchResponse)
│
├── src/
│   ├── api/               FastAPI application
│   │   ├── app.py         App factory (create_app); mounts /fetch + /health, audit middleware
│   │   ├── auth.py        Builds require_scope / get_actor from platform_auth (envelope="connectors_")
│   │   ├── deps.py        get_key_store / get_source_registry — the single wiring point
│   │   ├── hashing.py     Thin shim over platform_auth: connectors_-envelope key generation
│   │   └── routers/
│   │       └── fetch.py    POST /fetch — require_scope("fetch"), maps source errors → HTTP status
│   │
│   ├── sources/            Source-agnostic fetch layer (no FastAPI)
│   │   ├── base.py             SourceFetcher Protocol + normalized error hierarchy
│   │   ├── registry.py         Config-driven source_id → SourceFetcher wiring
│   │   ├── google.py           GoogleSource: URL → file id → Drive metadata → extractor
│   │   └── google_extractors/  Per-MIME-type extraction strategies (native Docs/Slides/Sheets/Forms APIs,
│   │                            Drive export, local PDF/.docx parsing)
│   │
│   ├── mint_key.py        connectors-keys CLI — prints a key + its CONSUMER_KEYS entry, no store writes
│   └── config.py          Settings (CONNECTORS_ENV, API_KEY, CONSUMER_KEYS, GOOGLE_CREDENTIALS_JSON, ...) + boot check
│
├── tests/                 Fast tests — no Docker, no network (fake Google clients)
├── Dockerfile             Production image (built + import-smoke-tested by CI; used by Railway).
│                          Build context is the repo root; installs via `uv sync --frozen --no-dev --package connectors`.
├── docker-compose.yml     Builds/runs the image locally on port 8005 (no DB service).
└── railway.json           Railway deploy config: Dockerfile builder, /health check, NO preDeployCommand
                            (stateless — nothing to migrate).
```

## Testing

The suite is single-mode — no Docker, no database, no network:

```bash
uv run pytest
```

Runs with Google API clients injected as fakes via `app.dependency_overrides` / constructor injection, covering the `/fetch` happy path, URL parsing, the Docs/Slides/Sheets/Forms extractors, the local PDF/`.docx` parsers, the Drive-export fallback for uploaded `text/*` files, the auth paths, config/boot checks, and the source-error → HTTP-status mapping.

Lint and format with ruff:

```bash
uv run ruff check .
uv run ruff format .
```

**CI** runs a `connectors-test` job (`.github/workflows/ci.yml`): `uv sync --extra dev`, `uv run pytest`, `ruff check`, and `ruff format --check`. The `docker-build` job builds the Dockerfile and smoke-tests that the app imports at boot (`python -c "import src.api.app"`).

## Status

v0.1: a stateless `POST /fetch` adapter over a Google Drive/Docs/Slides/Sheets/Forms service account (plus local parsing of uploaded PDF and `.docx` files), config-seeded scoped API keys (`fetch` / `admin`) with an attested-actor audit trail, and a fast 132-test suite with no external dependencies.

**Not implemented (by design):**

- **Persistence** — no database. Consumers that need to cache fetched content own that cache themselves.
- **Per-end-user authorization** — connectors authenticates the calling service, not the end user on whose behalf a fetch happens; that check belongs in the consumer.
- **Non-Google sources** — the source registry is designed to grow (`SourceFetcher` protocol), but only Google Drive/Docs/Sheets/Slides/Forms (plus PDF/`.docx` parsed via Drive bytes) is wired up today.
- **OCR** — a scanned PDF with no embedded text layer captures nothing; see "Known limitations" above.
- **Forms responses** — only form structure (title, questions, options) is read; responses are deliberately never fetched (see the Supported types table above).

## Documentation

- [docs/API.md](docs/API.md) — consumer-facing endpoint reference: request/response shapes, errors, curl examples
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — contributor orientation: why the service is shaped this way, boundaries, trade-offs
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) — task walkthroughs and the pre-push checklist
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — deploy shape, variables, key provisioning, troubleshooting
