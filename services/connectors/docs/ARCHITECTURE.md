# connectors — architecture

Why this service is shaped the way it is. For *what it does* and how to run it, see the [README](../README.md); for *how to change it*, see [CONTRIBUTING.md](CONTRIBUTING.md).

## What it is, in one line

An **outbound adapter**: it holds source credentials, turns URLs into text, enumerates bounded source metadata pages, and manages meeting Docs through a separate OAuth writer.

## What it deliberately is not

Two non-goals do more to explain the design than any of the goals:

- **Not a gateway.** It does not front other UTMIST services or route traffic to them. Its outbound calls go to external sources (Google and GitHub).
- **Not the final authorization boundary.** It authenticates the *calling service* and never learns the end user. Google fetches return complete source ACL evidence alongside content; documentation-system maps that evidence to its people/teams and applies actor-scoped visibility.

The second point is the one that bites. connectors reads the Drive permission list **before** any export/download and refuses the fetch if that list cannot be read completely, but it cannot decide which UTMIST identity a Google email/group represents. A consumer must reconcile the returned ACL before persisting or disclosing the content. Broad service-account reach alone is never an end-user grant.

## Layering

```
contracts/fetch.py          Pydantic wire types (FetchRequest, FetchResponse)
        ▲
src/api/                    FastAPI — routing, auth, error→status mapping
        │  depends on
        ▼
src/sources/base.py         SourceFetcher Protocol + SourceError hierarchy
        ▲
src/sources/google.py       GoogleSource — one implementation
        │
src/sources/google_extractors/   per-MIME-type extraction strategies
```

The rules, same as every other service in the platform:

- `contracts/` imports nothing from `src/`.
- `src/sources/` imports no FastAPI and no `src/api/`. It is a plain library that could be lifted out and used from a script.
- `src/api/deps.py` is the **single wiring point** for the API — it builds the registry from `Settings` (via `build_registry`) and exposes it as a dependency, which is what lets tests swap in fakes with `app.dependency_overrides[get_source_registry]`. `GoogleSource` itself is constructed one layer down, in `src/sources/registry.py`; `deps.py` never names a concrete source.

## The error hierarchy is the contract

`src/sources/base.py` defines five normalized errors, and `src/api/routers/fetch.py` maps each to exactly one HTTP status:

| `SourceError` subclass | Status | Meaning |
|---|---|---|
| `SourceNotConfigured` | 503 | No usable credential for this source |
| `SourceForbidden` | 403 | Source denied access; Google 403 is normalized without inspecting its reason |
| `SourceNotFound` | 404 | No such file, or the URL yields no file id |
| `SourceUnsupported` | 422 | Recognized file with no text form |
| `SourceUnavailable` | 502 | Upstream 5xx, timeout, transport failure |

This is why a source implementation never imports FastAPI: it raises a domain error, and the router owns the translation. A new source that raises these five errors gets correct HTTP behavior for free.

Read-source Google `HttpError` statuses are normalized in `execute()` in
`google_extractors/base.py`, shared by fetch, discovery, and extractors. One
upstream status maps to one `SourceError` there. Meeting operations expose a
separate `MeetingWriteError` contract so ambiguous writes remain distinguishable;
see [meeting writes](#meeting-writes).

## The extractor strategy

Each Google editor type has a native API that returns far richer structure than Drive's flat text export — a Doc's headings and tables, a Sheet's tabs, a Slide deck's per-slide structure. Adopting them one at a time must not disturb the fetch path, so extraction is a **strategy keyed by MIME type**:

```
GoogleSource.fetch(url)
  → parse_file_id(url)                    regex table, one pattern per URL shape
  → drive.files().get(...)                metadata: name + mimeType + optional size
  → EXTRACTORS[mime]                      or the text/* Drive media download fallback
  → media size guard                       PDF/.docx/text/* before byte download
  → extractor.extract(services, file_id, mime)  → ExtractedText(text, warnings)
  → SourceResult(title, content[:max], warnings)
```

Google Docs requests set `includeTabsContent=true` and traverse top-level and
nested tabs in document order. [Google's tabs API](https://developers.google.com/workspace/docs/api/how-tos/tabs)
returns tab content with an empty legacy `body`; omitting the parameter returns
only the first tab in `body`. The body fallback supports legacy response shapes
and test fakes only: it cannot recover omitted tabs, does not run when tabs are
present but empty, and never retries a failed API call without the flag.

In multi-tab documents, each tab with rendered content gets an H1 label such as
`# Tab: "Operations" / "Recovery"`. Each title is JSON-quoted so literal slashes,
quotes, backslashes and line breaks cannot masquerade as path separators or
headings. Blank titles become `Untitled tab`. Empty tabs emit no label, but their
names remain in their children's paths. Document headings move down one level,
capped at H6 (H5 and H6 therefore share that level), so TITLE/HEADING_1 cannot
close the tab's section in the catalog chunker. A single tab keeps its existing
rendered bytes and content hash. Chunks spanning multiple short tabs still have
no common `heading_path`; their text retains the labels under the catalog's
existing packing policy.

After deployment, existing snapshots need a successful fetch before later-tab
text can reach retrieval. Use the catalog's existing
[source-refresh schedule](../../documentation-system/docs/DEPLOYMENT.md#source-access-refresh-schedule)
for eligible Google records, or `POST /docs/{id}/refetch` for affected documents
that need immediate repair. Normal content hashes trigger re-indexing when the
extracted text changes; the chunker itself is unchanged, so no chunker-version
bump or schema migration is needed. There is no extractor-version inventory:
an old partial snapshot can look current until it is refetched.
[Discovery](../../documentation-system/docs/DISCOVERY.md#recovery-and-lifecycle)
does not refetch existing records, and `doc-backfill` only finds records missing
content, not partially extracted ones. For rollout acceptance, record affected
catalog IDs and deployment/refetch outcomes, then verify indexed later-tab
passages and their tab context with an authorized search. Track fetch and index
failures separately using the existing recovery runbook; deployment alone is
not evidence that existing documents were repaired.

The non-obvious part is that **extractors declare their own requirements**. Each one carries `scopes` and `services` tuples, and `required_scopes()` / `required_services()` union across the registered set. Adding a native extractor therefore does not mean editing a hard-coded scope constant somewhere else — the union picks it up. `_build_services` raises `SourceNotConfigured` for any service name missing from `_API_VERSIONS`, so a forgotten version mapping fails loudly at the first fetch rather than producing a confusing Google error.

A consequence worth internalizing: **a new supported file type does not imply a new OAuth scope.** Uploaded PDFs and `.docx` files are downloaded as raw bytes through the Drive media path that `drive.readonly` already covers, then parsed locally with `pypdf` / `python-docx`. Only a genuinely new *Google API* adds a scope.

Uploaded PDF, `.docx`, and text downloads must use the SDK's
`files().get_media(supportsAllDrives=True, ...)`. Metadata reads also set
`supportsAllDrives=True`, matching the permission reads.
Calling `files().get(alt="media")` still selects its JSON response model, which
overwrites `alt` and returns metadata instead of bytes. `execute_bytes()` wraps
`execute()` for media downloads and native Drive exports: a non-bytes response is
`SourceUnavailable` (502), before any decoding or local parsing. Malformed PDF or
Word bytes remain `SourceUnsupported` (422). Offline regression tests exercise
the real SDK with a shared fake HTTP transport, including `files().export()`.

pypdf diagnostics can contain document text from malformed input. The PDF
extractor installs a `NullHandler` on the `pypdf` package logger and disables
propagation to application handlers once at import. This covers child warnings
and errors without changing logging during concurrent extractions. Do not attach
output handlers directly to pypdf loggers. Safe `SourceUnsupported` messages and
the response's no-text-layer warning still describe extraction failures/loss.

## Threading: what is memoized and what is not

This is the subtlest thing in the service, and the comments in `google.py` exist because getting it wrong is silent corruption rather than a crash.

FastAPI runs sync route handlers in a threadpool, so concurrent `/fetch` calls really are concurrent.

- **Credentials are memoized** (`_get_credentials`, double-checked locking). Decoding the key and exchanging a JWT for a token is expensive and must not happen per request. Two threads racing the first build is benign: at worst there's a duplicate token exchange, last write wins, and both tokens are valid.
- **The transport is rebuilt every fetch** (`_build_services`). `httplib2.Http` keeps a mutable per-host connection pool and is **not** thread-safe. Sharing one across concurrent fetches risks one request's response bytes arriving on another request's connection. This is the reason the clients aren't cached alongside the credentials, and it should stay that way.

Relatedly, `build_registry` gives all four Google source ids (`gdocs`/`gsheets`/`gslides`/`gdrive`) a **single shared `GoogleSource` instance** — they have identical config, and one instance means one memoized credential rather than four.

## Degradation: no credential is a valid running state

`GOOGLE_CREDENTIALS_JSON` may be empty in **any** tier, including production. It is deliberately excluded from `verify_production_secrets()`, as is the optional `GITHUB_TOKEN` (public repositories need none). A connectors deploy with no Google project yet still boots, serves `/health`, passes auth, and returns 503 for Google fetches.

That is what lets connectors be deployed and wired to documentation-system *before* the Google Cloud project exists — turning on Google support later is a variable edit plus a redeploy, not a code change or a deploy-order dependency. documentation-system is built to tolerate exactly this: a failed fetch is a warning on the ingested doc, never an error.

Contrast with `API_KEY` and `CONSUMER_KEYS`, which *are* boot-checked: `get_key_store()` is called eagerly in `create_app()`, so a malformed `CONSUMER_KEYS` kills the container at startup instead of on the first request.

## Where the truncation actually happens

Two clamps exist, and only one of them is meant to fire:

- connectors' `MAX_CONTENT_CHARS` defaults to **1,200,000**.
- documentation-system's own `MAX_CONTENT_CHARS` is **1,000,000**.
- connectors' `MAX_FILE_BYTES` defaults to **25 MiB** and checks uploaded PDF, `.docx`, `text/*`, and GitHub file metadata before download, then caps the streamed body before parsing.

The gap is deliberate. The consumer's clamp trips first and reports a truncation warning to whoever ingested the doc; connectors' clamp is a transport backstop that should never be the visible one. If you change either, keep connectors' the larger of the two.

`_build_services` places `BoundedDriveHttp` beneath Drive's `AuthorizedHttp`.
Metadata, native exports and credential refresh use the existing SDK transport;
uploaded-file media uses HTTPX streaming with the configured timeout and byte
limit. Missing/stale metadata and ignored `Range` headers cannot bypass this
cap. Each raw network chunk is checked before appending to the download buffer;
an oversized body is closed and rejected as `SourceUnsupported` (422).

The transport requests identity encoding and rejects unexpected encodings before
decompression, as well as partial responses (502). Redirect bodies are closed
unread; redirect targets retain the cap, have a fixed hop limit, and must use
HTTPS. HTTPX removes authorization on cross-origin redirects. HTTP error bodies
are also closed unread; the status still reaches the SDK's error mapping and
401 credential refresh.

The retained download buffer is capped at `MAX_FILE_BYTES`, with bounded extra
space for one network chunk and the final bytes copy. This is not a cap on PDF
or Word parser allocations or decompressed document structures. Native Google
editor API responses/exports remain exempt. `MediaIoBaseDownload` alone would
not provide this protection: an ignored Range request can make it buffer the
whole response before returning a chunk.

## Why no persistence

Caching fetched content here would mean deciding invalidation policy on behalf of every consumer, and holding document text in a service whose entire authorization story is "the caller already checked." Consumers that want a cache own it — documentation-system stores its own content snapshots, with its own staleness rules.

## Metadata discovery

The adapter also exposes bounded Google metadata enumeration through the
`DiscoverySource` Protocol. `deps.py` wires the configured Google identity; no Google
SDK enters the API layer. Each request lists one page of direct shares or one
folder's immediate children. Format classification uses the same `EXTRACTORS` and
`text/*` fallback as fetch. Discovery returns no ACLs or content and cannot authorize
a member. The external catalog runner owns traversal, checkpoints and scheduling;
connectors remains stateless. See [API](API.md#post-discovergoogle) and the
[discovery runbook](../../documentation-system/docs/DISCOVERY.md).

## Meeting writes

`contracts/meeting.py` is the injected writer boundary; only
`src/sources/meeting_google.py` imports Google OAuth/upload clients.
`src/api/deps.py` wires it separately from read-only source fetching. Each call
uses a fresh HTTP transport. Its `_execute()` calls `request.execute(num_retries=0)`
and raises `MeetingWriteError` with an `uncertain` flag; shared ACL-read errors are
also translated to that writer contract. Do not substitute read-source errors:
the writer router returns content-free `{code, uncertain}` details, and callers
must preserve ambiguous-create recovery. Content is an
in-memory HTML upload converted in the same Drive create operation; no empty Doc
is published and retries never replace a human-edited body. See the
[durable record architecture](../../../docs/MEETING-RECORDING.md#durable-records)
and [writer API](API.md#meeting-writer-googledocs).
