# connectors — API reference

Base URL (local): `http://localhost:8005` · Swagger UI: `/docs` · Schema: `/openapi.json`

Two endpoints. Everything except `/health` requires `X-API-Key`.

| Method | Path | Scope | Description |
|---|---|---|---|
| POST | `/fetch` | `fetch` | Fetch a document's text content from a source |
| GET | `/health` | — | Liveness probe |

## Authentication

Every request carries the key in the `X-API-Key` header:

```
X-API-Key: connectors_<prefix>_<secret>
```

A key is either the bootstrap env key (`API_KEY`, carries the `admin` wildcard) or a per-consumer key seeded from `CONSUMER_KEYS`. The `admin` scope satisfies any scope check, so the bootstrap key works on `/fetch` too.

There is **no `X-Actor` header** and no `X-On-Behalf-Of`. This is a service-to-service API; the audit actor is always the authenticated key's own name (attested actor). connectors never learns which end user a fetch is for — see [ARCHITECTURE.md](ARCHITECTURE.md#what-it-deliberately-is-not).

| Failure | Status |
|---|---|
| Missing or unparseable `X-API-Key` | 401 |
| Valid key lacking the `fetch` scope | 403 |

---

## `POST /fetch`

Resolve a document URL to plain text.

**Request** (`FetchRequest`, `contracts/fetch.py`):

> Unlike team-tracking, documentation-system, and verification, this service does **not** set `extra="forbid"` on its input model. An unknown field is silently ignored, not rejected with a 422. Don't rely on a typo'd field name surfacing as an error.

| Field | Type | Required | Notes |
|---|---|---|---|
| `url` | string | yes | Non-empty. The document URL. |
| `source_id` | string | yes | Non-empty. Which source to use. |

`source_id` is **supplied by the caller, not derived here.** documentation-system already resolves a URL's source kind during ingest, so re-deriving it would be duplicated logic that could disagree. Registered ids today: `gdocs`, `gsheets`, `gslides`, `gdrive` — all four map to the same Google source instance.

`gdrive` is the catch-all for anything that isn't a native Google editor file: uploaded PDFs, `.docx`, and `text/*` uploads. In practice any of the four ids will fetch any Google file, since the extractor is chosen by the file's actual MIME type rather than by `source_id`; the id only selects *which source implementation* handles the URL.

**Response** (`FetchResponse`) — `200`:

| Field | Type | Notes |
|---|---|---|
| `title` | string \| null | The Drive file name |
| `content` | string \| null | Extracted text, truncated at `MAX_CONTENT_CHARS` (default 1,200,000) |
| `warnings` | string[] | Non-fatal information loss. Empty on a clean fetch. |

**Example:**

```bash
curl -sS http://localhost:8005/fetch \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"source_id": "gdocs", "url": "https://docs.google.com/document/d/<file-id>/edit"}'
```

```json
{
  "title": "2026 Sponsorship Plan",
  "content": "# 2026 Sponsorship Plan\n\n## Goals\n...",
  "warnings": []
}
```

### What comes back per file type

The extractor is chosen by the file's MIME type, from `EXTRACTORS` in `src/sources/google.py`:

| MIME type | Extractor | Output shape |
|---|---|---|
| `application/vnd.google-apps.document` | Docs (native API) | Markdown: headings, list bullets, tables |
| `application/vnd.google-apps.presentation` | Slides (native API) | Markdown, one section per slide |
| `application/vnd.google-apps.spreadsheet` | Sheets (native API) | Markdown table per tab |
| `application/pdf` | PDF (`pypdf`, parsed locally) | One `## Page N` section per page |
| `.docx` (`…wordprocessingml.document`) | Docx (`python-docx`, parsed locally) | Markdown mirroring the Docs extractor |
| `application/vnd.google-apps.form` | Forms (native API) | Markdown: title, description, section headings, questions + options |
| `text/*` (uploaded) | Drive export fallback | Raw text |
| anything else | — | `422` |

Legacy `.doc` (`application/msword`) is a different format from `.docx` and is **not** supported — `python-docx` cannot read it.

**Google Forms responses are never fetched.** Only the form's structure is read. Responses duplicate the linked responses spreadsheet and are applicant personal data.

### Recognized URL shapes

`parse_file_id` (`src/sources/google.py`) matches these; anything else yields `404`:

```
docs.google.com/document/d/<id>
docs.google.com/spreadsheets/d/<id>
docs.google.com/presentation/d/<id>
docs.google.com/forms/d/<id>          (but NOT /forms/d/e/<published-id>)
drive.google.com/file/d/<id>
drive.google.com/open?id=<id>
```

The `/forms/d/e/<id>/viewform` published link carries a *published id*, not a Drive file id, and the Forms API cannot resolve it. The pattern excludes that shape on purpose so it returns a clean `404` instead of a confusing error from Google.

### Warnings

`warnings` is populated when extraction succeeded but lost information:

| Condition | Warning |
|---|---|
| A spreadsheet tab exceeds `MAX_ROWS_PER_TAB` (2000) | Names the tab and its real row count; the tab is truncated to 2000 rows |
| Sheets' `batchGet` returns fewer ranges than tabs | `batchGet returned N valueRanges for M tabs; some tabs may be missing from the output` |
| A PDF yields no text on any page | Notes that no text layer was found (the signature of a scanned document — there is no OCR) |

Every tab of a spreadsheet is read — there is no first-tab-only limitation. Docs and Slides have no size cap.

### Errors

| Condition | Status | Detail |
|---|---|---|
| `source_id` not registered | 422 | `unsupported source: <id>` |
| File's MIME type has no text form | 422 | `no text form for mime type: <mime>` |
| PDF is encrypted | 422 | `pdf is encrypted and cannot be read` |
| PDF could not be parsed | 422 | `pdf could not be parsed: <ExceptionType>` |
| Malformed request body | 422 | Pydantic validation error |
| Source not configured (`GOOGLE_CREDENTIALS_JSON` empty or malformed) | 503 | `source not configured` |
| Service account denied access to the file | 403 | `source denied access to this file` |
| Valid key without the `fetch` scope | 403 | — |
| No file found for the URL | 404 | `file not found for this url` |
| Upstream Drive/Docs API failure | 502 | `source upstream error` |
| Missing/invalid `X-API-Key` | 401 | — |

The **403 vs 503** distinction matters when debugging: 403 means the credential works but the file isn't shared with the service account's address; 503 means there's no usable credential at all.

---

## `GET /health`

Unauthenticated liveness probe. Railway's healthcheck path.

```bash
curl -sS http://localhost:8005/health
```

```json
{"status": "ok"}
```

Answers `200` even when `GOOGLE_CREDENTIALS_JSON` is unset — no credential is a supported running state, and `/fetch` returns 503 in that case. A green healthcheck therefore does **not** imply Google fetches work.

---

## Audit log

Every request emits one JSON line with the resolved actor, endpoint, status, and duration. `/fetch` additionally records `source_id`, and on success the number of `warnings` returned (`request.state.audit_extra`). The document's URL and content are never logged.
