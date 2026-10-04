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
| `docs:read` | Read endpoints (`GET`) — but see visibility below: **alone, it returns nothing** |
| `docs:read:all` | Read endpoints, bypassing per-doc visibility (sees every doc) |
| `docs:write` | All mutating endpoints (`POST`, `PATCH`, `DELETE`) |
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

1. they personally own it (`owning_person_id` matches), **or**
2. they're an active member of the owning team (`owning_team_id` ∈ their team ids), **or**
3. the doc carries a matching grant — `org` (everyone), `person` (that person), or
   `team` (a team they're on).

Team membership is resolved live from the directory. **If the directory is unreachable,
the actor's team set is treated as empty** — a partial fail-closed: personally-owned and
`org`-granted docs still resolve, team-granted ones are withheld rather than leaked.

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
| `GET` | `/sources` | `docs:read` | List source kinds |
| `GET` | `/sources/{id}` | `docs:read` | Get one source |

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
| `active` | bool | `false` = soft-deleted |
| `tags` | string[] | Lowercased, trimmed |
| `grants` | DocGrant[] | Who can see this doc beyond its owners — see below |
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
  fetch, a deferred owner label). An empty list means everything resolved cleanly.

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

### Content fetch (best-effort)

If the derived source has content fetching enabled (`web`, `github`), the service tries to
fetch a title and snapshot at ingest. A fetch failure is **never** fatal: the doc is still
created, `title` falls back to the caller's title or the URL, and a warning is appended.
Sources that require auth (Google Drive/Docs/Sheets/Slides, Notion) are skipped with a
warning; no snapshot is taken.

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
| `active_only` | bool | `true` | When `true`, hides soft-deleted docs |

Returns a JSON array of `Doc` objects in a deterministic order. There is no pagination —
the full matching set is returned (adequate for the current catalog size).

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
| `active` | bool \| null | Set `false` to soft-delete, `true` to restore |

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
is a no-op. Returns the updated `Doc`.

Revoking never removes *ownership*-based visibility — an owner keeps access regardless of
grants.

---

## `POST /docs/{id}/refetch` — refresh the snapshot

Scope: `docs:write`. Re-runs the content fetch for an existing doc on demand (there is no
scheduled/background refetch). On success, updates `title` (if the fetch returned one),
`content_snapshot`, and `fetched_at`, then returns the updated `Doc`.

### Status codes

| Status | Meaning |
|--------|---------|
| **200 OK** | Refetched; returns the updated `Doc` |
| **404 Not Found** | No such doc |
| **502 Bad Gateway** | The fetch failed (`FetchError`) — includes the case where no fetcher is registered for the doc's source (auth-gated sources) |
| **401 / 403** | Auth failure / missing scope |

Note the contrast with ingest: at ingest a fetch failure degrades to a warning, but an
explicit `refetch` surfaces the failure as **502** so the caller knows it didn't work.

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

The eight seeded sources: `web`, `github`, `gdrive`, `gdocs`, `gsheets`, `gslides`,
`notion`, `youtube`. Only `web` and `github` currently have a fetcher and
`content_fetch_enabled: true`.

## `GET /sources/{id}` — get one source

Scope: `docs:read`. **200** with the `Source`, or **404** if unknown.

---

## Error format

Errors use FastAPI's standard shape:

```json
{ "detail": "doc not found" }
```

Validation errors (422) carry the structured `detail` array FastAPI generates. See
`/openapi.json` for the exact per-endpoint schemas.
