# documentation-system — architecture

Orientation for contributors: read after the [README](../README.md) and before
[`CONTRIBUTING.md`](CONTRIBUTING.md). It explains *why* the service is shaped as it is.

## The one-sentence model

The documentation-system is a **catalog of URLs**. Given a URL, it determines what kind of
thing it is (its *source*), tries to fetch a title and snapshot, validates the owner against
the [team-tracking](../../team-tracking/) directory, and stores it — deduplicated, so the same
URL never appears twice. Everything below keeps that flow testable and swappable.

## The Protocol boundaries

No concrete dependency — database, URL fetching, directory, or embeddings — leaks into the
ingest logic or the HTTP layer. Each sits behind a **Protocol** (a structural interface in
`contracts/`). The ingest orchestrator and routers depend only on the Protocols; concrete
implementations are wired in one place (`src/api/deps.py`).

Why, in a student org? The fast suite runs in memory in about a second, with no Docker,
network, or Postgres, and swapping in Postgres required zero changes to ingest or routers.

| Protocol | File | Responsibility |
|----------|------|----------------|
| `StorageAdapter` | `contracts/storage.py` | Persist/query docs, content, embedded chunks, tags, sources, API keys |
| `Fetcher` | `contracts/fetcher.py` | `fetch(url) -> FetchResult` (title + snapshot) |
| `DirectoryClient` | `contracts/directory.py` | Resolve team/person ids to labels |
| `Embedder` | `contracts/embedder.py` | Embed chunk text through the llm service's `/embed` |
| `EmbeddingClient` | `contracts/retrieval.py` | Resolve query text to a validated model-specific vector |

`contracts/` imports nothing from `src/`. That dependency direction keeps the boundaries
honest: the domain types and Protocols don't know FastAPI, Postgres, or httpx exist.

### 1. `StorageAdapter` — persistence

Defines every persistence operation with identical semantics across implementations:
`create_doc`, `get_doc`, `get_doc_by_normalized_url`, `list_docs`, `update_doc`,
`add_tag`, `remove_tag`, grant operations, `upsert_doc_content`, `get_doc_content`,
`get_doc_content_meta`, `delete_doc_content`, `replace_doc_chunks`,
`replace_doc_chunks_if_current`, `clear_doc_chunks_if_unindexable`, `list_doc_chunks`,
`get_doc_index_state`,
`list_docs_for_indexing`, `search_chunks`, the two source reads,
API-key operations, and the readiness query. See `contracts/storage.py` for the
complete signatures.

- **`InMemoryStorageAdapter`** (`src/storage/in_memory.py`) — plain dicts, no I/O. Used by
  the fast suite via FastAPI's `dependency_overrides`.
- **`PostgresStorageAdapter`** (`src/storage/postgres.py`) — SQLAlchemy Core. Used in
  production and in the `RUN_PG_TESTS` integration suite, which runs the *same* behavioral
  assertions against a live database.

A divergence between the adapters is a bug; the shared seed (`build_seed_sources()` in
`tests/conftest.py`) and the mirrored Postgres tests exist to catch it.

### 2. `Fetcher` — content snapshots

A `Fetcher` returns a `FetchResult` (`title`, `content_snapshot`) or raises `FetchError`,
which is deliberately non-fatal at ingest: it becomes a warning and never blocks the doc.

- **`WebFetcher`** (`src/fetch/web.py`) — HTTP fetch (5s timeout, follows redirects), parses
  `<title>`, and keeps up to 2000 chars of stripped text as the snapshot. HTTP errors become
  `FetchError`.
- **`ConnectorsFetcher`** (`src/fetch/connectors.py`) — calls the connectors service's
  `POST /fetch`. Backs `github` (repository README or `/blob/` file, titled `owner/repo`)
  and the four Google sources; the snapshot is derived from the full content returned.

#### The `FetcherRegistry`

`FetcherRegistry` (`src/fetch/registry.py`) maps a `source_id` to a `Fetcher`.
`default_registry()` registers `WebFetcher` for `web` and one `ConnectorsFetcher` each for
`github`, `gdocs`, `gsheets`, `gslides`, and `gdrive` — exactly the sources with
`content_fetch_enabled`. Any other source raises `FetchUnsupported` (a
`FetchError`), which is how auth-gated sources like Google Drive "skip fetching".

### 3. `DirectoryClient` — ownership validation

`DirectoryClient` resolves an owning team/person id to a display label. Production uses
**`HttpDirectoryClient`** (`src/directory/http_client.py`), which calls team-tracking's
`GET /teams/{id}` and `GET /people/{id}` with the configured directory API key.

The crucial part is the **404-vs-unavailable** distinction:

- **HTTP 404** → the directory is up and has no such record → returns `None`. Callers treat
  this as a genuinely unknown owner (→ HTTP 400).
- **Connection failure or 5xx** → raises `DirectoryUnavailable`. Callers treat this as "can't
  tell right now" and **degrade** rather than reject.

So an owner id is rejected only when the directory positively doesn't know it.

## The ingest orchestrator

`src/ingest.py` (`ingest_doc`) is the heart of the service. It is framework-free — it takes
injected `storage`, `fetchers`, `directory`, and an `actor` string, so it is trivially
unit-testable with fakes. Five steps:

1. **Normalize + dedup.** Normalize the URL (`normalize_url`) and look it up by normalized
   form. If an **active** doc already exists, merge any new tags, and return
   `IngestResult(created=False, …)` — no duplicate. Google sources also refresh source grants
   and content when `content_fetch_enabled` is true; see "Source-derived access" below.
2. **Determine the source.** A caller-supplied `source_id` wins (and is validated —
   unknown → `BadReference`). Otherwise the source is derived from the URL
   (`derive_source`).
3. **Fetch, best-effort.** If the source has `content_fetch_enabled`, try the registry's
   fetcher; on `FetchError`, append a warning and carry on. If the source `requires_auth`,
   skip with a warning. If no title survives, fall back to the URL.
4. **Resolve ownership.** For each owner id, call the directory. Reachable + valid → label;
   reachable + unknown → `BadReference` (→ 400); unreachable → warning + null label
   (degrade).
5. **Persist** via `storage.create_doc`, returning `IngestResult(created=True, …)`.

`BadReference` means "the caller gave a bad id and we can prove it"; routers map it to
**HTTP 400**.

### Where degrade completes: label backfill

Degrade leaves a null label. In `src/api/routers/docs.py`, `_backfill_labels` runs on
`GET /docs/{id}` and re-resolves null owner labels (persisting them if the directory is back),
and `PATCH` re-resolves labels whenever an owner id changes. A doc ingested during an outage
heals on the next read or update.

## URL normalization + most-specific-source derivation

`src/url_norm.py` holds two pure, heavily tested helpers:

- **`normalize_url`** produces the dedup key: lowercase scheme/host, drop default port and
  fragment, strip tracking params (`utm_*`, `gclid`, `fbclid`, `ref`, …), sort the rest,
  strip trailing slash.
- **`derive_source`** picks the source. `url_patterns` are bare hosts (`github.com`) or host +
  path prefix (`docs.google.com/document`). A pattern matches when the URL host equals or is a
  subdomain of the pattern host **and** the path starts with its prefix. The **longest match
  wins**, so `docs.google.com/spreadsheets/…` resolves to `gsheets`, not `gdocs`/`web`. `web`
  (empty pattern) is the fallback after recognized Google file identities: Forms
  edit URLs without a matching pattern use the registered `gdrive` source.

## Data model

Tables are defined in `src/storage/schema.py`; their migration history is listed in
[`DEPLOYMENT.md`](DEPLOYMENT.md#migrations-alembic).

### `sources`

The registry of URL kinds. `id` is a slug primary key. Key columns: `label`, `url_patterns`
(text array driving `derive_source`), `requires_auth`, `has_api`, `content_fetch_enabled`,
`active`. Eight rows are seeded (see below).

### `docs`

The catalog itself. Key columns: `url`, `url_normalized` (indexed dedup key), `title`,
`source_id` (FK → `sources.id`, defaults `'web'`), `description`, the two
`owning_*_id` / `owning_*_label` pairs, `content_snapshot`, `fetched_at`,
`source_access_synced_at`, `source_access_attempted_at`, and `active`
(the soft-delete flag). Indexed on `url_normalized`, both owner ids, and `source_id`.

Migration `004` adds a **partial unique index** on `url_normalized WHERE active`, so the
database enforces dedup rather than only ingest's read-then-write. `WHERE active` lets a
soft-deleted doc's URL be catalogued again.

### `doc_grants`

Who may see a doc (migrations `003` and `007`). Ownership is metadata only. One row per grant:
`doc_id` (FK → `docs.id`, `ON DELETE CASCADE`), `grantee_type` (`person` / `team` /
`org`), `grantee_id` (UUID, **null for `org`**), and `origin` (`manual` or
`google_drive`). Source rows also retain permission id, normalized principal, role,
inherited parent/shared-drive ids, source expiry, and effective expiry. Effective expiry is
the earlier of Drive's permission expiration and the configured staleness deadline.

Four constraints/indexes carry the grant invariants:

- `ck_doc_grants_grantee_shape` — a CHECK enforcing that `org` grants have a null
  `grantee_id` while `person`/`team` grants have a non-null one. The same rule is
  validated in `contracts/types.py`, so a bad shape is a 422 long before it reaches the DB;
  the CHECK is the backstop.
- `uq_doc_grants_manual_grantee` — partial uniqueness for manual person/team grants.
- `uq_doc_grants_org` — a *partial* unique index on manual org grants.
  It exists because the constraint above **cannot** catch duplicate org grants: their
  `grantee_id` is NULL, and in SQL `NULL != NULL`, so two identical org rows don't collide.
  Without this index, "share with the org" twice would insert two rows.
- `uq_doc_grants_source_permission` — preserves one resolved row per source permission and
  grantee while allowing a manual row for that same grantee to coexist.

Grantee ids are **not** foreign keys: people and teams live in team-tracking's database,
which this service never touches directly. Labels are resolved over HTTP at the API layer
and never stored on the grant.

### Source ACL reconciliation

Google connector responses carry content and a complete Drive permission list. The
connector reads that list before any export/download. documentation-system then resolves
`user` entries through primary or verified `email` identities and `group` entries only
through Team Tracking's fail-closed, membership-synced Google Group lookup. It never
infers Google Group membership from an ordinary Team Tracking membership.

Unknown/external emails, unmapped/legacy groups, domain-wide shares, and `anyone` shares
produce warnings but no grant. This deliberately means a broadly shared file can remain
absent from the catalog's source-backed audience until an explicit supported mapping
exists. Email verification identifies the UTMIST person; the permission returned by
Google is the separate evidence that the Google principal is actually entitled.

`replace_source_grants` is atomic per document and origin. Postgres locks the document row
before deleting grants, holding the lock through insertion and timestamp updates so concurrent
replacements leave one complete ACL snapshot, even when the prior grant set is empty.
A successful empty result is a revocation; a connector/directory failure keeps the last
complete result. Source grants
expire after the configured maximum age, so a prolonged refresh outage fails closed.
Manual grants are independent and are never silently created or deleted by source
reconciliation. Ownership fields are attribution metadata and do not authorize access.

`doc-source-refresh` is an external HTTP client of the bounded refresh endpoint.
It keeps database/provider credentials in the API, checks its scoped key before
refreshing, and never replays an ambiguous POST. Separate refresh and index failure
counts preserve the distinction between source freshness and searchable content.
Railway failure notifications and independent start/finish heartbeat pings cover
process failures and missing runs; monitoring failure does not undo or replay API
work. Deployment, recovery and monitoring setup live in the
[refresh runbook](DEPLOYMENT.md#source-access-refresh-schedule).

Before its first heartbeat, the runner acquires the database's singleton
`source_refresh_run` row through authenticated HTTP. The guard covers the whole
drain and final heartbeat, across containers and API restarts. A conflicting
runner exits nonzero and sends only `/fail`; it cannot replace the active start
or send a misleading success. Run IDs alone only correlate Healthchecks durations.
The guard has no TTL: after a crash or uncertain completion, admitting a new run
could overlap work still running in the API. An operator must confirm quiescence
before releasing that exact ID. The bounded refresh endpoint remains independently
callable; direct API clients must use the same guard protocol for an entire drain.
Each catalog database must have its own heartbeat check.

Visibility keeps two team sets in the actor context: ordinary active directory memberships
for manual grants, and separately confirmed synchronized Google memberships for
source-derived team grants. This prevents a newly added directory member from inheriting a
Drive document before #237 has actually added them to the Google Group.

### `doc_tags`

One row per (doc, tag). `UniqueConstraint(doc_id, tag)` makes tagging idempotent;
`ON DELETE CASCADE` cleans up with the doc. Tags are stored lowercased/trimmed.

### `api_keys`

Auth store: `name` (unique), `prefix` (unique, the lookup key), `key_hash` (Argon2), `scopes`
(text array), `active`, `revoked_at`, `last_used_at`. The plaintext key is never stored.

Docs, sources, and API keys carry the audit quartet `created_at` / `updated_at` /
`created_by` / `updated_by`. Derived content and chunks follow document identity
and content provenance.

### `doc_content`

Full extracted text lives in a separate 1:1 row keyed to `docs.id`, with a SHA-256
`content_hash`, `fetched_at`, and `updated_at` (migration `005`). Missing content
has no row. Both content reads and metadata reads enforce document visibility.

### `doc_chunks`

Migration `008` enables pgvector and adds embedded chunks keyed by `(doc_id, ordinal)`.
The document FK cascades on hard deletion. Each row stores `chunk_text`, half-open
`start_offset` / `end_offset` **Unicode code-point** indices into the source text,
`source_content_hash`, `embedding vector(1536)`, `embedding_model`, and `dimensions`.
Migration `010` adds nullable `source_title_hash`: SHA-256 of the title (None treated
as empty). It records embedding context separately from the body hash used by citations.
Existing chunks retain NULL provenance until the normal stale-index pass rebuilds them.
The source hash uses `src/content.py`'s SHA-256 convention; it identifies the text
version whose offsets were embedded, even if `doc_content` is later replaced.

The initial profile from #173 is `text-embedding-3-small`, 1536 dimensions. Store
the resolved `/embed` response model, not the `openai-embed-3-small` config alias.
`DocChunk` in `contracts/types.py` validates the width, finite float32 values,
nonblank text/model, hash, and offset span. Its text limit (`MAX_CHUNK_CHARS`) is
16,384 Unicode code points: well above the approximate 2,000-character target,
but conservative enough to keep generated `tsvector` metadata below Postgres's
size limit, including multibyte text. Oversized chunks raise `ValueError` before
either adapter accesses storage. Both adapters round vectors to float32 and return
immutable, hashable chunk values with tuple embeddings; JSON still uses arrays.
A dimension change needs a schema migration;
model compatibility across documents and re-embedding remain #209's responsibility.

`replace_doc_chunks(doc_id, chunks)` validates a complete set before storage access:
ordinals are exactly `0..n-1`, with one model and source hash per set. Input order
does not matter. Replacement is atomic and idempotent; `[]` clears the set and an
unknown doc returns `False`. Postgres locks the parent doc before deleting/inserting,
including the first write, so concurrent replacements cannot interleave chunk sets.
The in-memory adapter validates before replacing its dict entry. Both reject invalid
sets with `ValueError` and preserve the previous set on failure.

`list_doc_chunks` returns detached values in ordinal order and applies the same
visibility predicate as `get_doc_content`. Missing, invisible, and unindexed docs
all return `[]`. Like content reads, it does not filter the document's `active` flag.
`replace_doc_chunks` records supplied provenance without comparing it to current
content; the indexer uses `replace_doc_chunks_if_current` instead (see
[Indexing](#indexing-175-srcindexingpy)). #176 must apply active/authorization
predicates before ranking.

`search_vector` is a stored generated `tsvector` from the same `chunk_text`, with a
GIN index. It uses the explicit `simple` text-search configuration to preserve names
and acronyms without stemming or stop-word removal. Future keyword queries must use
that same configuration. Authorized retrieval applies explicit portable token
normalization before `simple` parsing; see [Authorized retrieval](#authorized-retrieval).
See [Index type](#index-type) for why vectors initially use exact scans.

### Seeded sources

| id | label | patterns | requires_auth | content_fetch |
|----|-------|----------|:---:|:---:|
| `web` | Web page | (none) | no | **yes** |
| `github` | GitHub | `github.com` | no | **yes** |
| `gdrive` | Google Drive | `drive.google.com` | yes | **yes** |
| `gdocs` | Google Docs | `docs.google.com/document` | yes | **yes** |
| `gsheets` | Google Sheets | `docs.google.com/spreadsheets` | yes | **yes** |
| `gslides` | Google Slides | `docs.google.com/presentation` | yes | **yes** |
| `youtube` | YouTube | `youtube.com`, `youtu.be` | no | no |

## Auth

Level 2 (scoped API keys), as in team-tracking. The machinery lives in the shared leaf package
`platform_auth` (`packages/auth/`), which imports no service's `src/` or `contracts/`.
`src/api/auth.py` and `src/api/hashing.py` are ~15-line shims that call its `build_auth(...)`
with this service's `doc_` key envelope and default config (no dev-spoof affordance, unlike
team-tracking) and re-export the same names:

- **`hashing.py`** (shim) — key format `doc_<prefix>_<secret>` (8-char prefix), Argon2
  hashing, `parse_prefix` to extract the lookup prefix.
- **`auth.py`** (shim) — `require_api_key` looks the key up by prefix, verifies the hash, and
  checks `active` / not-revoked; the bootstrap env key is accepted as scope `admin`.
  `require_scope(scope)` gates each route; `admin` is a wildcard. `get_actor` returns the
  key's own name — the **attested actor** (no `X-Actor` header, no impersonation).
- **`AuditLogMiddleware`** — one structured JSON log line per request (method, path, status,
  duration, key name from `request.state.auth_key`, remote IP); never fails the request.
  `app.py` imports it from `platform_auth` directly, as it binds nothing per-service.

## Visibility: the second authorization layer

Scopes answer "may this key call this endpoint"; visibility answers "which *rows* may it
see". A request must pass both.

**One definition, two implementations.** `contracts/visibility.py` holds `doc_visible()`
— a pure function over actor context and grants (owning ids never authorize access).
The in-memory adapter calls
it directly; the Postgres adapter compiles the *same* rule into SQL so filtering happens
in the database rather than in Python over a full table scan. That's a genuine duplication
of logic, and it's held in lockstep by parity tests (`tests/test_visibility.py` plus the
adapter parity suite). **If you change the rule, change both and extend those tests** —
this is the file where a divergence becomes a silent data leak.

**The direct-document actor context** is one of three things (`src/api/authz.py` builds it):

| Context | When | Meaning |
|---|---|---|
| `SEE_ALL` | a `docs:read:all`/`admin` key with no `X-On-Behalf-Of`; or *any* write key with no `X-On-Behalf-Of` | every doc |
| `DENY` | a plain `docs:read` key with no `X-On-Behalf-Of` | **no docs at all** |
| `Actor(person_id, team_ids, source_team_ids)` | `X-On-Behalf-Of: <uuid>` present | that person's directory and confirmed Google-group view |

`DENY` is deliberate: a bare `docs:read` key carries no identity, so it has no basis for
seeing anything; consumers must say *who* they act for. Reads on behalf of someone still
**require** a read scope, so a write-only key can't read through that door.

An `Actor` sees a doc only if a valid grant matches (`org` / their `person` id / one
of their teams). Ownership remains available for attribution and filtering.

**Team ids are resolved live** from the directory (`get_active_team_ids`). If the
directory is unreachable the set is treated as **empty** rather than failing the request —
a partial fail-closed. Person-granted and `org`-granted docs still resolve; team-granted
ones are withheld. Withholding is the safe direction: an outage can hide a doc, never
expose one.

**Invisible reads 404, not 403.** `get_visible_doc_or_404` guards read *and* write routes,
so a caller can't probe for docs they may not see via status codes.

## Retrieval layer: chunking + search strategy (RAG)

> **Status: spike / decision record** for the RAG epic (#125), fixing the parameters the
> indexing pipeline (#175) and retrieval endpoint (#176) build on. The numbers are *starting*
> values for the evaluation set (#178) to validate; when #178 moves one, update it here.

**Decision (#208, [PR #219](https://github.com/UTMIST/Misty/pull/219)): ~500-token chunks,
~50-token overlap (10%), and hybrid keyword/vector retrieval.** Character equivalents below
are estimates for the sampled text, not alternative fixed-character defaults.

### What we are chunking

The pipeline chunks the **stored plain text of a catalog entity**, not a URL-document:
`doc_content.content_text` (capped at `MAX_CONTENT_CHARS` = 1,000,000, see `src/content.py`,
populated during ingest), not the shorter `docs.content_snapshot`. Keying on the entity is
deliberate: meeting records will be catalogued entities with no source URL, and a pipeline
built around URL-fetched docs would silently exclude them.

Stored text is already source-specific. Current shapes (all from the Google connector,
`services/connectors/`):

| Source | Extracted shape | Chunking implication |
|--------|-----------------|----------------------|
| Google Docs | Markdown headings, bullets, links, and tables; cell paragraphs joined with spaces | Respect headings, paragraphs, and table rows where they fit |
| Google Slides | `## Slide N`, title/body text, tables, and speaker notes | Preserve slide boundaries where they fit; table-cell newlines become `<br>` |
| Google Sheets | Per-tab headings and Markdown tables of `FORMATTED_VALUE` rows, row-capped; cell newlines become `<br>` | Split per tab, then per row: each row is one line |
| PDF (Drive) | `## Page N` boundaries with extracted page text; internal structure can be lossy | Respect pages where they fit, then use recursive splitting |
| DOCX (Drive) | Markdown headings, bullets, and tables in document order | Respect rendered structure where it fits; table-cell newlines become `<br>` |
| GitHub READMEs (#86) | Markdown (future) | Split on headings then paragraphs |

### Chunk size and overlap

The spike sampled real UTMIST Google Docs (GitHub standards, transition-report templates,
Senior Exec meeting notes, a single-link stub) of ~26 to ~3,421 tokens. Transition-report
exports contained literal `&#10;` cell breaks: the Technical Writing report's longest line was
2,189 characters (~547 tokens), 357 once decoded, with 74 occurrences and similar templates in
~9–10 departments. These are historical export observations, not current connector output.

**Split the stored representation, not an assumed export format.** The Docs extractor joins
cell paragraphs with spaces (no `&#10;`); the Markdown renderer encodes Slides/DOCX cell
newlines as `<br>` (`services/connectors/src/sources/google_extractors/`). Never globally
decode entity-like text: it may be literal content, and changing its length breaks source
offsets. Any normalization needs source-specific rules and a mapping back to
`doc_content.content_text`.

Starting parameters:

- **Chunk size: ~500 tokens (~2,000 characters at ~4 chars/token).**
- **Overlap: ~50 tokens (~200 characters, 10%).**
- **Boundaries first, size second:** markdown headers → top-level list items → paragraph
  breaks → sentences → words, falling through only when a unit is still oversized.

Why 500: at 300 tokens, longer documents became 11–16 chunks and table-cell blobs or single
list answers were split more often; at 800, they shrank to 5–6 chunks that merged unrelated
department or role blocks. At 500 a chunk holds a `ROLE: X` table block or a department status
block while staying narrow, and short documents stay at 1–4 chunks.

The 10% overlap preserves continuity across row or list-item boundaries at negligible cost
for this corpus. Four characters per token is a proxy, not a guarantee: validate counting
against #173's model and input limits, and boundaries against current connector fixtures.

### Hybrid vs pure vector

**Decision: hybrid retrieval — pgvector cosine similarity plus Postgres full-text search
(`tsvector`/`tsquery`, optionally `pg_trgm` for fuzzy acronym/name matches), merged with
something like Reciprocal Rank Fusion — rather than pure vector search.**

Sampling ~25 Google Docs from targeted Drive folders (2026–2027 departmental storage,
2025–2026 transition reports) suggests **low hundreds of documents**, plausibly
**1,000–5,000 chunks** — well within Postgres full-text search, no dedicated service needed.

The corpus is dense with exact-match terms embeddings may blur: UTMIST acronyms (`CPSIF`,
`MLF`, `deMISTify`, `CUCAI`, `EigenAI`), names, Discord handles, emails, dollar figures, and
dates. Lexical search suits queries like "who do I email about CPSIF" and helps separate
similar but distinct events such as `AI²`, `GenAI Genesis`, and `Academic Journal Workshops`.

It fits #174's storage: vectors live in Postgres, and a GIN-indexed `tsvector` column keeps
both paths in-stack without Elasticsearch/OpenSearch. Revisit if the corpus grows ~10x or
hybrid can't fix measured latency or relevance problems.

### Index type

`vector(1536)` with **no ANN index (exact scan) to start.** At a few thousand chunks an exact
nearest-neighbor scan provides a simple 100%-recall baseline for #178. Introduce an **HNSW**
index (pgvector >= 0.5) only once the corpus crosses roughly 10k chunks and exact scans start
to cost materially. This applies to the vector branch only; the keyword branch uses the GIN
index described above.

### Retrieval constraints and open decisions

- **Relevance floor: absolute cosine, implemented in #176; measured on #178.**
  `SEARCH_MIN_COSINE` starts at 0.3. Complete keyword matches qualify independently;
  RRF ranks never determine eligibility. #178's `calibrate` mode measures how well
  cosine separates correct documents from incorrect ones. Against its fixture the two
  distributions **overlap across their whole range**, so no floor separates them and
  moving the threshold trades recall against precision with no sweet spot. That
  measurement uses a lexical stand-in embedder, so it bounds what the floor can do
  structurally rather than settling the production value; re-measure against real
  embeddings before changing `SEARCH_MIN_COSINE`. See [EVALUATION.md](EVALUATION.md).
- **Visibility follows the explicit person or channel context in #77/#176.** Direct search
  and DMs preserve the person's access. Shared-channel answers use the configured teams'
  Google Group entitlement (#238 maps channels to teams) — never the asker's private grants
  or a model-supplied team list. Extend grant semantics; the person-only `Actor` / `SEE_ALL` /
  `DENY` types are not a channel contract. Resolve channel scope via the authenticated
  directory API; missing config or unavailable scope must not broaden access. Compile the
  predicate *into both* vector and keyword queries before top-k and fusion; never filter after
  ranking, and allow no unfiltered variant.

### Spike methodology and limitations

A small local Python harness simulated recursive splitting on real Google Docs from the
connector's Drive corpus and measured character/line counts; no document text was committed or
persisted. It measured no embeddings or retrieval and does not replace #178's hit-rate@k
evaluation.

Limitations:

- **No exact corpus count.** The low-hundreds estimate comes from targeted sampling, not a
  full walk of the four academic-year trees. #175 should record the real count on first full
  ingest and revisit this decision if it differs by an order of magnitude.
- **Google Docs only.** Sheets, Slides, PDFs, GitHub, and future sources were not validated;
  don't assume the results generalize to GitHub (#86).
- **No retrieval-quality measurement.** Hit-rate needs #173's embedding model and belongs to
  #178.
- **Use extracted text length, not Drive file size.** One 884KB Drive file extracted to ~14KB
  of text.
- **Large-document extraction can truncate.** The tool cut off `Senior Exec Meeting #10`
  mid-sentence, so the largest documents may be bigger than measured.

### Chunker (#218): `src/chunking.py`

A pure function shaped like `src/content.py`: text in, chunks out, no I/O, and no knowledge
of URLs, connectors, storage, embeddings, or access. The caller passes
`doc_content.content_text`.

```python
from src.chunking import ChunkSettings, chunk_text

result = chunk_text(content_text)                     # defaults: 500 / 50
result = chunk_text(content_text, ChunkSettings(target_tokens=300, overlap_tokens=30))
for c in result.chunks:
    c.ordinal, c.start, c.end, c.text, c.token_count  # core
    c.heading_path, c.section_start, c.section_end    # structure
    c.locator, c.start_boundary, c.end_boundary       # citation / diagnostics
    c.overlap_prev, c.chunk_hash
result.chunker_version, result.settings, result.counting
```

**Offsets.** Python string indices (code points), half-open `[start, end)`, into the exact
input; `chunk.text == content_text[chunk.start:chunk.end]` always holds. Nothing is
normalized, decoded (`&#10;` stays literal), or prefixed; surrounding whitespace is excluded
by moving offsets, not editing text. Other languages must convert (JavaScript indexes UTF-16
units, so astral characters such as emoji count twice).

**Token counting.** `estimate_tokens` = `ceil(len(text) / 4)` (`result.counting ==
"chars/4"`). An estimate, but 500 tokens is ~2,000 characters, 16x inside `/embed`'s
32,000-character cap (#173), so error cannot breach the model limit. Inject a real tokenizer
via `count_tokens=`; `tiktoken` isn't a dependency because it downloads its encoding at
runtime, which would break the offline suite. A counter must not decrease as text grows and
must fit any single character in budget (else `ValueError`); pass `counting=` to label it
(required for a lambda or `functools.partial`, whose names say nothing).

**Settings.** `target_tokens > 0` and `0 <= overlap_tokens < target_tokens`, else `ValueError`.
Every chunk's `token_count` is `<= target_tokens`, including hard-cut ones.

**Splitting.** Two passes:

1. *Split* recursively into the coarsest pieces that fit, descending a level only for a piece
   still over budget: `heading` (ATX `#`..`######` at line start, outside fenced code;
   covers Docs headings, Slides `## Slide N`, PDF `## Page N`, and Sheets `## <tab>`) →
   `list` (top-level `-`, `*`, `+`, `1.`) → `paragraph` (blank line) → `line` → `sentence`
   (`.!?` + whitespace, or `。！？`; not after a common abbreviation or initial, or before a
   lowercase word) → `word` → `hard` (character cut at an extended grapheme cluster boundary,
   UAX #29 via the `regex` package's `\X`, so a written character is never split unless a
   single cluster is over budget on its own).
2. *Pack* pieces greedily up to the budget. Whole small sections pack together, but a chunk
   starting mid-section ends at the next heading, and one that would end partway into an
   absorbed section closes before its heading. Otherwise an overflowing chunk prefers a
   coarser boundary in the last 20% of its budget.

**Overlap.** The next chunk starts in the previous chunk's tail at the earliest boundary whose
tail fits `overlap_tokens`, preferring whole trailing pieces, then sentence ends, then words.
It never crosses back over a heading or starts inside a heading line, and is
dropped if it would overflow the chunk (so whitespace-free text like CJK may get none).
`overlap_prev` counts the repeated characters for duplicate-free stitching.

**No CSV rules.** Every input is treated as Markdown; quotes carry no meaning. Sheets reach
the chunker as Markdown tables (the extractor renders them like Docs/Slides/DOCX tables, cell
newlines as `<br>`), so a row is one line and heading-like text inside a cell is never at a line
start. Sheets content stored before that extractor change is CSV; refetch it before indexing.

**Metadata.**

| Field | Meaning | Intended use |
|-------|---------|--------------|
| `heading_path` | Headings enclosing the whole chunk, outermost first; `()` if none (preamble, or a chunk packing top-level sections) | Embedding/BM25 context prefix (title + path + text) and citations, added *at index time*, never into `text` |
| `section_start` / `section_end` | Innermost section enclosing the whole chunk (the preamble is its own; else the whole text) | Parent expansion: retrieve the precise chunk, hand the model its whole section |
| `locator` | `("slide", n)` / `("page", n)` where the chunk starts, inherited by subheadings | Citations like "slide 4" |
| `start_boundary` / `end_boundary` | One of the levels above, or `start`/`end` | #178 diagnostics: how often chunks are cut at `word`/`hard` |
| `overlap_prev` | Characters repeated from the previous chunk | De-duplicating stitched neighbours |
| `chunk_hash` | `content_hash(text)` (sha256, the `src/content.py` convention) | Re-embed only chunks whose text changed |
| `chunker_version` | `CHUNKER_VERSION` on the result | Re-chunk output of an older splitter |

Document-level, temporal, and permission metadata (doc id, fetch provenance, embedding model,
grants) belong to indexing (#175) and retrieval (#176).

**Changing it.** Bump `CHUNKER_VERSION` in the same PR as any change that can alter output for
the same input and settings. `tests/test_chunking.py` asserts the invariants above on fixed
cases (connector shapes, Unicode, repeated text, boundaries) and 200 seeded random inputs.

### Carrying this into implementation

- **#218 (pure chunker):** done; see "Chunker (#218)" above.
- **#174 (storage):** store vectors and keyword-search metadata for the same chunks.
- **#175 (indexing/lifecycle):** done; see "Indexing (#175)" below.
- **#176 (retrieval):** implemented; see "Authorized retrieval" below. Evaluation (#178)
  and consumer integration (#278) remain release gates. #277's automated lifecycle
  verification and controlled staging gate are tracked in the
  [integration audit](INGESTION-RETRIEVAL-AUDIT.md).
- **#178 (evaluation):** built; see "Evaluation (#178)" below and
  [EVALUATION.md](EVALUATION.md). The question set covers exact-name, acronym and date
  queries alongside buried and boundary-straddling answers. First finding: at the
  starting floor the relevance threshold, not the chunk size, dominates the score.

### Indexing (#175): `src/indexing.py`

`index_doc(doc_id, storage=, embedder=, force=False, settings=ChunkSettings())` makes one
doc's chunk set match its
stored `doc_content` and title. It is keyed on catalog identity and stored text, never on a URL or a
fetch, so anything catalogued with stored text (future meetings, #211) indexes the same way.

1. A missing or retired doc, or one with no content, has its chunks cleared (`cleared`).
   `clear_doc_chunks_if_unindexable` rechecks under the chunk writers' lock; if a newer
   fetch or reactivation made it indexable, cleanup writes nothing (`skipped`).
2. The body and title hashes are compared with `get_doc_index_state`. Matching hashes,
   existing chunks, and no `force` → `unchanged`: no embedding call or write. NULL title
   provenance from an older chunk set is stale, even for a doc with no title.
3. `chunk_text` splits the text. Each chunk is embedded as `title \n heading > path \n\n
   text` — the context prefix the chunker section calls for — while `chunk_text` stores only
   the raw slice, so offsets still index the source. The added title/heading context
   is capped at 1,000 characters, keeping long headings from overwhelming the input;
   source chunks are never truncated by this step.
4. The whole set is built in memory, then written with
   `replace_doc_chunks_if_current(doc_id, hash, chunks, expected_title_hash=...)`. In one
   transaction it locks the doc and content rows, checks the doc is active and both
   body/title hashes still match the embedding inputs, and replaces the set. Shorter text therefore never leaves tail chunks.

**Failure and retry.** Embedding errors (`EmbedderUnavailable`: connection, 429, 5xx;
`EmbedderRejected`: 4xx, malformed or wrong-width vectors) and storage errors return
`failed` with a sanitized reason, never raise, and write nothing: the previous set stays
whole, never a mix. The catalog write that triggered indexing is kept. Retrying is calling
`index_doc` again, which is what `POST /docs/index` does.

**Races.** If content or title changes while a doc is being embedded, the conditional
write is refused (`skipped`); the newer writer runs its own index. Postgres locks
the `doc_content` row because `upsert_doc_content` never touches `docs`.
Source-deletion cleanup also compares the content hash and capture time read before
fetching, under the doc and content row locks. A delayed connector 404 cannot delete a
newer successful fetch, including one that stored identical text. The catalog row stays
in all cases.

**Triggers** — all inline; there is no worker or scheduler inside the catalog:

| Event | Effect |
|-------|--------|
| `POST /docs` (new or repeated ingest) | `index_doc`; a failure becomes an ingest warning |
| `POST /docs/{id}/refetch`, `refresh-due` | `index_doc` after a successful fetch; failures are logged / counted in `index_failed` |
| `PATCH active=false` / `true` | `index_doc` clears / rebuilds |
| `PATCH title` | `index_doc` refreshes title context, including title removal; an identical title makes no embedding call |
| Connectors `404 file not found for this url` (`SourceGone`) | `delete_doc_content`: text and chunks go together, so nothing rebuilds them. A later successful fetch stores and indexes again |
| Any other fetch failure (timeout, 5xx, 403, an unrecognized 404) | Nothing: a transient failure is not a deletion |
| `POST /docs/index` (`docs:index`) | Admin retry and #209's re-embed entry point (`stale_only=false, force=true`) |

`list_docs_for_indexing` finds work for the admin route: active docs whose chunks are
missing, built from older body/title hashes, or missing title provenance, plus docs
that still have chunks but should not
(retired, or no content). It pages by doc id so a doc that keeps failing cannot block the
rest. A changed embedding model is not "stale" to it: the stored model is the provider's
resolved id, not the configured alias, so #209 forces a pass instead.

`HttpEmbedder` (`src/embed/http_client.py`) splits inputs into `/embed` requests of at most
96 inputs and `EMBED_MAX_REQUEST_CHARS` characters (mirrors of the llm service's limits),
orders vectors by their `index`, and rejects any width other than the 1536-dimension column.
Before the first request, it validates **every** input: nonblank UTF-8 text, at most
32,000 characters, and no single input larger than `EMBED_MAX_REQUEST_CHARS`. A known
invalid input in a later batch therefore cannot waste paid work on earlier batches.
These are character guards, not an exact provider token count.
Revoking a doc's source access (zero mapped grants) keeps its chunks, as it keeps its
content; retrieval (#176) applies visibility.

Indexing uses `SEARCH_EMBED_MODEL` unless `EMBED_MODEL` explicitly overrides it. Keep
the two aligned: retrieval excludes chunks produced by another model, even at the same
vector width. An override supports a planned re-embedding pass, not mixed-model search.

### Evaluation (#178): `src/eval/`

A local harness that seeds a fixed corpus at a given chunk configuration, asks a fixed
question set, and reports `hit@k` with the configuration attached. Full usage, fixture
format and limitations: **[EVALUATION.md](EVALUATION.md)**.

Three decisions worth knowing here:

- **It drives the real `index_doc`,** not a copy of the pipeline, so what it measures
  includes `embedding_input`'s title and heading context. `index_doc` takes `settings`
  for exactly this reason; every caller in the service uses the default.
- **It embeds with a deterministic lexical stand-in** (`src/eval/embedding.py`), since
  #178 requires automated tests to avoid paid provider calls. Cosine tracks vocabulary
  overlap, which makes the score respond to chunking and makes it useless as a claim
  about production retrieval quality.
- **A leak is scored apart from the hit rate.** Returning a document the asker may not
  see is an authorization failure, not a ranking one, and a single averaged number
  would hide it.

Equal scores across configurations are a valid outcome and are reported as measured.

## Authorized retrieval

`POST /search` uses the [shared API contract and fixture](API.md#authorized-retrieval-post-search).
The fixture is the consumer handoff for #71/#210 and the shared offline/Postgres
adapter test source. `tests/ingestion_retrieval_cases.py` additionally creates its
documents through `POST /docs`, runs real chunking/embedding/storage integration,
and executes the same HTTP exchanges. External responses use MockTransport;
the dependency factories, HTTP clients, authorization and ranking remain real.
The same lifecycle cases run on both adapters; additional Postgres cases inject
SQL faults to prove rollback and safe retrieval errors. See the
[#277 integration audit](INGESTION-RETRIEVAL-AUDIT.md) for coverage and evidence.
Consumer-owner agreement, controlled staging/provider acceptance, and #278 remain
separate release gates.

The request body controls only query and k. Trusted headers select either an
`Actor` or a `Channel(source_team_ids)`; literal assertion scopes are required,
including for admins. Channel context is created only by search. Existing direct
document routes continue to construct their existing person/sentinel contexts.
`StorageAdapter.search_chunks` requires a person or channel and rejects `SEE_ALL`,
`DENY`, and missing context before opening a database connection.

Person retrieval resolves both directory membership and confirmed source-group
membership live, retaining the complete grant rule. Lookup failure returns 503
instead of partial scope. Channel retrieval calls authenticated
`/channels/{guild}/{channel}/teams` and `/teams/google-groups`, intersects active
configured teams with healthy synced groups, and permits only unexpired
`google_drive` team grants. It never includes private shares, manual team grants,
or org grants. Empty configuration yields no candidates and no embedding call.
Neither resolved scope nor results are cached, so grant removal, expiration,
membership changes, channel reconfiguration, and group sync failure affect the
next request.

The Postgres adapter runs one SQL statement: a materialized eligible-chunk CTE
applies current visibility, document active flag, and exact embedding model/width
before either candidate path sorts and limits. Exact pgvector cosine and keyword
density each contribute at most `max(50,5*k)` candidates. Cosine is rounded to six
decimals to keep float32 implementations stable at ranking ties. Each branch
breaks ties by document UUID and ordinal. RRF with constant 60 combines their
one-based ranks; final ties use that same identity. Citations carry source URLs,
document IDs, original chunk offsets, and source hashes; no embedding or grant
metadata crosses the response boundary.

**Keyword portability trade-off.** Both adapters tokenize ASCII letter/digit runs
and lowercase them. All query terms must occur; density is matching occurrences
divided by all occurrences. Postgres uses `to_tsvector('simple', normalized_text)`
and `plainto_tsquery('simple', normalized_query)`. Normalization separates email
components and hyphenated dates identically offline and in SQL. Unicode-only
queries use vectors. Runtime normalization currently scans the eligible corpus
and does **not** use #174's raw-text GIN index: the raw Postgres parser treats
emails and hyphens differently from these portable tokens. At the small starting
corpus this is deliberate. A future indexed normalized column must preserve
these semantics and pass the same parity cases; do not silently substitute the
raw index. No schema migration is needed for retrieval.

Query embeddings use an `EmbeddingClient` Protocol, wired to authenticated HTTP
`/embed` in `deps.py`. The adapter checks response indices, exact resolved model,
width, finite nonzero float32 values, and duplicate JSON fields before querying
storage. Equal vector widths never make models interchangeable. Both candidate
paths exclude stored chunks from other models. HTTP clients are pooled; calls
are synchronous worker-thread operations, with timeouts and no retries.
Dependency errors never trigger broader or keyword-only fallback.

## SSRF protection in the web fetcher

Ingest fetches caller-supplied URLs, a textbook SSRF sink: unguarded, `POST /docs` would
proxy into Railway's private network or a cloud metadata endpoint.

The guard (`src/fetch/web.py`) resolves the hostname first and **pins the connection to the
validated IP**, so httpx never re-resolves — closing the DNS-rebinding window between check
and connect. It rejects private, loopback, link-local, and **carrier-grade-NAT**
(`100.64.0.0/10`) ranges; the last matters because `ipaddress.is_private` doesn't cover RFC
6598. Auto-redirects are off: each hop is validated and followed manually up to a cap, so a
public URL that 302s to `169.254.169.254` is still blocked. Resolver failures are errors, not
passes, and malformed URLs (or redirect `Location`s) surface as `FetchError`s, not 500s.

There is deliberately no hostname allowlist: any public URL is fetchable, since the catalog's
job is cataloguing the open web.

## Wiring: `src/api/deps.py`

The one place concrete adapters meet their Protocols. `get_storage` builds a
`PostgresStorageAdapter` over a pooled engine; `get_fetchers` returns `default_registry()`;
`get_directory` builds an `HttpDirectoryClient` from settings; `get_embedder` builds a cached
`HttpEmbedder` (the test suite's autouse `fake_embedder` fixture replaces it with an
offline fake). Tests override `get_storage`
(and inject fake fetchers/directory) via `app.dependency_overrides` — which is why these are
injected functions, not module globals.

## Automatic discovery (#291)

`doc-discover` is an external HTTP runner, checkpointed on its own volume. It calls
connectors' bounded metadata enumeration and the catalog's `/docs/discovery` import
endpoint. It never connects to the catalog database or imports another service's
code. Listing metadata is not ACL evidence: imports reuse `ingest_doc` and `index_doc`,
so grants still come only from each file's complete, resolved source ACL.

`StorageAdapter.get_doc_by_google_file_id` matches supported URL aliases in both
adapters, including old manual records and editor `u/<number>/d/` URLs. New normalized
Google keys collapse to a
canonical Drive file URL, reusing the active-URL uniqueness constraint for concurrent
new ingests. Published `/d/e/` editor/Forms links retain their distinct URL keys:
publication ids are not Drive file identities. No migration or rewriting of existing
keys/grants is needed. Legacy
alias lookup currently evaluates URL expressions in Postgres; measure catalog growth
before introducing an indexed identity column and its duplicate-data migration.

Discovery imports new identities and reports existing content/index gaps. It also
corrects active legacy Google identities with source `web` to the enabled Google
source supplied by discovery, retaining their id and manual grants. No content or
ACL fetch occurs in this correction: #292 continues to own refresh. Records lacking
an initial Google ACL sync report `registered` even if old web chunks exist. Retired
records stay retired. Complete or incomplete
listings never remove records, chunks or grants. The [runbook](DISCOVERY.md) owns the
checkpoint, scheduling, recovery and topology contract.

All Google inserts take a transaction-scoped Postgres advisory lock derived from the
file id (a process lock in memory). Discovery's `create_doc(skip_existing_google=True)`
then checks active and retired aliases in that same transaction before inserting.
An existing identity raises `ExistingGoogleDoc`; ingest returns it without applying
the in-flight fetch's content or grants. This closes the registration/retirement race
during fetching, including aliases that do not share a normalized URL. Retirement
preserves rows, so it cannot invalidate the existence check. Manual re-ingest retains
its existing semantics. Provider and directory calls finish before this short lock;
it never spans network fetching or embedding.

## Durable meeting records

`contracts/meeting.py` defines the hand-off, metadata and injected writer.
`meeting_records.py` orchestrates the leased state machine from the
[shared design](../../../docs/MEETING-RECORDING.md#durable-records-design-212).
Migration 012 follows the refresh-runner migration 011. Both storage adapters
implement atomic replay checks, version-checked transitions and catalog commits;
`catalog_meeting` commits the Doc, source grants, content and saved state together.
Migration 013 adds a durable `index_pending` flag.
Catalog acceptance schedules indexing in the same transaction; the coordinator
immediately runs the existing conditional indexer with an injected embedder.
Failures retry through `retry-due` without rewriting, resharing or refetching the Doc.
Removal invalidates attempts before erasing content. The canonical Google ID is
never replaced, and the pending transcript clears only after adoption commits.
Migration 014 adds the `google_deleted_at` receipt. Successful canonical deletion
is recorded before later cleanup or the final `removed` transition, so retries
skip a file already confirmed deleted. An
unacknowledged Google 404 remains ambiguous; it never creates a deletion receipt.

Migration 015 records every known removed Google identity. `purge_meeting_catalog`
uses the Google insertion locks to persist those fences and purge all matching
catalog aliases, including ordinary ingests that never set `record.doc_id`.
An insert already holding the lock commits before this purge; later inserts see
the removal fence.
Reconciliation applies the same purge to late duplicates and old tombstones,
including when the owner search returns no files. Known identities remain fenced
even when no catalog row existed at removal time.

Meeting content is fetched with the writer OAuth identity in initial ingest,
refetch and scheduled refresh. An ambiguous owner 404 follows the nondestructive
fetch-error path and never extends grant TTLs. `purged_at` prevents subsequent
content, metadata, grants or chunks from restoring removed catalog data. These
internal purge protections ship with meeting removal; the generic privileged
catalog purge API remains a follow-up in the design.

Generation-time decisions/action items/participants remain separate fields;
readable content follows current Google edits. Google creation is a single HTML
conversion and later retries never write that body. Audio stays entirely outside
this persistence boundary. No durable PDF copy is retained by the platform.
