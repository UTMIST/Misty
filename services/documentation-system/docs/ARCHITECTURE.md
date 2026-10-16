# documentation-system — architecture

Orientation for contributors: read after the [README](../README.md) and before
[`CONTRIBUTING.md`](CONTRIBUTING.md). It explains *why* the service is shaped as it is.

## The one-sentence model

The documentation-system is a **catalog of URLs**. Given a URL, it determines what kind of
thing it is (its *source*), tries to fetch a title and snapshot, validates the owner against
the [team-tracking](../../team-tracking/) directory, and stores it — deduplicated, so the same
URL never appears twice. Everything below keeps that flow testable and swappable.

## The three Protocol boundaries

No concrete dependency — database, URL fetching, or directory service — leaks into the
ingest logic or the HTTP layer. Each sits behind a **Protocol** (a structural interface in
`contracts/`). The ingest orchestrator and routers depend only on the Protocols; concrete
implementations are wired in one place (`src/api/deps.py`).

Why, in a student org? The fast suite runs in memory in about a second, with no Docker,
network, or Postgres, and swapping in Postgres required zero changes to ingest or routers.

| Protocol | File | Responsibility |
|----------|------|----------------|
| `StorageAdapter` | `contracts/storage.py` | Persist/query docs, tags, sources, API keys |
| `Fetcher` | `contracts/fetcher.py` | `fetch(url) -> FetchResult` (title + snapshot) |
| `DirectoryClient` | `contracts/directory.py` | Resolve team/person ids to labels |

`contracts/` imports nothing from `src/`. That dependency direction keeps the boundaries
honest: the domain types and Protocols don't know FastAPI, Postgres, or httpx exist.

### 1. `StorageAdapter` — persistence

Defines every persistence operation with identical semantics across implementations:
`create_doc`, `get_doc`, `get_doc_by_normalized_url`, `list_docs`, `update_doc`,
`add_tag`, `remove_tag`, the two source reads, and the API-key operations.

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
- **`GithubFetcher`** (`src/fetch/github.py`) — network-free: derives an `owner/repo` title
  from the URL path, no snapshot. (Raw-content fetch is a later enhancement.)

#### The `FetcherRegistry`

`FetcherRegistry` (`src/fetch/registry.py`) maps a `source_id` to a `Fetcher`.
`default_registry()` registers `{"web": WebFetcher(), "github": GithubFetcher()}` — exactly
the sources with `content_fetch_enabled`. Any other source raises `FetchUnsupported` (a
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
  (empty pattern) is the universal fallback.

## Data model

Six tables (`src/storage/schema.py`; `001` creates the first four, `002` seeds sources,
`003` adds `doc_grants`, `004` hardens dedup, `005` adds `doc_content`, and `007` adds
source-access provenance/expiry):

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

Every table carries the audit quartet `created_at` / `updated_at` / `created_by` /
`updated_by`.

### Seeded sources

| id | label | patterns | requires_auth | content_fetch |
|----|-------|----------|:---:|:---:|
| `web` | Web page | (none) | no | **yes** |
| `github` | GitHub | `github.com` | no | **yes** |
| `gdrive` | Google Drive | `drive.google.com` | yes | **yes** |
| `gdocs` | Google Docs | `docs.google.com/document` | yes | **yes** |
| `gsheets` | Google Sheets | `docs.google.com/spreadsheets` | yes | **yes** |
| `gslides` | Google Slides | `docs.google.com/presentation` | yes | **yes** |
| `notion` | Notion | `notion.so`, `notion.site` | yes | no |
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

**The actor context** is one of three things (`src/api/authz.py` builds it):

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

`vector(N)` with **no ANN index (exact scan) to start** — a 100%-recall baseline for #178 at
a few thousand chunks. Add **HNSW** (pgvector >= 0.5) past roughly 10k chunks, once scans cost
materially. (The keyword branch uses the GIN index above.)

### Retrieval constraints and open decisions

- **Relevance floor: choose in #176, calibrate on #178.** An absolute cosine floor is
  unmeasured; more candidates shift the score distribution, and a relative-to-top-hit floor
  can admit an irrelevant best hit. Evaluate abstention, define how the floor interacts with
  lexical-only matches and fusion (fused ranks aren't cosine scores), and recalibrate when the
  corpus or model changes materially.
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
  don't assume the results generalize to GitHub (#86). Notion (#85) is canceled and untested.
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
- **#175 (indexing/lifecycle):** populate and maintain each chunk's embedding and `tsvector`
  from the same text, including replacement and deletion.
- **#176 (retrieval):** combine keyword and vector results, applying the authorized person or
  channel predicate inside both queries before ranking and fusion. Choose the relevance-floor
  policy using #178; no unfiltered fallback.
- **#178 (evaluation):** build the question set and measure hit-rate@k, including
  exact-name/acronym/date queries, before treating the starting parameters as final.

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
`get_directory` builds an `HttpDirectoryClient` from settings. Tests override `get_storage`
(and inject fake fetchers/directory) via `app.dependency_overrides` — which is why these are
injected functions, not module globals.
