# connectors — architecture

Why this service is shaped the way it is. For *what it does* and how to run it, see the [README](../README.md); for *how to change it*, see [CONTRIBUTING.md](CONTRIBUTING.md).

## What it is, in one line

An **outbound adapter**: it holds the credential for an external document source, and turns a URL into text. Nothing else.

## What it deliberately is not

Two non-goals do more to explain the design than any of the goals:

- **Not a gateway.** It does not front other UTMIST services or route traffic to them. Its only outbound direction is *away* from the platform, to Google.
- **Not an authorization boundary.** It authenticates the *calling service* via a scoped API key, but it never learns which end user that service is fetching on behalf of. There is no `X-On-Behalf-Of` here (unlike documentation-system). Access control for the underlying file is entirely Drive's own sharing settings on the service account's address.

The second point is the one that bites. A consumer that needs "can *this particular person* read this doc?" must answer that itself **before** calling `/fetch`. connectors will happily return any document the service account can see, to any caller holding the `fetch` scope.

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
| `SourceForbidden` | 403 | Credential valid, file not shared with it |
| `SourceNotFound` | 404 | No such file, or the URL yields no file id |
| `SourceUnsupported` | 422 | Recognized file with no text form |
| `SourceUnavailable` | 502 | Upstream 5xx, timeout, transport failure |

This is why a source implementation never imports FastAPI: it raises a domain error, and the router owns the translation. A new source that raises these five errors gets correct HTTP behavior for free.

Google's own `HttpError` statuses are normalized in exactly one place — `execute()` in `google_extractors/base.py`, which every extractor routes its API calls through. One upstream status maps to one `SourceError` at one site.

## The extractor strategy

Each Google editor type has a native API that returns far richer structure than Drive's flat text export — a Doc's headings and tables, a Sheet's tabs, a Slide deck's per-slide structure. Adopting them one at a time must not disturb the fetch path, so extraction is a **strategy keyed by MIME type**:

```
GoogleSource.fetch(url)
  → parse_file_id(url)                    regex table, one pattern per URL shape
  → drive.files().get(...)                metadata: name + mimeType
  → EXTRACTORS[mime]                      or the text/* Drive-export fallback
  → extractor.extract(services, file_id, mime)  → ExtractedText(text, warnings)
  → SourceResult(title, content[:max], warnings)
```

The non-obvious part is that **extractors declare their own requirements**. Each one carries `scopes` and `services` tuples, and `required_scopes()` / `required_services()` union across the registered set. Adding a native extractor therefore does not mean editing a hard-coded scope constant somewhere else — the union picks it up. `_build_services` raises `SourceNotConfigured` for any service name missing from `_API_VERSIONS`, so a forgotten version mapping fails loudly at the first fetch rather than producing a confusing Google error.

A consequence worth internalizing: **a new supported file type does not imply a new OAuth scope.** Uploaded PDFs and `.docx` files are downloaded as raw bytes through the Drive media path that `drive.readonly` already covers, then parsed locally with `pypdf` / `python-docx`. Only a genuinely new *Google API* adds a scope.

## Threading: what is memoized and what is not

This is the subtlest thing in the service, and the comments in `google.py` exist because getting it wrong is silent corruption rather than a crash.

FastAPI runs sync route handlers in a threadpool, so concurrent `/fetch` calls really are concurrent.

- **Credentials are memoized** (`_get_credentials`, double-checked locking). Decoding the key and exchanging a JWT for a token is expensive and must not happen per request. Two threads racing the first build is benign: at worst there's a duplicate token exchange, last write wins, and both tokens are valid.
- **The transport is rebuilt every fetch** (`_build_services`). `httplib2.Http` keeps a mutable per-host connection pool and is **not** thread-safe. Sharing one across concurrent fetches risks one request's response bytes arriving on another request's connection. This is the reason the clients aren't cached alongside the credentials, and it should stay that way.

Relatedly, `build_registry` gives all four Google source ids (`gdocs`/`gsheets`/`gslides`/`gdrive`) a **single shared `GoogleSource` instance** — they have identical config, and one instance means one memoized credential rather than four.

## Degradation: no credential is a valid running state

`GOOGLE_CREDENTIALS_JSON` may be empty in **any** tier, including production. It is deliberately excluded from `verify_production_secrets()`. A connectors deploy with no Google project yet still boots, serves `/health`, passes auth, and returns 503 for Google fetches.

That is what lets connectors be deployed and wired to documentation-system *before* the Google Cloud project exists — turning on Google support later is a variable edit plus a redeploy, not a code change or a deploy-order dependency. documentation-system is built to tolerate exactly this: a failed fetch is a warning on the ingested doc, never an error.

Contrast with `API_KEY` and `CONSUMER_KEYS`, which *are* boot-checked: `get_key_store()` is called eagerly in `create_app()`, so a malformed `CONSUMER_KEYS` kills the container at startup instead of on the first request.

## Where the truncation actually happens

Two clamps exist, and only one of them is meant to fire:

- connectors' `MAX_CONTENT_CHARS` defaults to **1,200,000**.
- documentation-system's own `MAX_CONTENT_CHARS` is **1,000,000**.

The gap is deliberate. The consumer's clamp trips first and reports a truncation warning to whoever ingested the doc; connectors' clamp is a transport backstop that should never be the visible one. If you change either, keep connectors' the larger of the two.

## Why no persistence

Caching fetched content here would mean deciding invalidation policy on behalf of every consumer, and holding document text in a service whose entire authorization story is "the caller already checked." Consumers that want a cache own it — documentation-system stores its own content snapshots, with its own staleness rules.
