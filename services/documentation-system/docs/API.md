# documentation-system — API reference

Consumer-facing reference for the documentation-system HTTP API: the catalog of URLs
(docs, sheets, repos, videos) with owners, tags, and best-effort content snapshots.

If you are building a Discord bot, a dashboard, or any other downstream client, this is
your document. Treat the service internals as a black box — everything you need is here.

- **Interactive Swagger UI:** `GET /swagger`
- **Machine-readable OpenAPI schema:** `GET /openapi.json`

`GET /health` is an unauthenticated liveness check that does not contact the
database. `GET /health/ready` is an unauthenticated readiness check that runs
`SELECT 1` against this service's Postgres database. Both return
`{"status":"ok"}` when healthy; readiness returns `503` with
`{"detail":"documentation-system database unavailable"}` if the database is
unreachable. Railway uses `/health/ready` for deployment health checks.

The OpenAPI schema is the authoritative, always-current contract. This page explains the
*semantics* the schema can't — idempotency, degrade behavior, and error meaning — and
links to the schema rather than restating it.

## Authorized retrieval: `POST /search`

This is the shared contract for member search (#210), helper answers (#71), and
evaluation (#178). The portable [retrieval fixture](fixtures/retrieval.json)
contains synthetic seed data, sparse fake vectors, and HTTP exchanges for all
three consumers. Expand sparse vectors with zeros to 1536 values; never call an
embedding provider for this fixture. `X-API-Key` values are symbolic fixture keys.

The body is `{"query":"CPSIF", "k":10}`. `query` must be nonblank UTF-8,
contain no NUL, and be at most 2000 Unicode code points. Surrounding whitespace
is stripped. `k` is an integer from 1 to 50, default 10. Extra fields are rejected,
including caller-supplied teams, vectors, models, and identity.

Every request needs `docs:read` (or `docs:read:all`/`admin`) and exactly one
authenticated context:

- Person/DM: `X-On-Behalf-Of: <person UUID>` and literal `act-as-user` scope.
- Channel: `X-Guild-ID: <decimal snowflake>` and `X-Channel-ID: <decimal snowflake>`,
  with literal `act-as-channel` scope. The service resolves the configured teams
  through the authenticated directory API and checks their synced Google Groups.

The admin wildcard grants neither assertion scope. Consumers assert identity
from their authenticated event/session, outside model-controlled tool arguments.
A channel search uses only current source-derived grants to those groups. It
does not include private person shares, manual team grants, or org grants.
An unconfigured channel returns `{"results":[]}`. There is no unfiltered search,
even for administrators. Direct `/docs` endpoints retain their existing person
visibility behavior; channel headers cannot confer access there.

Example person request:

```http
POST /search
X-API-Key: <docs consumer key with docs:read and act-as-user>
X-On-Behalf-Of: 11111111-1111-1111-1111-111111111111
Content-Type: application/json

{"query":"CPSIF","k":2}
```

Example response (one chunk per result; a document may appear more than once):

```json
{
  "results": [
    {
      "doc_id": "00000000-0000-0000-0000-000000000001",
      "title": "Personal CPSIF notes",
      "url": "https://example.test/personal-alice",
      "source_id": "gdocs",
      "text": "CPSIF CPSIF CPSIF personal notes",
      "chunk": {
        "ordinal": 0,
        "start_offset": 0,
        "end_offset": 32,
        "source_content_hash": "12807ddcb015128e5e64d0c725f80d4545880fc30817d8f18243d7938e8537d8"
      },
      "score": 0.03278688524590164
    }
  ]
}
```

`doc_id` resolves through `/docs/{id}` under the same person's context. The source
`url` is the original link, not a fabricated deep link. `chunk` identifies the
half-open Unicode code-point range in the exact content revision named by its
SHA-256. Use the hash to detect stale citations after replacement; JavaScript
clients must convert code points to UTF-16 indices. No vectors or grants are
returned. A channel citation links to the source; it does not create a separate
channel-authorized direct-document endpoint.

Ingest and successful content refresh index inline. An indexing failure preserves
the previous complete chunk set: search may return its older text and body hash
until `POST /docs/index` succeeds, even though the stored content or catalog title
has changed. Citations always describe the returned chunk's revision. They do not
promise that the source URL still serves that revision or that historical full
text is available through the API. A newly ingested document with failed indexing
has no results until a retry succeeds.

Authorization uses current grants on every search, including searches over retained
older chunks. Successful source-ACL reconciliation revokes access immediately;
directory membership and channel/group changes apply on the next request. Source
permission changes become known at repeated Google ingest, refetch, or scheduled
refresh. A failed reconciliation retains the last complete grants until their
existing expiry; it never extends them. Manual grants remain independent. See the
[refresh schedule](DEPLOYMENT.md#source-access-refresh-schedule) and
[integration verification](INGESTION-RETRIEVAL-AUDIT.md) for the accepted freshness
window, recovery cases, and release evidence.

Ranking is hybrid exact cosine plus complete keyword matching, with reciprocal
rank fusion (`1/(60+vector_rank) + 1/(60+keyword_rank)`). Each branch considers up
to `max(50,5*k)` eligible chunks, with active-document, grant-expiry, context,
model, and dimension predicates applied **before** its limit. Ties use document
UUID and chunk ordinal. Vector candidates must meet `SEARCH_MIN_COSINE` (default
0.3); complete keyword matches qualify independently. Fused scores are rank
scores, not confidence or cosine values. Empty results are valid.

Keyword terms are lowercase ASCII letter/digit runs, with punctuation separating
terms and all distinct query terms required. This preserves acronyms, names,
email components, and dates without stemming or stop-word removal. Keyword rank
is matching-term occurrences divided by total term occurrences. Non-ASCII-only
queries use the vector branch. See [architecture](ARCHITECTURE.md#authorized-retrieval)
for the portable normalization and database query trade-off.

| Condition | Status / body |
|---|---|
| No valid key | 401, standard auth detail |
| Missing read or literal assertion scope | 403, `{"detail":"missing scope: <scope>"}` |
| Missing, conflicting, or incomplete context | 400, `{"detail":"provide exactly one person or channel context"}` |
| Malformed person UUID | 400, `{"detail":"X-On-Behalf-Of must be a UUID"}` |
| Malformed channel IDs | 400, `{"detail":"channel IDs must be decimal snowflakes"}` |
| Invalid body | 422, Pydantic detail list (`type`, `loc`, `msg`; input omitted) |
| Directory unreachable, unauthorized, or malformed | 503, `{"detail":"retrieval scope unavailable"}` |
| Query embedding failure / malformed or incompatible embedding | 502, `{"detail":"query embedding unavailable"}` |
| Embedding rate limit or unconfigured dependency | 503, same embedding detail |
| Embedding timeout | 504, same embedding detail |
| Storage unavailable | 503, `{"detail":"retrieval storage unavailable"}` |

Retrieval fails closed on scope lookup errors, including person membership errors;
it does not inherit direct-document reads' partial directory degradation.
No fallback to keyword-only or unfiltered access occurs on dependency failure.

## Authentication

Other requests require an `X-API-Key` header. A request with a missing or invalid key is
rejected with **401 Unauthorized**.

```bash
curl -sS http://localhost:8001/sources \
  -H "X-API-Key: doc_ab12cd34_<secret>"
```

Two kinds of key are accepted:

- **Issued scoped keys** — the normal case. Format `doc_<prefix>_<secret>` where `<prefix>`
  is 8 characters. Stored as an Argon2 hash; the plaintext is shown exactly once at
  issue time. Each key carries a set of scopes. Issue them with the `doc-keys` CLI (see
  `docs/DEPLOYMENT.md`).
- **The bootstrap env key** — the value of the `API_KEY` env var. It does not use the
  `doc_` envelope and is treated as scope `admin`. Intended for local dev and initial
  bootstrap, not per-consumer production use.

### Scopes

| Scope | Grants |
|-------|--------|
| `docs:read` | Read endpoints (`GET`, except `GET /docs/index/status`) — but see visibility below: **alone, it returns nothing** |
| `docs:read:all` | Read endpoints, bypassing per-doc visibility (sees every doc) |
| `docs:write` | All mutating endpoints (`POST`, `PATCH`, `DELETE`) except `POST /docs/index` |
| `docs:index` | `POST /docs/index` — admin (re-)indexing, which spends embedding-provider budget — and `GET /docs/index/status` |
| `admin` | Wildcard — satisfies any scope check |

A request with a valid key but the wrong scope is rejected with **403 Forbidden**
(`{"detail": "missing scope: docs:write"}`).

### Attested actor

There is **no `X-Actor` header**. The actor stamped on `created_by` / `updated_by` and in
the audit log is always the authenticated key's own name — a caller cannot claim to be
someone else. Name your keys after the consumer (e.g. `discord-bot`) so the audit trail
is meaningful.

### Visibility and `X-On-Behalf-Of`

Docs are **not all world-readable to any valid key.** Every read is evaluated against an
*actor context*, and the caller chooses which one by how it authenticates:

| Request | Actor context | Sees |
|---|---|---|
| `docs:read:all` (or `admin`) key, no `X-On-Behalf-Of` | **SEE_ALL** | every doc |
| `docs:read` key, no `X-On-Behalf-Of` | **DENY** | **nothing** — an empty list, and `404` on every id |
| Any read-scoped key **+** `X-On-Behalf-Of: <person-uuid>` | that person | only what that person may see |

The `DENY` row is the surprising one and it is deliberate: a plain `docs:read` key has no
identity of its own, so it has no basis to see anything. A consumer like the Discord bot is
expected to pass `X-On-Behalf-Of` with the id of the member who ran the command.

**What a person can see** (`contracts/visibility.py` — the single definition; the
in-memory adapter calls it directly and the Postgres adapter compiles the same rule to
SQL, kept in lockstep by parity tests):

The doc must carry a valid matching grant — manual `org` (everyone), `person` (that
person), or manual `team` (a team they're on). Source-derived grants must be unexpired;
a source-derived `team` grant additionally requires Team Tracking to confirm that person's
managed Google Group membership is synchronized. Ownership fields provide attribution and
filtering only; neither personal ownership nor membership in the owning team grants access.

Team membership is resolved live from the directory. **If the directory is unreachable,
the actor's team set is treated as empty** — a partial fail-closed: person-granted and
manual `org`-granted docs still resolve, team-granted ones are withheld rather than leaked.

**Invisible docs return `404`, not `403`**, on every route that takes a `{doc_id}` —
including the write routes. Distinguishing the two would leak the existence of a doc the
caller isn't allowed to know about.

`X-On-Behalf-Of` must be a valid person UUID; a malformed value is `400`, and a caller
lacking a read scope is `403`. Neither falls through to `SEE_ALL`.

> On **write** routes, a request with *no* `X-On-Behalf-Of` gets `SEE_ALL` — holding a
> `docs:write` key is itself the trust boundary. Supply the header and writes are
> additionally constrained to what that person can see.

## Endpoints at a glance

| Method | Path | Scope | Purpose |
|--------|------|-------|---------|
| `POST` | `/docs` | `docs:write` | Ingest a URL (idempotent) |
| `GET` | `/docs` | `docs:read` | List / filter docs |
| `GET` | `/docs/{id}` | `docs:read` | Get one doc (backfills owner labels) |
| `PATCH` | `/docs/{id}` | `docs:write` | Update a doc |
| `POST` | `/docs/{id}/tags` | `docs:write` | Add a tag |
| `DELETE` | `/docs/{id}/tags/{tag}` | `docs:write` | Remove a tag |
| `POST` | `/docs/{id}/grants` | `docs:write` | Grant visibility to a person / team / the org |
| `DELETE` | `/docs/{id}/grants` | `docs:write` | Revoke a grant |
| `POST` | `/docs/{id}/refetch` | `docs:write` | Re-run the content fetch |
| `POST` | `/docs/source-access/refresh-due` | `docs:write` | Refresh due Google ACLs/content in a bounded batch |
| `POST` | `/docs/source-access/refresh-runs/{run_id}` | `docs:write` | Acquire the singleton runner guard |
| `DELETE` | `/docs/source-access/refresh-runs/{run_id}` | `docs:write` | Release only the matching runner guard |
| `POST` | `/docs/index` | `docs:index` | (Re-)index stored content for retrieval; retry path |
| `GET` | `/docs/index/status` | `docs:index` | Chunk coverage per embedding model; re-embed progress |
| `GET` | `/sources` | `docs:read` | List source kinds |
| `GET` | `/sources/{id}` | `docs:read` | Get one source |
| `GET` | `/api-keys/self` | *(any valid key)* | The calling key's own name and scopes |

## The `Doc` shape

Every doc-returning endpoint responds with this object:

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID | Server-assigned |
| `url` | string | As submitted |
| `url_normalized` | string | Canonical dedup key (see idempotency below) |
| `title` | string \| null | From the fetcher, else the caller, else the URL |
| `source_id` | string | The source kind, e.g. `web`, `github` |
| `description` | string \| null | |
| `owning_team_id` | UUID \| null | Validated against the directory |
| `owning_team_label` | string \| null | Cached label; may be null pending backfill |
| `owning_person_id` | UUID \| null | Validated against the directory |
| `owning_person_label` | string \| null | Cached label; may be null pending backfill |
| `content_snapshot` | string \| null | Best-effort text snapshot |
| `fetched_at` | datetime \| null | When the snapshot was last taken |
| `source_access_synced_at` | datetime \| null | Last complete successful source ACL reconciliation |
| `source_access_attempted_at` | datetime \| null | Last attempted source ACL refresh, successful or not |
| `active` | bool | `false` = soft-deleted |
| `tags` | string[] | Lowercased, trimmed |
| `grants` | DocGrant[] | Who can see this doc — see below |
| `created_at` / `updated_at` | datetime | |
| `created_by` / `updated_by` | string | Attested actor |

A **`DocGrant`**:

| Field | Type | Notes |
|-------|------|-------|
| `grantee_type` | `"person"` \| `"team"` \| `"org"` | |
| `grantee_id` | UUID \| null | Required for `person`/`team`; **must be null** for `org` |
| `grantee_label` | string \| null | Resolved from the directory at the API layer |
| `created_at` | datetime | |
| `created_by` | string | Attested actor |
| `origin` | `"manual"` \| `"google_drive"` | Manual and source-derived grants coexist independently |
| `source_permission_id` | string \| null | Opaque Drive permission id for source grants |
| `source_principal` | string \| null | Normalized source email for provenance |
| `source_role` | string \| null | Drive role observed at reconciliation |
| `source_inherited` | bool | Whether Drive reports the permission as inherited |
| `source_inherited_from` | string[] | Parent folder/shared-drive ids reported by Drive |
| `source_expires_at` | datetime \| null | Expiration reported by Drive, when present |
| `expires_at` | datetime \| null | Source grants fail closed after this time; manual grants do not expire |

---

## `POST /docs` — ingest

Catalog a URL. Scope: `docs:write`.

### Request body (`DocIngest`)

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `url` | string | yes | The URL to catalog |
| `source_id` | string \| null | no | Force a source kind; otherwise derived from the URL |
| `title` | string \| null | no | Overrides the fetched title |
| `description` | string \| null | no | |
| `owning_team_id` | UUID \| null | no | Validated against the directory |
| `owning_person_id` | UUID \| null | no | Validated against the directory |
| `tags` | string[] | no | Lowercased and trimmed server-side |

Unknown fields are rejected (the model forbids extras → **422**).

### Response (`IngestResult`)

Ingest returns an **envelope**, not a bare doc:

```json
{
  "doc": { "...": "the Doc object" },
  "created": true,
  "warnings": []
}
```

- **`created`** — `true` if a new doc was created, `false` if the URL was already
  catalogued.
- **`warnings`** — a list of non-fatal, human-readable strings (e.g. a failed content
  fetch, a deferred owner label, a failed indexing run). An empty list means everything
  resolved cleanly.

After the catalog write, ingest [indexes](#post-docsindex--re-index-stored-content) the doc's stored content — on the
idempotent path too, since a repeated Google ingest may store new text. If embedding or
storing the chunks fails, the doc and its content are still saved and `warnings` contains
`indexing failed (<reason>); retry with POST /docs/index`. A repeated Google ingest that
finds the file gone (connectors' `404 file not found for this url`) removes the stored
content and its chunks and warns `source file not found (...)`. If another fetch has
stored content since this request started, it retains that newer content and index.

### Status codes

| Status | Meaning |
|--------|---------|
| **201 Created** | A new doc was created (`created: true`) |
| **200 OK** | The URL was already catalogued (`created: false`) — idempotent hit |
| **400 Bad Request** | Unknown `source_id`, or an owner id the (reachable) directory does not recognize |
| **401 / 403** | Auth failure / missing scope |
| **422** | Malformed body |

### Idempotency and tag-merge

Ingest is idempotent, keyed on a **normalized** form of the URL — not the raw string.
Normalization lowercases the scheme and host, drops the default port and URL fragment,
removes tracking query params (`utm_*`, `gclid`, `fbclid`, `ref`, …), sorts the remaining
query params, and strips a trailing slash. So `https://Example.com/a/?utm_source=x` and
`https://example.com/a` collapse to the same doc.

Re-submitting a URL that already maps to an **active** doc does **not** create a duplicate.
Instead the response is `200` with `created: false`, any new `tags` in the request are
merged onto the existing doc, and `warnings` contains a note like
`already catalogued (added by <original creator>)`. Other fields in the re-ingest request
(title, owners, etc.) are **not** applied on an idempotent hit — use `PATCH` to change
those.

For Google sources, repeated ingest also reconciles source grants and refreshes content
only when that source's `content_fetch_enabled` setting is true. Disabled sources never
retrieve or store new content through initial ingest, repeated ingest, or scheduled refresh.

**An idempotent hit never fetches.** Even when the existing doc has no stored content —
the case a fetch would repair — dedup stays a cheap, network-free merge, so `POST /docs`
keeps predictable latency however many times a URL is re-submitted. Content repair is
`POST /docs/{id}/refetch`'s job; the `doc-backfill` CLI drives it in bulk over every
contentless doc (see [`DEPLOYMENT.md`](DEPLOYMENT.md#repairing-docs-missing-content-doc-backfill-cli)).
Keeping one repair path also keeps one copy of refetch's rule that an empty fetch must not
wipe stored text.

The gap is still reported: when the existing doc has no stored content, `warnings` also
contains `no content is stored for this doc`. That states the fact and not the remedy on
purpose — warnings are part of this contract and consumers render them verbatim, so the
API does not name operator tooling. To find these docs, use
[`GET /docs?has_content=false`](#get-docs--list--filter).

### Content fetch (best-effort)

If the derived source has content fetching enabled (`web`, `github`, and the four Google
sources), the service tries to fetch a title, snapshot and full content at ingest. A fetch
failure is **never** fatal: the doc is still created, `title` falls back to the caller's
title or the URL, and a warning is appended.

`web` and `github` fetch in-process; the Google sources (`gdrive`, `gdocs`, `gsheets`,
`gslides`) fetch via the [connectors](../../connectors/) service, which holds the Google
credentials — migration `006` enabled them. `youtube` has no fetcher and
`content_fetch_enabled: false`, so its links are catalogued without content and fetching
is silently skipped.

### Content fetch (best-effort)

If the source has content fetching enabled (`web`, `github`, and the Google sources), the
service tries to fetch a title and snapshot at ingest. Google responses must also contain a
complete ACL. User shares resolve only through primary or verified additional emails;
group shares resolve only through Team Tracking's active, membership-synced managed-group
mapping. Unknown/external emails, unmapped or legacy groups, domain shares, and link-wide
(`anyone`) shares create no grant. A Google fetch with no mapped audience stores no fetched
title or content. A fetch/ACL failure is non-fatal at ingest: the URL is catalogued with a
warning and no source-derived access.

Warnings describe ignored or unmapped permission types without disclosing collaborator
emails, group addresses, domains, or permission ids to the ingest caller.

### Owner validation and degrade

Owner ids are validated against the team-tracking directory:

- Directory reachable, id valid → label cached on the doc.
- Directory reachable, id unknown (404) → **400 Bad Request** (`owning_team_id not found`
  / `owning_person_id not found`).
- Directory unreachable → **degrade**: the doc is created, the id is stored, the label is
  left null, and a warning like `directory unavailable; owning_team_id label deferred` is
  added. The label is backfilled on a later read or update.

---

## `GET /docs` — list / filter

List docs. Scope: `docs:read`. All filters are optional query params and combine (AND).

| Param | Type | Default | Effect |
|-------|------|---------|--------|
| `owning_team_id` | UUID | — | Only docs owned by this team |
| `owning_person_id` | UUID | — | Only docs owned by this person |
| `source_id` | string | — | Only docs of this source kind |
| `tag` | string | — | Only docs carrying this tag (matched case-insensitively) |
| `has_content` | bool | — | `false` returns only docs with no stored content; `true` only docs with it |
| `active_only` | bool | `true` | When `true`, hides soft-deleted docs |

Returns a JSON array of `Doc` objects in a deterministic order. There is no pagination —
the full matching set is returned (adequate for the current catalog size).

### `has_content` — finding docs that need a content backfill

`has_content=false` is the discovery query for documents whose content is missing: a doc
whose source fetch failed at ingest is still catalogued, but gets no stored content and
only a warning on the ingest response (see
[`POST /docs`](#post-docs--ingest)), and older records predate content storage
entirely. Omitting the param filters nothing, so existing callers are unaffected.

Two things it deliberately does **not** key on:

- **`content_snapshot`.** The snapshot is a bounded preview on the `Doc` itself; stored
  content is a separate row. A doc can show a snapshot and still be contentless, and it
  still needs backfilling, so it is still returned by `has_content=false`.
- **The text being non-empty.** Empty content is never stored in the first place — both
  ingest and `refetch` skip the write when a fetch yields no text — so presence of the
  row is equivalent to presence of real content.

This filter needs no scope of its own: it narrows the same actor-scoped listing every
other filter narrows, so a caller can only ever discover contentless docs it was already
permitted to see.

**Visibility is applied on top of these filters**, so the result is what the *actor* may
see AND matches the filters. A plain `docs:read` key with no `X-On-Behalf-Of` gets an
empty array no matter what it filters on — see
[Visibility and `X-On-Behalf-Of`](#visibility-and-x-on-behalf-of).

---

## `GET /docs/{id}` — get one

Fetch a single doc by id. Scope: `docs:read`.

- **200** with the `Doc`.
- **404** if no such doc — **or** if it exists but isn't visible to the acting person
  (`{"detail": "doc not found"}`). The two are deliberately indistinguishable.

**Label backfill on read:** if an owner id is present but its label is still null (because
the directory was down at ingest), this endpoint attempts to resolve the label now and
persists it. If the directory is still down, the label stays null and the read still
succeeds.

---

## `PATCH /docs/{id}` — update

Patch a doc. Scope: `docs:write`. Send only the fields you want to change (`DocUpdate`):

| Field | Type | Notes |
|-------|------|-------|
| `title` | string \| null | |
| `description` | string \| null | |
| `owning_team_id` | UUID \| null | Re-validated + label re-resolved |
| `owning_person_id` | UUID \| null | Re-validated + label re-resolved |
| `active` | bool \| null | Set `false` to soft-delete (removes the doc's search chunks), `true` to restore (re-indexes stored content) |

Tags are **not** patched here — use the tag endpoints below.

### Status codes

| Status | Meaning |
|--------|---------|
| **200 OK** | Updated; returns the new `Doc` |
| **400 Bad Request** | An owner id was changed to one the (reachable) directory does not recognize |
| **404 Not Found** | No such doc |
| **401 / 403** | Auth failure / missing scope |

When you change an owner id, the label is re-resolved with the same rules as ingest: a
reachable directory that doesn't know the id → **400**; an unreachable directory →
degrade to a null label (no error), backfilled later.

---

## `POST /docs/{id}/tags` — add a tag

Scope: `docs:write`. Body: `{"tag": "onboarding"}`. The tag is lowercased and trimmed.
Adding a tag that already exists is a no-op (idempotent). Returns the updated `Doc`
(**200**), or **404** if the doc doesn't exist.

## `DELETE /docs/{id}/tags/{tag}` — remove a tag

Scope: `docs:write`. The `{tag}` path segment is lowercased and trimmed before matching.
Removing a tag that isn't present is a no-op. Returns the updated `Doc` (**200**), or
**404** if the doc doesn't exist.

---

## `POST /docs/{id}/grants` — grant visibility

Scope: `docs:write`. Makes a doc visible to a person, a team, or the whole org, beyond
whoever owns it. **Idempotent** — re-granting the same grantee is a no-op, not an error.
These endpoints manage only `origin="manual"` rows. A manual row may coexist with a
source-derived row for the same grantee; removing it never revokes or alters source state.

```bash
# Share with one team
curl -X POST http://localhost:8001/docs/<id>/grants \
  -H "X-API-Key: doc_ab12cd34_<secret>" -H "Content-Type: application/json" \
  -d '{"grantee_type": "team", "grantee_id": "3f1c…"}'

# Share with everyone in the org — note: NO grantee_id
curl -X POST http://localhost:8001/docs/<id>/grants \
  -H "X-API-Key: doc_ab12cd34_<secret>" -H "Content-Type: application/json" \
  -d '{"grantee_type": "org"}'
```

### Request body (`DocGrantInput`)

| Field | Type | Required | Notes |
|-------|------|:---:|-------|
| `grantee_type` | `"person"` \| `"team"` \| `"org"` | ✅ | |
| `grantee_id` | UUID | conditional | **Required** for `person` and `team`; must be **omitted/null** for `org` |

The shape rule is enforced by a validator, so `{"grantee_type": "org", "grantee_id": "…"}`
and `{"grantee_type": "team"}` are both **422**.

### Status codes

| Status | Meaning |
|--------|---------|
| **200 OK** | Granted (or already granted); returns the updated `Doc` |
| **404 Not Found** | No such doc — **or** the doc exists but isn't visible to the acting person |
| **422 Unprocessable Entity** | `grantee_id` present/absent contrary to `grantee_type` |
| **401 / 403** | Auth failure / missing scope |

## `DELETE /docs/{id}/grants` — revoke a grant

Scope: `docs:write`. Same request body as `POST`; the grantee is identified in the **body**,
not the path (an `org` grant has no id to put there). Removing a grant that isn't present
is a no-op. Only the matching manual grant is removed. Returns the updated `Doc`.

After revocation, access requires another valid matching grant. Ownership does not preserve
access.

---

## `POST /docs/{id}/refetch` — refresh the snapshot

Scope: `docs:write`. Re-runs the content fetch for an existing doc on demand. For Google
sources it first resolves the complete returned ACL and atomically replaces only the
`google_drive` grants. A successful empty resolution revokes all source-derived access and
does not store newly fetched content. On a mapped result, it updates `title`,
`content_snapshot`, and `fetched_at`. Visibility is checked again after reconciliation;
if the acting person's access was revoked, the route returns `404` while keeping the
successful reconciliation. Otherwise it returns the updated `Doc`. Explicit refetch remains
available even when the source's automatic content fetching is disabled.

### Status codes

| Status | Meaning |
|--------|---------|
| **200 OK** | Refetched; returns the updated `Doc` |
| **404 Not Found** | Doc is missing or invisible, including access revoked by this refetch |
| **502 Bad Gateway** | Fetch failed, Google ACL was omitted/incomplete, or directory resolution was unavailable; prior source grants remain until their expiry |
| **401 / 403** | Auth failure / missing scope |

Note the contrast with ingest: at ingest a fetch failure degrades to a warning, but an
explicit `refetch` surfaces the failure as **502** so the caller knows it didn't work.

A successful refetch [indexes](#post-docsindex--re-index-stored-content) the new content. An indexing failure does not
change the response (the `Doc` has no warnings field); it is logged, and the doc is listed
as stale by `POST /docs/index`. When connectors confirms the file is gone (`404 file not
found for this url`), the route still returns **502**, and also deletes the stored content
and its chunks so the doc stops being searchable, unless a newer fetch has stored content
meanwhile; the catalog row stays. Any other failure
(timeouts, 5xx, 403, a different 404) leaves content and chunks untouched.

## Source refresh runner guard

`POST /docs/source-access/refresh-runs/{run_id}` atomically acquires the catalog's
single runner slot. `run_id` is a fresh UUID chosen by the caller; `created_by`
comes from the authenticated key. Requires `docs:write` (or `admin`). Returns
`200 {"run_id": "..."}`; an occupied slot returns `409`, including reuse of its
current UUID. Invalid UUIDs return `422`. No document or provider work occurs.

`DELETE` on the same path releases only that exact owner, returning the same
`200` response; a missing or different owner returns `409`. This is also the
operator recovery operation, so **first confirm the owning process and its API
requests have stopped**. Releasing a live owner defeats the guard.

Acquire before any start heartbeat or refresh batch, retain through all batches
and the final heartbeat, and release only after confirmed completion. A rejected
runner must send failure, never start/success or release another run. The guard
persists across restarts with no expiry. On ambiguous HTTP/monitor outcomes,
leave it for [operator recovery](DEPLOYMENT.md#recovery-credential-rotation-and-disablerollback).
This coordinates participating clients: `refresh-due` itself remains a standalone
batch endpoint. Manual callers must follow this protocol or use `doc-source-refresh`.

## `POST /docs/source-access/refresh-due` — scheduled reconciliation

Scope: `docs:write`. Query parameter `limit` defaults to 10 and is constrained to 1–100.
Selects active Google docs with `content_fetch_enabled=true` whose latest sync or attempt is
older than
`SOURCE_ACCESS_REFRESH_AFTER_HOURS` (24 by default), refreshes each independently, and
returns `{ "attempted", "refreshed", "failed", "index_failed" }`. Each refreshed doc is
re-indexed; `index_failed` counts those whose new content could not be indexed (their prior
chunks remain until `POST /docs/index` succeeds). A confirmed-gone file is handled as in
refetch and counts as `failed`. One failed document does not stop the
batch. Failed rows record their attempt time, so they do not starve later rows during the
same hourly drain; run until `attempted` is less than `limit`. See the
[refresh schedule](DEPLOYMENT.md#source-access-refresh-schedule). Disabled sources are skipped
before applying the batch limit; neither their content nor ACL is fetched automatically.

Source grants expire after `SOURCE_ACCESS_MAX_AGE_HOURS` (48 by default). A failed refresh
does not erase a last-known-good grant immediately, but an extended connector, directory,
or scheduler outage eventually makes it unusable. Manual grants remain independent and do
not expire. Ownership never authorizes access. `MAX_AGE` must be greater than `REFRESH_AFTER`.

## `POST /docs/index` — (re-)index stored content

Scope: `docs:index` (or `admin`). Builds each doc's search index from its **stored**
`doc_content` — it never fetches. A doc's text is split by the chunker, embedded through the
llm service's `/embed`, and its whole chunk set is replaced in one transaction. Each
embedding model has its own set: the one search serves (`SEARCH_EMBED_MODEL`) and,
during a re-embed, the target's (`EMBED_MODEL`). Ingest,
refetch, scheduled refresh, and `PATCH active` or `PATCH title` run the same step inline; this endpoint is
the retry path for their failures and the driver for
[re-embedding after a model change](DEPLOYMENT.md#re-embedding-after-a-model-change-209).
Synchronous and bounded.

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `doc_ids` | UUID[] \| null | null | 1–100 docs to index. Every id must exist (else **404** before any work) |
| `stale_only` | bool | `true` | Listing mode (no `doc_ids`): only docs that lack a set for the served or target model, hold one built from older body/title context or without title provenance, hold a set for any other model, or should not have chunks (retired, no content, or whitespace-only text). `false` lists every doc with content |
| `force` | bool | `false` | Re-embed even when a set's content and title are unchanged. Not needed for a model change: a missing target set is already stale |
| `after` | UUID \| null | null | Listing-mode cursor: the previous response's `next_after`. Not allowed with `doc_ids` |
| `limit` | int | 25 | 1–100 docs per call in listing mode |

The response counts each doc's outcome and lists them:

```json
{
  "attempted": 2, "indexed": 1, "unchanged": 0, "cleared": 0, "skipped": 0, "failed": 1,
  "results": [
    {"doc_id": "…", "status": "indexed", "chunk_count": 7, "detail": null},
    {"doc_id": "…", "status": "failed", "chunk_count": 0,
     "detail": "embedding service unavailable for text-embedding-3-small: llm service returned 503"}
  ],
  "next_after": null
}
```

| `status` | Meaning |
|----------|---------|
| `indexed` | A new chunk set replaced the old one (for at least one model) |
| `unchanged` | Every model's chunks already match the stored content and title; no embedding call (unless `force`). Sets for models no longer served or targeted are still deleted |
| `cleared` | The doc is retired, missing, or has no indexable text; its chunks were removed |
| `skipped` | The content or title changed while embedding; nothing written (the newer write re-indexes) |
| `failed` | Embedding or storage failed for a model (named in `detail`); that model's previous set is intact. The served set is written first, so a failing re-embed target leaves it current |

To drain, repeat with `after: next_after` until `next_after` is null. A failing doc is
reported and passed over rather than blocking the batch. Reindexing never creates
duplicates, and a shorter text never leaves chunks from the longer one behind.

| Status | Meaning |
|--------|---------|
| **200 OK** | Ran; see per-doc `results` |
| **404 Not Found** | A `doc_ids` entry does not exist (nothing was indexed) |
| **401 / 403** | Auth failure / missing `docs:index` — checked before any provider call |
| **422** | Malformed body — also rejected before any provider call |

## `GET /docs/index/status` — chunk coverage per embedding model

Scope: `docs:index` (or `admin`). Read-only: no provider call. Reports how far a
[re-embed](DEPLOYMENT.md#re-embedding-after-a-model-change-209) has progressed and whether
any doc has dropped out of search because it is indexed only under another model.

```json
{
  "serving_model": "text-embedding-3-small",
  "target_model": "text-embedding-3-large",
  "indexable_docs": 120,
  "pending_docs": 37,
  "models": [
    {"model": "text-embedding-3-large", "docs": 83, "chunks": 610,
     "chunk_chars": 1180000, "missing_docs": 37},
    {"model": "text-embedding-3-small", "docs": 120, "chunks": 884,
     "chunk_chars": 1712000, "missing_docs": 0}
  ]
}
```

| Field | Meaning |
|-------|---------|
| `serving_model` | Resolved id of `SEARCH_EMBED_MODEL`: the only set search reads |
| `target_model` | Resolved id of `EMBED_MODEL` when it differs (a re-embed, or the old model kept while readers drain after cutover), else `null` |
| `indexable_docs` | Active docs with stored content |
| `pending_docs` | Docs a stale-only `POST /docs/index` would list now. A re-embed is ready to cut over at `0`. Docs whose text is whitespace only have nothing to embed and are not counted |
| `models[]` | Every stored model plus the served and target ones, by model id |
| `models[].docs` / `chunks` / `chunk_chars` | Docs with a set for the model, its chunks, and their total text length (input size for a cost estimate) |
| `models[].missing_docs` | Active docs with content indexed under some other model but not this one. Nonzero for `serving_model` means those docs are excluded from search |

---

## `GET /sources` — list source kinds

Scope: `docs:read`. Query param `active_only` (bool, default **false**) hides inactive
sources when `true`. Returns an array of `Source` objects.

A **source** describes a kind of URL and how the catalog treats it:

| Field | Type | Meaning |
|-------|------|---------|
| `id` | string | Slug primary key, e.g. `web`, `github` |
| `label` | string | Human name, e.g. `GitHub` |
| `url_patterns` | string[] | Host / host+path prefixes used to derive this source from a URL |
| `requires_auth` | bool | Fetching this source needs credentials |
| `has_api` | bool | The source exposes an API (informational) |
| `content_fetch_enabled` | bool | Whether the catalog attempts a snapshot |
| `active` | bool | |

The seven seeded sources: `web`, `github`, `gdrive`, `gdocs`, `gsheets`, `gslides`,
`youtube`. All but `youtube` have a fetcher and
`content_fetch_enabled: true` — migration `006` enabled the four Google sources. `github`
and the Google sources fetch through the connectors service.

## `GET /sources/{id}` — get one source

Scope: `docs:read`. **200** with the `Source`, or **404** if unknown.

---

## `GET /api-keys/self` — what can this key do

No scope beyond a valid key: a caller may always read its own grant, and the response
describes the key the caller already holds. **401** without a valid key.

```json
{ "name": "content-backfill", "scopes": ["docs:read", "docs:read:all", "docs:write"] }
```

`scopes` is sorted and literal — `admin` is reported as `admin`, not expanded into the
scopes it satisfies. Applying the wildcard is the caller's job (`AuthedKey.has_scope`
does it server-side).

**Why a tool needs this.** Read routes answer an authorization failure with *filtered
data*, not an error: a key holding `docs:read` but not `docs:read:all`, sent without
`X-On-Behalf-Of`, resolves to `DENY` and gets **200 `[]`** from `GET /docs` — identical
on the wire to an empty catalog (see "Visibility and `X-On-Behalf-Of`" above). Anything
that acts on a listing being empty — `doc-backfill` concluding there is nothing to
repair — must check its scopes here first rather than infer them from the data.
Mirrors team-tracking's endpoint of the same path.

---

## Error format

Errors use FastAPI's standard shape:

```json
{ "detail": "doc not found" }
```

Validation errors (422) carry the structured `detail` array FastAPI generates. See
`/openapi.json` for the exact per-endpoint schemas.

## Discovery import — `POST /docs/discovery`

Requires `docs:write`; actor is the authenticated key name. This literal route is
registered before `/docs/{doc_id}`. Strict body:

```json
{"file_id": "example", "source_id": "gdocs"}
```

File ids contain letters, digits, `_` and `-` (1–200 characters); source is one of
`gdocs`, `gsheets`, `gslides`, `gdrive`. No URLs, owners, grants or claimed actor are
accepted. The trusted discovery caller supplies the source from the connector's
MIME classification. The catalog checks active/fetch-enabled source status before
fetching, then deduplicates by Drive identity, including legacy/manual URL aliases.
New records use the normal ingest, complete source-ACL reconciliation and indexer.
Existing records are inspected without refetching or re-embedding. An active legacy
Google URL catalogued as `web` is reassigned to the enabled Google source supplied
by discovery, preserving its id, content, tags and manual grants. The existing source
refresh job (or explicit refetch) then reconciles its actual Google ACL and content;
until the first successful ACL sync it is reported as `registered`. Retirement is
honored, including a manual registration/retirement while discovery is fetching:
the final storage insert checks identity atomically with other Google inserts and
returns the existing record without applying discovery's fetched data. Scopes and
validation precede provider work.

Always returns 200 for a classified outcome:

```json
{"status": "indexed", "doc_id": "11111111-1111-1111-1111-111111111111", "warnings": 0}
```

`indexed` means new usable chunks were written; `existing` means a current stored
index already exists and a Google source has completed its initial ACL sync.
`registered` means catalogued but missing content or initial Google ACL reconciliation;
`index_failed` reports failed, missing or stale indexing; `retired` is skipped;
`disabled` means source fetching is disabled/inactive (no doc id). Warnings are a
count, with no content/principals/raw upstream details. An empty/whitespace extraction,
unmapped ACL or fetch failure must not be counted as indexed.

Use the existing refetch/source-refresh and `/docs/index` recovery endpoints; discovery
only corrects the legacy source classification. See [operations and limits](DISCOVERY.md).
All new Google URL ingests now use file identity for normalized dedup keys; displayed
original URLs and existing stored keys are preserved. Editor aliases with an optional
`u/<number>/` account selector share the same identity. Forms edit URLs without an
explicit source use `gdrive` when no more-specific source pattern matches and that
source is registered. Published `/d/e/` URLs are
excluded from Drive identity extraction and keep distinct URL keys. Preexisting
duplicate records are not merged. The active earliest record wins identity lookup.

## Meeting records

These service-to-service endpoints use attested key actors and strict input
models. `meeting_id` is the bot's UUID, independent of the Doc title or URL.

| Method/path | Scope | Behavior |
|---|---|---|
| `PUT /meeting-records/{meeting_id}` | `meetings:write` | Durable hand-off: 202 on insert; 200 on identical replay; 409 on changed payload |
| `GET /meeting-records/{meeting_id}` | `meetings:read` | Status, canonical IDs/URL, generation metadata and delivery state; no pending transcript |
| `GET /meeting-records` | `meetings:read` | Optional `persistence_status` and `delivery_status` filters |
| `POST /meeting-records/retry-due?limit=10` | `meetings:write` | Bounded oldest-due persistence, reconciliation and expiry with outcome counts; limit 1–100 |
| `POST /meeting-records/{meeting_id}/retry` | `meetings:admin` | Explicit retry of a failed/inaccessible record; removal remains terminal |
| `POST /meeting-records/{meeting_id}/remove` | `meetings:admin` | Mark removal before external calls; purge catalog and permanently delete tagged Docs |
| `PATCH /meeting-records/{meeting_id}/delivery` | `meetings:write` | `{status: posted\|failed, discord_message_id}`; only saved, nonremoved records; posted requires an ID |

The PUT body contains `guild_id`, `text_channel_id`, `voice_channel_id`,
`requester_discord_id`, timezone-aware `started_at` and `ended_at`, `title`,
`participants: [{discord_id, display_name}]`, `summary`, `decisions: string[]`,
`action_items: string[]`, and the full assembled `transcript`. Decisions and
action items stay discrete JSON fields. Unknown fields, duplicate participants,
invalid timestamps/IDs, NUL/invalid UTF-8, and oversized content fail with 422.
Escaped text is capped at 3,500,000 characters to fit the writer transport.
The directory resolves participant IDs and the complete voice-channel team
Google Groups; incomplete group coverage results in an admin-only record.
A directory outage returns 503 so `meeting` holds and retries the hand-off.

Acceptance commits before Google work starts. A background attempt runs once;
remaining work belongs to the existing hourly runner. State, leases and the
immutable canonical ID live in Postgres; repeated acceptance never creates
another row. A timed-out create keeps its lease until reconciliation. Recurring
scans continue after save, failure and removal and delete late duplicates without
rewriting the canonical Doc. Admin-only status is represented by empty
`audience_team_ids`; no org grant or public sharing fallback exists.

Catalog acceptance also sets `index_pending=true` atomically, then indexes the
stored content immediately. Successful indexing clears the flag. A failure keeps
the saved Doc and retries through `retry-due` after five minutes without another
Google write; the hourly runner picks up due retries.

`google_deleted_at` is a durable receipt for a confirmed canonical deletion,
allowing removal to resume after later cleanup fails. An ambiguous 404 never
sets that receipt. `google_deletions` maps each cleanup identity to `pending`
(before the external delete) or `confirmed` (after acknowledgement). Both states
permanently exclude that identity from canonical adoption; explicit retry can
create a fresh Doc from the retained payload. Successful reconciliation clears
`reconcile_failed`, so recovered scans stop reporting cleanup failures.

Removal retries the version check before acknowledging its removal marker.

Removal purges the linked catalog row and every alias of the canonical or discovered
duplicate Google IDs, even when ordinary ingestion left `doc_id=null` on the
meeting. `removed_google_document_ids` retains these identity fences so concurrent
or later registration cannot restore them. Reconciliation also purges catalog
copies of late duplicates and previously removed meetings with empty owner searches.
This purge includes every `google_deletions` identity, including confirmed duplicates
that disappeared from Google search before removal. Pending deletions are retried by
ID in both removal and reconciliation even when search no longer lists the file.
Cleanup remains incomplete, with a failed retry result, until deletion is acknowledged;
an empty listing or ambiguous 404 does not confirm deletion. Older tombstones with
pending deletions follow the same recovery path.

The retry response contains all of `attempted`, `succeeded`, `failed`,
`persistence_failed`, `index_failed`, and `cleanup_failed` as nonnegative integers.
`succeeded + failed = attempted`; `failed` counts meetings once, while the failure
categories may overlap. A selected meeting with unfinished/failed persistence,
pending indexing, or failed cleanup counts as failed even though the HTTP request
completed successfully. An empty queue returns all zeros. The monitored runner
requires every count and sends a failure heartbeat for any failed meeting work.

`last_error` contains a short code. Inspect failed records with
`GET /meeting-records?persistence_status=failed`. `inaccessible` means an owner
fetch cannot confirm deletion; retained snapshots/content/chunks and existing
grant expiry remain intact. Only independently confirmed trash/deletion or an
explicit removal triggers purge. A security removal records
`removed_by=security:unexpected_acl`; admins notify the requester and retain the
Discord PDF unless its removal is separately requested. The bot's link delivery
and Discord cleanup are implemented separately in #223.
