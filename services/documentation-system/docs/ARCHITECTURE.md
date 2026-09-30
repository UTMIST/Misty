# documentation-system — architecture

Orientation for contributors. Read this after the [README](../README.md) and before
[`CONTRIBUTING.md`](CONTRIBUTING.md). It explains *why* the service is shaped the way it
is, not just what each file contains.

## The one-sentence model

The documentation-system is a **catalog of URLs**. You hand it a URL; it figures out what
kind of thing that URL is (its *source*), tries to fetch a title and snapshot, validates
who owns it against the [team-tracking](../../team-tracking/) directory, and stores it —
deduplicating so the same URL never appears twice.

Everything below is in service of keeping that flow testable and swappable.

## The three Protocol boundaries

The core design idea is that no concrete dependency — not a specific database, not a
specific way of fetching a URL, not a specific directory service — should leak into the
ingest logic or the HTTP layer. Each of those three concerns sits behind a **Protocol**
(a structural interface, in `contracts/`). The ingest orchestrator and routers depend only
on the Protocol; concrete implementations are wired in at one place (`src/api/deps.py`).

Why bother, in a student org? Because it lets the whole service run its test suite in
memory with no Docker, no network, and no Postgres — the fast suite runs in about a second — and
it means swapping the in-memory store for Postgres required zero changes to the ingest code
or routers. New contributors can be productive without standing up infrastructure.

| Protocol | File | Responsibility |
|----------|------|----------------|
| `StorageAdapter` | `contracts/storage.py` | Persist/query docs, tags, sources, API keys |
| `Fetcher` | `contracts/fetcher.py` | `fetch(url) -> FetchResult` (title + snapshot) |
| `DirectoryClient` | `contracts/directory.py` | Resolve team/person ids to labels |

`contracts/` imports nothing from `src/`. This dependency direction is the invariant that
keeps the boundaries honest: the domain types and Protocols don't know FastAPI, Postgres,
or httpx exist.

### 1. `StorageAdapter` — persistence

Defines every persistence operation with identical semantics across implementations:
`create_doc`, `get_doc`, `get_doc_by_normalized_url`, `list_docs`, `update_doc`,
`add_tag`, `remove_tag`, the two source reads, and the API-key operations.

Two implementations:

- **`InMemoryStorageAdapter`** (`src/storage/in_memory.py`) — plain Python dicts. Used by
  the fast test suite, injected via FastAPI's `dependency_overrides`. No I/O.
- **`PostgresStorageAdapter`** (`src/storage/postgres.py`) — SQLAlchemy Core against
  Postgres. Used in production and in the `RUN_PG_TESTS` integration suite, which runs the
  *same* behavioral assertions against a live database to prove the two adapters agree.

The Protocol is the contract; a divergence between the two adapters is a bug, and the
shared test seed (`build_seed_sources()` in `tests/conftest.py`) plus the mirrored Postgres
tests exist to catch exactly that.

### 2. `Fetcher` — content snapshots

A `Fetcher` takes a URL and returns a `FetchResult` (`title`, `content_snapshot`), or
raises `FetchError`. `FetchError` is deliberately non-fatal at the ingest layer — it
becomes a warning, never blocks the doc.

Two implementations today:

- **`WebFetcher`** (`src/fetch/web.py`) — fetches the page over HTTP (5s timeout, follows
  redirects), parses the `<title>`, and extracts up to 2000 chars of stripped text as the
  snapshot. HTTP errors become `FetchError`.
- **`GithubFetcher`** (`src/fetch/github.py`) — cheap and network-free: derives an
  `owner/repo` title from the URL path, no snapshot. (Raw-content fetch is a later
  enhancement.)

#### The `FetcherRegistry`

`FetcherRegistry` (`src/fetch/registry.py`) maps a `source_id` to a concrete `Fetcher`.
`default_registry()` wires up `{"web": WebFetcher(), "github": GithubFetcher()}` — matching
exactly the sources whose `content_fetch_enabled` is `true`. Calling `fetch_for` with a
source that has no registered fetcher raises `FetchUnsupported` (a subclass of
`FetchError`). This is why auth-gated sources like Google Drive "skip fetching" — there's
simply no fetcher registered for them, and ingest treats the resulting `FetchError` as a
warning.

### 3. `DirectoryClient` — ownership validation

`DirectoryClient` resolves an owning team/person id to a display label. The production
implementation is **`HttpDirectoryClient`** (`src/directory/http_client.py`), which calls
the team-tracking service's `GET /teams/{id}` and `GET /people/{id}` with the configured
directory API key.

The crucial part is its **404-vs-unavailable** distinction:

- **HTTP 404** → the directory is up and says "no such record" → returns `None`. Callers
  interpret this as a genuinely unknown owner (→ HTTP 400 at the API layer).
- **Connection failure or 5xx** → raises `DirectoryUnavailable`. Callers interpret this as
  "we can't tell right now" and **degrade** rather than reject.

This two-way split is what makes degrade-on-directory-down safe: we only reject an owner id
when we have positive confirmation the directory doesn't know it.

## The ingest orchestrator

`src/ingest.py` (`ingest_doc`) is the heart of the service. It's pure of framework
concerns — it takes injected `storage`, `fetchers`, `directory`, and an `actor` string, so
it's trivially unit-testable with fakes. The five steps:

1. **Normalize + dedup.** Normalize the URL (`normalize_url`) and look it up by normalized
   form. If an **active** doc already exists, merge any new tags, and return
   `IngestResult(created=False, …)` — no duplicate, no other fields touched.
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

`BadReference` is the orchestrator's "the caller gave a bad id and we can prove it" signal;
routers map it to **HTTP 400**.

### Where degrade completes: label backfill

Degrade leaves a null label behind. `src/api/routers/docs.py` closes the loop:
`_backfill_labels` runs on `GET /docs/{id}` and re-resolves any null owner labels (persisting
them if the directory is now reachable), and `PATCH` re-resolves labels whenever an owner id
changes. So a doc ingested during a directory outage heals itself on the next read or update.

## URL normalization + most-specific-source derivation

`src/url_norm.py` holds two pure, heavily-tested helpers with no I/O:

- **`normalize_url`** produces the dedup key: lowercase scheme/host, drop default port and
  fragment, strip tracking params (`utm_*`, `gclid`, `fbclid`, `ref`, …), sort remaining
  params, strip trailing slash.
- **`derive_source`** picks the source. A source's `url_patterns` are bare hosts
  (`github.com`) or host + path prefixes (`docs.google.com/document`). A pattern matches
  when the URL host equals or is a subdomain of the pattern host **and** the path starts
  with the pattern's prefix. The **longest matching pattern wins** — so
  `docs.google.com/spreadsheets/…` resolves to `gsheets`, not the shorter `gdocs`/`web`
  match. `web` (empty pattern) is the universal fallback.

## Data model

Five tables (`src/storage/schema.py`; `001` creates the first four, `002` seeds sources,
`003` adds `doc_grants`, `004` hardens dedup):

### `sources`

The registry of URL kinds. `id` is a slug primary key. Key columns: `label`,
`url_patterns` (text array driving `derive_source`), `requires_auth`, `has_api`,
`content_fetch_enabled`, `active`. Eight rows are seeded (see below).

### `docs`

The catalog itself. Key columns: `url`, `url_normalized` (indexed dedup key), `title`,
`source_id` (FK → `sources.id`, defaults `'web'`), `description`, the two
`owning_*_id` / `owning_*_label` pairs, `content_snapshot`, `fetched_at`, and `active`
(the soft-delete flag). Indexed on `url_normalized`, both owner ids, and `source_id`.

Migration `004` adds a **partial unique index** on `url_normalized WHERE active`, so
dedup is enforced by the database rather than only by ingest's read-then-write. The
`WHERE active` qualifier is what makes it workable: a soft-deleted doc doesn't block
re-cataloguing the same URL later.

### `doc_grants`

Who may see a doc, beyond its owners (migration `003`). One row per grant:
`doc_id` (FK → `docs.id`, `ON DELETE CASCADE`), `grantee_type` (`person` / `team` /
`org`), `grantee_id` (UUID, **null for `org`**), plus `created_at` / `created_by`.

Three constraints carry the invariants, and the third is the non-obvious one:

- `ck_doc_grants_grantee_shape` — a CHECK enforcing that `org` grants have a null
  `grantee_id` while `person`/`team` grants have a non-null one. The same rule is
  validated in `contracts/types.py`, so a bad shape is a 422 long before it reaches the DB;
  the CHECK is the backstop.
- `uq_doc_grants_grantee` — unique on (`doc_id`, `grantee_type`, `grantee_id`), which is
  what makes `add_grant` idempotent.
- `uq_doc_grants_org` — a *partial* unique index on `doc_id WHERE grantee_type = 'org'`.
  It exists because the constraint above **cannot** catch duplicate org grants: their
  `grantee_id` is NULL, and in SQL `NULL != NULL`, so two identical org rows don't collide.
  Without this index, "share with the org" twice would insert two rows.

Grantee ids are **not** foreign keys — the people and teams they point at live in
team-tracking's database, which this service never touches directly. Labels are resolved
over HTTP at the API layer and never stored on the grant.

### `doc_tags`

One row per (doc, tag). `UniqueConstraint(doc_id, tag)` enforces idempotent tagging;
`ON DELETE CASCADE` cleans up with the doc. Tags are stored lowercased/trimmed.

### `api_keys`

Auth store. Columns: `name` (unique), `prefix` (unique, the lookup key), `key_hash`
(Argon2), `scopes` (text array), `active`, `revoked_at`, `last_used_at`. The plaintext key
is never stored.

Every table carries the audit quartet `created_at` / `updated_at` / `created_by` /
`updated_by`.

### Seeded sources

| id | label | patterns | requires_auth | content_fetch |
|----|-------|----------|:---:|:---:|
| `web` | Web page | (none) | no | **yes** |
| `github` | GitHub | `github.com` | no | **yes** |
| `gdrive` | Google Drive | `drive.google.com` | yes | no |
| `gdocs` | Google Docs | `docs.google.com/document` | yes | no |
| `gsheets` | Google Sheets | `docs.google.com/spreadsheets` | yes | no |
| `gslides` | Google Slides | `docs.google.com/presentation` | yes | no |
| `notion` | Notion | `notion.so`, `notion.site` | yes | no |
| `youtube` | YouTube | `youtube.com`, `youtu.be` | no | no |

## Auth

Level 2 (scoped API keys), the same model as team-tracking. The machinery itself now lives
in the shared `platform_auth` package (`packages/auth/`) — a pure leaf package with no
imports of any service's `src/` or `contracts/`, shared with team-tracking. This service's
`src/api/auth.py` and `src/api/hashing.py` are thin (~15-line) shims that call
`platform_auth`'s `build_auth(...)` factory, binding documentation-system's `doc_` key
envelope and config (using `platform_auth`'s defaults — no dev-spoof affordance, unlike
team-tracking), and re-export the same names. The auth behavior and contract are
unchanged by this move:

- **`hashing.py`** (shim) — key format `doc_<prefix>_<secret>` (8-char prefix), Argon2
  hashing, `parse_prefix` for extracting the lookup prefix.
- **`auth.py`** (shim) — `require_api_key` looks the key up by prefix, verifies the hash,
  and checks `active` / not-revoked; the bootstrap env key is accepted as scope `admin`.
  `require_scope(scope)` gates each route; `admin` is a wildcard. `get_actor` returns the
  key's own name — the **attested actor** (no `X-Actor` header, no impersonation).
- **`AuditLogMiddleware`** — emits one structured JSON log line per request (method, path,
  status, duration, resolved key name, remote IP), reading the `request.state.auth_key`
  that auth stamped. It never fails the request. It binds nothing per-service, so
  `app.py` imports it from `platform_auth` directly rather than through a shim.

## Visibility: the second authorization layer

Scopes answer "may this key call this endpoint." Visibility answers "which *rows* may it
see." They compose: a request must pass both.

**One definition, two implementations.** `contracts/visibility.py` holds `doc_visible()`
— a pure function over (actor context, owning ids, grants). The in-memory adapter calls
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
| `Actor(person_id, team_ids)` | `X-On-Behalf-Of: <uuid>` present | that person's view |

`DENY` is the design's sharpest edge and it's deliberate: a bare `docs:read` key carries
no identity, so it has no principled basis for seeing anything. Consumers are expected to
say *who* they're acting for. The read path additionally **requires** a read scope even
when acting on behalf of someone — least privilege, so a write-only key can't read through
the on-behalf-of door.

An `Actor` sees a doc if they own it personally, are on the owning team, or a grant
matches (`org` / their `person` id / one of their teams).

**Team ids are resolved live** from the directory (`get_active_team_ids`). If the
directory is unreachable the set is treated as **empty** rather than failing the request —
a partial fail-closed. Personally-owned and `org`-granted docs still resolve; team-granted
ones are withheld. Withholding is the safe direction: an outage can hide a doc, never
expose one.

**Invisible reads 404, they don't 403.** `get_visible_doc_or_404` is applied on read *and*
write routes alike, so a caller can't probe for the existence of a doc they may not see by
watching status codes.

## Retrieval layer: chunking + search strategy (RAG)

> **Status: spike / decision record.** This is the "decide" stage of the RAG epic (#125).
> It fixes the parameters the indexing pipeline (#175) and retrieval endpoint (#176) get
> built on, so those decisions live here rather than in a comment thread. The numbers below
> are *starting* values to be validated empirically by the evaluation set (#178), not
> hand-tuned finals. When #178 moves one of them, update it here in the same PR.

**Decision (#208, [PR #219](https://github.com/UTMIST/Misty/pull/219)): ~500-token chunks,
~50-token overlap (10%), and hybrid keyword/vector retrieval.** Character equivalents below
are estimates for the sampled text, not alternative fixed-character defaults.

### What we are chunking

The pipeline chunks the **stored plain text of a catalog entity**, not a URL-document. The
source of that text is `doc_content.content_text` (capped at `MAX_CONTENT_CHARS` = 1,000,000,
see `src/content.py`), not the shorter `docs.content_snapshot`. It is populated during ingest.
Keying on the catalog entity rather than the URL-fetch path is deliberate: meeting records are
coming and will be catalogued entities with no source URL, and a pipeline written around
URL-fetched docs would silently exclude them.

The text is already normalized and source-specific by the time it reaches storage. The shapes
that actually exist today (all via the Google connector, `services/connectors/`):

| Source | Extracted shape | Chunking implication |
|--------|-----------------|----------------------|
| Google Docs | Prose, paragraph-structured | Recursive split on paragraph then sentence boundaries |
| Google Slides | One line per text run, slides joined by `\n` | Slide is a natural boundary; keep slides intact where they fit |
| Google Sheets | Per-tab, `FORMATTED_VALUE` rows, row-capped | Split per tab, then per row-group; never split mid-row |
| PDF / DOCX (Drive) | Flattened text, structure lossy | Fall back to plain recursive split |
| Notion (#85), GitHub READMEs (#86) | Markdown (future) | Split on headings then paragraphs |

A boundary-aware recursive splitter with a **per-source separator list** can honor the
structure each connector emits while targeting the token budget below. The spike used
character counts as a proxy for that budget; it did not establish fixed-character defaults.

### Chunk size and overlap

The spike sampled real UTMIST Google Docs rather than synthetic examples: structured GitHub
standards, sparse and dense transition-report templates, loosely structured Senior Exec meeting
notes, and a degenerate single-link stub. The sampled documents ranged from ~26 to ~3,421
tokens. The dense transition reports exposed an important preprocessing requirement: Google
Docs table-cell line breaks are exported as the literal string `&#10;`, not real newlines.

**Normalize encoded newlines inside table cells before splitting.** In the sampled Technical
Writing transition report, the longest literal line was 2,189 characters (~547 tokens), already
larger than the target chunk; after replacing `&#10;` with real line breaks it was 357 characters.
This occurred 74 times in that document, and the same transition-report template is reused
across roughly 9–10 departments, so this is a corpus-level pattern rather than an edge case.
Without normalization, paragraph/list boundaries inside these cells are invisible to the
splitter.

Starting parameters:

- **Chunk size: ~500 tokens (~2,000 characters at ~4 chars/token).**
- **Overlap: ~50 tokens (~200 characters, 10%).**
- **Respect boundaries first, size second:** try markdown headers → top-level list items →
  paragraph breaks → sentences → words, only falling through when a unit is still oversized.

The sampled corpus supports 500 tokens better than the alternatives: 300-token chunks produced
11–16 chunks for the longer documents and more often split table-cell blobs or single bullet-list
answers across chunks; 800-token chunks reduced longer documents to as few as 5–6 chunks and
often merged unrelated department or role blocks. At 500 tokens, chunks were large enough to hold
a `ROLE: X` table block or a department status block while remaining narrow enough for retrieval.
Short documents remained at 1–4 chunks rather than becoming pathologically fragmented.

The 10% overlap is carried through unchanged from testing: it preserves continuity across
table-row or list-item boundaries without materially inflating storage or embedding cost at this
corpus size. The spike used character-based proxies, not an embedding-model tokenizer.
#218 must document its counting method and validate it against the model and input constraints
selected in #173; four characters per token is not a guaranteed conversion. The boundary shape
and `&#10;` normalization requirement do not depend on the exact tokenizer.

### Hybrid vs pure vector

**Decision: use hybrid retrieval — pgvector cosine similarity combined with Postgres full-text
search (`tsvector`/`tsquery`, optionally `pg_trgm` for fuzzy acronym/name matches), merged with
something like Reciprocal Rank Fusion — rather than pure vector search.**

The spike sampled roughly 25 distinct Google Docs from targeted Drive folder look-ins across the
2026–2027 departmental storage and 2025–2026 transition-report sets. This was not an exhaustive
census, but suggests the reachable Google Doc corpus is on the order of **low hundreds of
documents**, plausibly **1,000–5,000 chunks** at the chosen chunk size. That is comfortably
within what Postgres full-text search handles natively and does not justify introducing a
dedicated search service.

The corpus is also dense with exact-match terms that embeddings may blur: UTMIST-specific
acronyms (`CPSIF`, `MLF`, `deMISTify`, `CUCAI`, `EigenAI`), people's names, Discord handles,
emails, exact dollar figures, and exact dates. Lexical search is a better fit for queries such
as "who do I email about CPSIF" and helps distinguish semantically similar but distinct events
such as `AI²`, `GenAI Genesis`, and `Academic Journal Workshops`.

This also fits the planned storage in #174: vectors live in Postgres, and adding a `tsvector`
column with a GIN index keeps both retrieval paths in-stack without introducing
Elasticsearch/OpenSearch or another service. Hybrid should be revisited if the corpus grows by
roughly an order of magnitude into the low thousands of documents, or if measured latency or
relevance problems appear that hybrid cannot address.

### Index type

`vector(N)` with **no ANN index (exact scan) to start.** At a few thousand chunks an exact
nearest-neighbor scan provides a simple 100%-recall baseline for #178. Introduce an **HNSW**
index (pgvector >= 0.5) only once the corpus crosses roughly 10k chunks and exact scans start
to cost materially. This applies to the vector branch only; the keyword branch uses the GIN
index described above.

### Two decisions this spike also closes

The epic left two questions open. This is where they land:

- **Relevance floor: absolute cosine similarity, calibrated on #178.** Cosine similarity is
  scale-free, so an absolute floor does not drift with corpus size the way a raw-distance
  threshold would; the concern with absolute thresholds is primarily un-normalized score
  distributions. A relative-to-top-hit floor can admit an irrelevant result when the best hit is
  itself irrelevant. Pick the threshold from the evaluation set, not intuition, and revisit it if
  corpus growth shifts the score distribution.
- **`/doc search` uses the exact same visibility rule as `/doc list`.** It reuses
  `doc_visible()` and the `Actor` / `SEE_ALL` / `DENY` context unchanged. The visibility
  predicate is compiled *into both* the vector and keyword queries before top-k selection and
  result fusion. Neither branch ranks unauthorized rows and filters them afterward. This keeps
  search and list behavior consistent.

### Spike methodology and limitations

This spike measured chunk shape and boundary behavior against real Google Docs from the
connector's actual Drive corpus; no document text was committed or persisted outside the spike.
A small local Python harness simulated recursive splitting and measured character/line counts.
It was not an embedding or retrieval harness and is not a substitute for #178's hand-built
question set, expected sources, and hit-rate@k evaluation.

Limitations:

- **No exact corpus count.** The low-hundreds estimate comes from targeted folder sampling,
  not an exhaustive walk of the four academic-year trees. #175 should record the real count on
  first full ingest and revisit this decision if it differs by an order of magnitude.
- **Google Docs only.** Sheets, Slides, PDFs, GitHub content, and future sources were not
  validated by this spike; the results should not be assumed to generalize to GitHub (#86).
  Notion (#85) is canceled and was not tested.
- **No retrieval-quality measurement.** Chunk shape was measured, but retrieval hit-rate was not.
  That requires the embedding model from #173 and belongs to #178.
- **Extracted text length matters more than Drive file size.** One sampled document reported an
  884KB Drive file size but extracted to only ~14KB of plain text, likely because the file size
  includes images/revision metadata. Chunk counts should therefore be based on extracted text.
- **Large-document extraction can truncate.** The extraction tool cut off `Senior Exec Meeting
  #10` mid-sentence, so the true size of the largest documents may be larger than measured.

### Carrying this into implementation

- **#218 (pure chunker):** implement the recursive header → list-item → paragraph → sentence →
  word splitter targeting 500 tokens / 50-token overlap (10%), with `&#10;` normalization as a
  documented preprocessing step. Document counting and source-offset conventions and validate
  input limits against the model selected in #173.
- **#174 (storage):** store vectors and keyword-search metadata for the same document chunks.
- **#175 (indexing/lifecycle):** populate and maintain each chunk's embedding and `tsvector`
  from the same chunk text, including replacement and deletion. Retrieval belongs to #176.
- **#176 (retrieval):** combine keyword and vector results, applying the same actor visibility
  predicate inside both queries before ranking and fusion.
- **#178 (evaluation):** build the real question set and measure retrieval hit-rate@k, including
  exact-name/acronym/date queries, before treating the starting parameters as final.

## SSRF protection in the web fetcher

Ingest fetches arbitrary caller-supplied URLs, which is a textbook SSRF sink: without a
guard, `POST /docs` becomes a proxy for reaching Railway's private network or a cloud
metadata endpoint.

The guard (`src/fetch/web.py`) resolves the hostname first and **pins the connection to
the validated IP**, so httpx never re-resolves the name — closing the DNS-rebinding window
between "we checked the name" and "we opened the socket". It rejects private, loopback,
link-local, and **carrier-grade-NAT** (`100.64.0.0/10`) ranges — that last one matters
because Python's `ipaddress.is_private` doesn't cover RFC 6598. Auto-redirects are
disabled: each hop is validated and followed manually up to a cap, so a public URL that
302s to `169.254.169.254` is still blocked. Resolver failures are errors, not passes, and
malformed URLs (or a malformed redirect `Location`) surface as ordinary `FetchError`s
rather than 500s.

Note what this is *not*: there's no hostname allowlist. Any public URL is fetchable by
design — the catalog's job is cataloguing the open web.

## Wiring: `src/api/deps.py`

The one place concrete adapters meet their Protocols. `get_storage` builds a
`PostgresStorageAdapter` over a pooled engine; `get_fetchers` returns `default_registry()`;
`get_directory` builds an `HttpDirectoryClient` from settings. Tests override
`get_storage` (and inject fakes for fetchers/directory) via `app.dependency_overrides` —
which is exactly why these are dependency-injected functions and not module globals.
