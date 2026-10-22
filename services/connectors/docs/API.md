# connectors — API reference

Base URL (local): `http://localhost:8005` · Swagger UI: `/docs` · Schema: `/openapi.json`

Everything except `/health` requires `X-API-Key`.

| Method | Path | Scope | Description |
|---|---|---|---|
| POST | `/discover/google` | `discover` | List one metadata page of direct shares or folder children |
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

> Unlike team-tracking, documentation-system, and verification, the `/fetch` input does **not** set `extra="forbid"` on its input model. An unknown field is silently ignored, not rejected with a 422. Don't rely on a typo'd field name surfacing as an error.

| Field | Type | Required | Notes |
|---|---|---|---|
| `url` | string | yes | Non-empty. The document URL. |
| `source_id` | string | yes | Non-empty. Which source to use. |

`source_id` is **supplied by the caller, not derived here.** documentation-system already resolves a URL's source kind during ingest, so re-deriving it would be duplicated logic that could disagree. Registered ids today: `gdocs`, `gsheets`, `gslides`, `gdrive` — all four map to the same Google source instance — and `github`.

`gdrive` is the catch-all for anything that isn't a native Google editor file: uploaded PDFs, `.docx`, and `text/*` uploads. In practice any of the four ids will fetch any Google file, since the extractor is chosen by the file's actual MIME type rather than by `source_id`; the id only selects *which source implementation* handles the URL.

`github` takes a repository URL (`github.com/{owner}/{repo}`, optionally `/tree/{ref}`) and returns its README, titled `owner/repo`, or a `/blob/{ref}/{path}` URL and returns that text file. The ref may contain `/` (`feature/docs`); it is matched against the repository's branches and tags (longest match wins), or taken as a commit SHA once GitHub confirms that commit exists; an unknown ref or an empty repository is 404. A renamed repository's redirect is followed. A repository without a README returns `content: null` with a warning. `permissions` is omitted for GitHub.

**Response** (`FetchResponse`) — `200`:

| Field | Type | Notes |
|---|---|---|
| `title` | string \| null | The Drive file name |
| `content` | string \| null | Extracted text, truncated at `MAX_CONTENT_CHARS` (default 1,200,000) |
| `warnings` | string[] | Non-fatal information loss. Empty on a clean fetch. |
| `permissions` | object[] \| omitted | Complete effective Google Drive ACL. Omitted only for sources without an ACL contract. |

Each permission carries `permission_id`, `principal_type` (`user`, `group`, `domain`, or
`anyone`), `principal` (email/domain when applicable), `role`, `inherited`, `inherited_from` (folder or
shared-drive ids), `expiration_time`, and `allow_file_discovery`. The ACL is fetched before content extraction;
if it cannot be read completely the request fails and no content is exported/downloaded.

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

Editor URLs also accept the account selector before `d/`, for example
`docs.google.com/document/u/0/d/<id>/edit` and `docs.google.com/forms/u/12/d/<id>/edit`.
The `/forms/d/e/<id>/viewform` published link carries a *published id*, not a Drive file id, and the Forms API cannot resolve it. The pattern excludes that shape, including account-indexed variants, so it returns a clean `404` instead of a confusing error from Google.

### Warnings

`warnings` is populated when extraction succeeded but lost information:

| Condition | Warning |
|---|---|
| A spreadsheet tab exceeds `MAX_ROWS_PER_TAB` (2000) | Names the tab and its real row count; the tab is truncated to 2000 rows |
| Sheets' `batchGet` returns fewer ranges than tabs | `batchGet returned N valueRanges for M tabs; some tabs may be missing from the output` |
| A PDF yields no text on any page | Notes that no text layer was found (the signature of a scanned document — there is no OCR) |

Every tab of a spreadsheet is read — there is no first-tab-only limitation. Docs and Slides have no size cap.

The `MAX_FILE_BYTES` guard applies before download to uploaded PDFs, `.docx` files, `text/*` files, and GitHub files (the size GitHub reports on a `HEAD` of the raw file; a file whose size is not reported is rejected too, and the download is additionally capped at the limit). Native Google editor types use their APIs and are not rejected when Drive metadata omits `size`.

### Errors

| Condition | Status | Detail |
|---|---|---|
| `source_id` not registered | 422 | `unsupported source: <id>` |
| File's MIME type has no text form | 422 | `no text form for mime type: <mime>` |
| PDF is encrypted | 422 | `pdf is encrypted and cannot be read` |
| PDF could not be parsed | 422 | `pdf could not be parsed: <ExceptionType>` |
| Uploaded PDF, `.docx`, `text/*`, or GitHub file exceeds `MAX_FILE_BYTES` | 422 | File-size limit detail; bytes are not downloaded |
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

## `POST /discover/google`

Requires a dedicated `discover` scope (`fetch` alone cannot enumerate). Returns
metadata only, using the same configured service-account identity as `/fetch`.
The strict request rejects extra fields:

```json
{"folder_id": null, "page_token": null, "page_size": 100}
```

With no folder, list directly shared, non-trashed items in Drive's user corpus.
With a folder id, list its immediate non-trashed children. Folder/file ids contain
letters, digits, `_` and `-`, at most 200 characters. Page size is 1–100; page tokens
are opaque, nonempty strings up to 4096 characters. Each call makes one listing
request; callers traverse folders and continue using the returned token.

```json
{
  "items": [{"file_id": "example", "kind": "file", "source_id": "gdocs"}],
  "next_page_token": null,
  "incomplete": false
}
```

`kind` is `file`, `folder`, `unsupported`, `shortcut`, or `shared_drive`. Only supported
files have a source id (`gdocs`, `gsheets`, `gslides`, `gdrive`), selected from the
extractor registry. Names, text, principals and permissions are not returned.
Shared Drive items and shortcuts are reported for exclusion, not followed. An empty
page can still have a next token. `incomplete=true` is a partial result, never proof
that absent items were deleted or access revoked. Invalid/repeated cursors and
malformed Google results fail safely. Authentication/scope errors are 401/403;
source denied listing is 403, missing source folder 404, missing configuration 503,
and upstream/invalid listing 502. Bodies and audit extras contain no raw Google data.

See the [discovery runbook](../../documentation-system/docs/DISCOVERY.md) for
supported topology, cadence, resumption, exclusions and staging acceptance.
`/fetch` still reads a complete ACL before extraction; its ACL pagination is capped
at 20 pages of 100 entries and fails if completeness cannot be established.

## Meeting writer (`/google/docs`)

All inputs forbid extra fields. The caller cannot choose a folder or an actor;
configuration selects the root and the authenticated key attests the actor.

| Method/path | Scope | Request / result |
|---|---|---|
| `POST /google/docs` | `meeting-write` | `{meeting_id: UUID, attempt_id: UUID, voice_channel_id, title, html}` → `{document_id, url}` |
| `POST /google/docs/search` | `meeting-write` | `{meeting_id}` → tagged IDs, attempt IDs, creation times, trashed flags; all pages, including trash and moved files |
| `PUT /google/docs/{id}/sharing` | `meeting-write` | `{commenters: [group emails]}` → complete read-back ACL |
| `POST /google/docs/{id}/fetch` | `meeting-fetch` | Owner-read title, text and full ACL in the existing fetch response shape |
| `GET /google/docs/{id}/status` | `meeting-fetch` | `{status: exists \| trashed \| inaccessible}` |
| `DELETE /google/docs/{id}` | `meeting-write` | Revoke direct shares, permanently delete; `{status: deleted}` |

Creation converts an in-memory HTML upload to a native Google Doc in one Drive
request. It does not subsequently write the body. Both the root and voice-channel
subfolder ACLs must contain only configured admin principals before upload.
The Doc initially inherits only those admins. Only the canonical ID chosen by
documentation-system may receive direct team group commenter shares. Read-back
refuses unexpected access and revokes direct shares before reporting
`unexpected_acl`; the catalog then removes that record.

Writer failures return a content-free `detail: {code, uncertain}` object: 503
for an unconfigured writer, 502 for ambiguous upstream outcomes, 409 for a
definite failure (including denied Google writes or unsafe destinations).
Authentication still uses 401/403; invalid requests use 422. A caller must never
blindly retry a create after `uncertain: true`. Documentation-system owns the
lease, canonical identity and recurring reconciliation.

Drive 404 cannot prove deletion, even for the writer: status returns
`inaccessible`, preserving catalog content for admin investigation. A trashed
file is positively identified; cleanup permanently deletes it. An unconfirmed
404 during deletion returns `delete_unconfirmed`, keeping cleanup pending.
