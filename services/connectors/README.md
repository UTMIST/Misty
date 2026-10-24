# connectors

HTTP adapter for UTMIST's internal consumers: document fetching, Google metadata discovery, and an optional Google meeting-Doc writer, with scoped API-key auth and no local database.

## What this service does

Consumers like documentation-system fetch Google documents and GitHub text without holding source credentials themselves. connectors owns the source-specific read logic and credentials, exposes bounded Google metadata discovery, and uses a separate OAuth identity for [meeting-Doc operations](docs/API.md#meeting-writer-googledocs), including creation, sharing, owner reads, and deletion.

Two things this service deliberately is **not**:

- **Not a gateway.** It does not sit in front of other UTMIST services or route traffic to them.
- **Not the final authorization boundary.** It authenticates the *calling service*, not an end user. For Google sources it returns the complete effective Drive ACL with the text; documentation-system resolves that evidence to verified people and membership-synced team groups before storing or disclosing content. The ACL is read before export/download, and an ACL-read failure blocks the content fetch.

There is no local database, migration, or persisted key store. API keys are seeded from a config env var at boot. The meeting writer changes durable Google state; interrupted writes need [catalog reconciliation](docs/DEPLOYMENT.md#rollback).

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

The default dev bootstrap key is `dev-api-key-change-me` (set in `.env`, carries the `admin` wildcard scope). Pass it as `X-API-Key` on business requests:

```bash
curl -sS http://localhost:8005/fetch \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"source_id": "gdocs", "url": "https://docs.google.com/document/d/<file-id>/edit"}' | python3 -m json.tool
```

**Ports:** this service runs on **8005** (team-tracking 8000, documentation-system 8001, llm 8002, verification 8003, meeting 8004) — chosen to avoid colliding when running the services locally.

### Environment tiers (`CONNECTORS_ENV`)

`CONNECTORS_ENV` declares which environment this instance represents: `local` (default), `staging`, or `production`. Outside `local`, `create_app()` calls `verify_production_secrets()` and **refuses to start** unless `API_KEY` is overridden from the dev default — a misconfigured deploy dies at boot, not on first request. `CONSUMER_KEYS` is also validated at boot (`get_key_store()` is called eagerly in `create_app()`), so a malformed value fails the container immediately rather than on the first `/fetch` call.

The read account is deliberately **not** part of that boot check: `GOOGLE_CREDENTIALS_JSON` may be empty in any tier. The optional meeting writer may be entirely disabled; partial writer configuration fails boot outside local. A connectors deploy with no Google account yet still starts, serves `/health`, and returns 503 for Google source fetches.

### Configuration

All settings load from the environment (`src/config.py`, `.env` in dev):

| Var | Default | Purpose |
|-----|---------|---------|
| `CONNECTORS_ENV` | `local` | Environment tier (`local` / `staging` / `production`). |
| `API_KEY` | `dev-api-key-change-me` | Env-bootstrap key; carries `admin` scope. Must be overridden outside `local`. |
| `CONSUMER_KEYS` | `""` | JSON array of per-consumer keys (see auth model). |
| `GOOGLE_CREDENTIALS_JSON` | `""` | Base64 of the Google service-account JSON key file. Empty is a valid running state — see the runbook below. |
| `GITHUB_TOKEN` | `""` | Optional fine-grained, read-only GitHub PAT (Contents: read). Empty fetches public repositories only; private ones then return 404. Never part of the boot check. |
| `MAX_CONTENT_CHARS` | `1200000` | Transport guard: truncates fetched content above this length. Deliberately set above documentation-system's own `MAX_CONTENT_CHARS` (1,000,000) so that clamp — not this one — is the one that actually trips and reports a truncation warning. |
| `MAX_FILE_BYTES` | `26214400` | Metadata precheck and streamed byte cap for uploaded PDFs, `.docx` files, `text/*` media, and GitHub files (25 MiB). Native Google editor API responses/exports are exempt. |
| `REQUEST_TIMEOUT_S` | `30` | Per-request timeout to upstream sources (seconds). |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN` | `""` | Optional Workspace user OAuth writer credentials (`drive.file`), separate from the read account. |
| `GOOGLE_MEETING_ROOT_FOLDER_ID` | `""` | Admin-only root folder accessible to the writer OAuth app. |
| `GOOGLE_MEETING_ADMIN_EMAILS` | `[]` | JSON array of explicitly trusted infra user/group emails, including inherited Drive admins. |

## Auth model

Auth is scoped API keys via the shared `platform_auth` package, matching `llm`, team-tracking, and documentation-system — but **DB-free**. There is no `api_keys` table.

- Business endpoints need `X-API-Key`; health and the API documentation/schema routes are unauthenticated. A key is either the shared bootstrap env key (`API_KEY`, scope: `admin`) or a per-consumer key seeded from the `CONSUMER_KEYS` env var.
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

Business endpoints require `X-API-Key`. `/health` and the API documentation/schema routes (`/docs`, `/redoc`, `/openapi.json`) are unauthenticated within the service's network exposure.

| Method | Path | Scope | Description |
|--------|------|-------|-------------|
| POST | `/fetch` | `fetch` | Fetch a document's text content from a source. |
| POST | `/discover/google` | `discover` | List a bounded metadata page; see the [discovery contract](docs/API.md#post-discovergoogle). |
| POST / PUT / GET / DELETE | `/google/docs` route group | `meeting-write` / `meeting-fetch` | Create, find, share, read, check, or delete meeting Docs; see [per-route scopes](docs/API.md#meeting-writer-googledocs). |
| GET | `/health` | none | Liveness probe → `{"status": "ok"}`. |

`POST /fetch` requires the `fetch` scope (`require_scope("fetch")`); the `admin` wildcard satisfies it, so the bootstrap key and any admin key also work.

**Request body** (`FetchRequest`, `contracts/fetch.py`):

| Field | Type | Notes |
|-------|------|-------|
| `url` | string | Required, non-empty. The document URL. |
| `source_id` | string | Required, non-empty. Which source to use (`gdocs`, `gsheets`, `gslides`, `gdrive`, or `github`). Supplied by the caller — the caller (e.g. documentation-system) already resolves this during ingest, so it is not re-derived here. |

`gdrive` covers everything that isn't a native Google Doc/Slides/Sheet: uploaded `text/*` files (via Drive media download), and — per the extractor registered for the file's MIME type — uploaded PDFs, uploaded `.docx` files, and Google Forms.

| MIME type | Extractor | Output |
|-----------|-----------|--------|
| `application/vnd.google-apps.document` (Google Doc) | Docs (native API) | Markdown: headings, list bullets, tables |
| `application/vnd.google-apps.presentation` (Google Slides) | Slides (native API) | Markdown, one section per slide |
| `application/vnd.google-apps.spreadsheet` (Google Sheet) | Sheets (native API) | Markdown table per tab |
| `application/pdf` (uploaded PDF) | PDF (`pypdf`, local parse) | One `## Page N` section per page |
| `.docx` (uploaded, `application/vnd.openxmlformats-officedocument.wordprocessingml.document`) | Docx (`python-docx`, local parse) | Markdown mirroring the Docs extractor: headings, list bullets, tables |
| `application/vnd.google-apps.form` (Google Form) | Forms (native API) | Markdown: title, description, section headings, and each question with its multiple-choice options where present. **Responses are never fetched** — they duplicate the linked responses spreadsheet and are applicant personal data. |
| `text/*` (uploaded) | Drive media download fallback | Raw text |

`github` accepts two URL shapes, fetched with plain REST calls against `api.github.com`:

| URL | Fetches | Title |
|-----|---------|-------|
| `github.com/{owner}/{repo}` (or `/tree/{ref}`) | The README GitHub resolves for that ref (default branch if none) — any README name/extension | `owner/repo` |
| `github.com/{owner}/{repo}/blob/{ref}/{path}` | That file at that ref; any UTF-8 text file, not only Markdown | `owner/repo/path` |

A repository with no README omits `content` and returns a `repository has no README` warning. Binary or non-UTF-8 files are 422. GitHub returns 404 for a private repository the token cannot see, so that surfaces as 404, not 403. A ref may contain `/` (`feature/docs`): it is matched against the repository's branches and tags, longest match first, or taken as a commit SHA once GitHub confirms that commit exists; an unknown ref or commit (or an empty repository) is 404, not a README-less repository. Percent-encoded paths (`setup%20guide.md`) are decoded once. A renamed or transferred repository's redirect is followed.

Legacy `.doc` (`application/msword`, the pre-2007 binary Word format) is a different format from `.docx` and stays unsupported — `python-docx` cannot read it.

**Response** (`FetchResponse`): `{ "title", "content", "warnings": [], "permissions": [...] }`.
Fields whose internal value is `None` are omitted, including unavailable `title` or `content`.
Each Google permission includes its opaque permission id, principal type and email/domain,
role, inherited parent/shared-drive ids, source expiration, and link-discovery flag. Non-Google sources may
omit `permissions`; an omitted Google ACL is an authorization failure to consumers, never
evidence that the file is public.

### Error → status table

| Condition | Status |
|-----------|--------|
| `source_id` not registered | 422 |
| Source not configured (e.g. `GOOGLE_CREDENTIALS_JSON` empty, or malformed) | 503 |
| Source denied access (Google 403; file sharing is one possible cause) | 403 |
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
   - **Google Drive API** — always required. Used for file metadata (name, MIME type) on every fetch and media downloads of uploaded `text/*`, PDF, and `.docx` files, which are parsed locally rather than via a native Google API.
   - **Google Docs API** — required for the native Docs extractor.
   - **Google Slides API** — required for the native Slides extractor.
   - **Google Sheets API** — required for the native Sheets extractor.
   - **Google Forms API** — required for the native Forms extractor.
3. **Create a service account** in that project (IAM & Admin → Service Accounts).
4. **Create a JSON key** for the service account and download it. Treat this file as a secret — do not commit it.
5. **Share each Drive folder/file connectors needs to read** with the service account's email address (looks like `name@project-id.iam.gserviceaccount.com`), granting sufficient access to inspect the complete ACL (the verified setup uses **Editor**). Nothing is readable until it's explicitly shared.
6. **Base64-encode the key file** and set it as `GOOGLE_CREDENTIALS_JSON`:
   ```bash
   base64 -i service-account.json | tr -d '\n'
   ```
   Paste the result into `GOOGLE_CREDENTIALS_JSON` (in `.env` locally, or as a Railway variable in staging/production).

### Scopes

The service account's credentials are built with these OAuth scopes:

- `https://www.googleapis.com/auth/drive.readonly` — always required (file metadata on every fetch and Drive media downloads of uploaded `text/*`, PDF, and `.docx` files — see below).
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

Uploaded PDFs, `.docx` files, `text/*` files, and GitHub files larger than
`MAX_FILE_BYTES` return `422`. Known oversized files are rejected before download;
the streamed body is also capped before parsing, covering missing or stale Drive
`size` metadata and files that grow after the metadata read. GitHub requires a
size from its raw-file `HEAD` request. Native Google editor API responses and
exports are exempt. See [download bounds](docs/ARCHITECTURE.md#where-the-truncation-actually-happens)
for the transport and parser-memory limits.

### Running without Google access

`GOOGLE_CREDENTIALS_JSON` empty is a valid, supported running state: `/fetch` returns **503** for any Google source, and every other part of the service (health check, auth, non-Google sources if added later) works normally. This means connectors can be deployed and wired up to consumers **before** the Google Cloud project and service account exist — flip on Google support later by setting the variable and redeploying, no code change required.

## Repo layout

```
connectors/
├── contracts/
│   ├── fetch.py            Pydantic fetch request/response models
│   ├── discovery.py        Discovery models and Protocol
│   └── meeting.py          Meeting writer models, Protocol, and errors
│
├── src/
│   ├── api/               FastAPI application
│   │   ├── app.py         App factory; mounts fetch, discovery, meeting routes + /health, audit middleware
│   │   ├── auth.py        Builds require_scope / get_actor from platform_auth (envelope="connectors_")
│   │   ├── deps.py        Key store, source registry, discovery source, meeting writer — the single wiring point
│   │   ├── hashing.py     Thin shim over platform_auth: connectors_-envelope key generation
│   │   └── routers/
│   │       ├── fetch.py    POST /fetch — fetch scope, maps source errors → HTTP status
│   │       ├── discovery.py POST /discover/google — discover scope
│   │       └── meeting.py  /google/docs routes — meeting-write / meeting-fetch scopes
│   │
│   ├── sources/            Source-agnostic fetch layer (no FastAPI)
│   │   ├── base.py             SourceFetcher Protocol + normalized error hierarchy
│   │   ├── registry.py         Config-driven source_id → SourceFetcher wiring
│   │   ├── google.py           GoogleSource: URL → file id → Drive metadata → extractor
│   │   ├── github.py           GithubSource: repo README or /blob/ file via the GitHub REST API (httpx)
│   │   ├── meeting_google.py   Separate OAuth meeting writer
│   │   └── google_extractors/  Per-MIME-type extraction strategies (native Docs/Slides/Sheets/Forms APIs,
│   │                            Drive media download, local PDF/.docx parsing)
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

Runs with Google API clients injected as fakes via `app.dependency_overrides` / constructor injection, covering the `/fetch` happy path, URL parsing, the Docs/Slides/Sheets/Forms extractors, the local PDF/`.docx` parsers, the Drive media download fallback for uploaded `text/*` files, the auth paths, config/boot checks, and the source-error → HTTP-status mapping.

Lint and format with ruff:

```bash
uv run ruff check .
uv run ruff format .
```

**CI** runs a `connectors-test` job (`.github/workflows/ci.yml`): `uv sync --extra dev`, `uv run pytest`, `ruff check`, and `ruff format --check`. The `docker-build` job builds the Dockerfile and smoke-tests that the app imports at boot (`python -c "import src.api.app"`).

## Status

The service fetches Google and GitHub text, enumerates Google metadata, and optionally manages meeting Docs through a separate OAuth writer. Config-seeded keys use `fetch`, `discover`, `meeting-write`, and `meeting-fetch` scopes (`admin` satisfies all), with an attested-actor audit trail and offline tests.

**Not implemented (by design):**

- **Local persistence** — no database. Consumers own fetched-content caches and reconciliation of external Google writes.
- **Per-end-user authorization** — connectors authenticates the calling service, not the end user on whose behalf a fetch happens; that check belongs in the consumer.
- **GitHub harvesting** — `github` fetches one explicitly supplied README or file; it does not enumerate repositories, sync, or read issues/PRs/wikis.
- **OCR** — a scanned PDF with no embedded text layer captures nothing; see "Known limitations" above.
- **Forms responses** — only form structure (title, questions, options) is read; responses are deliberately never fetched (see the Supported types table above).

## Documentation

- [docs/API.md](docs/API.md) — consumer-facing endpoint reference: request/response shapes, errors, curl examples
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — contributor orientation: why the service is shaped this way, boundaries, trade-offs
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) — task walkthroughs and the pre-push checklist
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — deploy shape, variables, key provisioning, troubleshooting

Automatic sharing discovery also exposes [`POST /discover/google`](docs/API.md#post-discovergoogle)
with a separate `discover` scope. See the [operator/member runbook](../documentation-system/docs/DISCOVERY.md).
