# API Reference

Full endpoint reference for `team-tracking` v1.

See [ARCHITECTURE.md](ARCHITECTURE.md) for design decisions. The machine-readable OpenAPI schema is at `GET /openapi.json`. Interactive Swagger UI is at `GET /docs`.

## Base URL and auth

`GET /health` is an unauthenticated liveness check. It returns `{"status":"ok"}`
without contacting Postgres. `GET /health/ready` is an unauthenticated readiness
check: it runs `SELECT 1` against this service's database, returning the same
body on success or `503` with `{"detail":"database unavailable"}` on failure.
Railway uses `/health/ready` for deployment health checks.

Local development: `http://localhost:8000`. In staging/production the API is deployed to Railway and reachable **only over Railway's private network** (no public domain) — consumers (bot, docs-system) hit it at `http://team-tracking.railway.internal:8000`. External access is a one-click "add public domain" in Railway if ever needed.

Other requests — read or write — must include a valid **scoped API key** in the `X-API-Key` header:

```
X-API-Key: tt_<prefix>_<secret>
```

Keys are issued by an operator via the `team-tracking-keys` CLI (see [DEPLOYMENT.md](DEPLOYMENT.md)) and have the format `tt_<prefix>_<secret>`: an 8-char public prefix used for lookup, and a secret verified against a stored argon2 hash. The plaintext is shown once at issuance and never recoverable. A legacy env bootstrap key (the `API_KEY` setting) is also accepted with `admin` scope, but is deprecated — real consumers get their own scoped key.

If the key is missing, malformed, or unrecognized, the endpoint returns `401 Unauthorized`. Every failure mode returns the same 401 body — the API never leaks which check failed.

### Scopes

Each endpoint requires a specific scope. A key only reaches an endpoint if its scopes include the required one (or the wildcard `admin`); otherwise the response is `403 Forbidden`.

| Domain | Scopes |
|--------|--------|
| People | `people:read`, `people:write` |
| Teams | `teams:read`, `teams:write` |
| Role kinds | `role_kinds:read` |
| Memberships | `memberships:read`, `memberships:write` |
| Providers | `providers:read` |
| Identifiers | `identifiers:read`, `identifiers:write` |
| Dev-only | `dev:spoof` — local-dev only; refused against `TT_ENV=production` at both issuance and request time |
| Wildcard | `admin` — grants every scope, but does NOT satisfy the `dev:spoof` guard |

The required scope for each endpoint is listed in its section below.

The **`dev:spoof`** scope is the discord-bot playground's declaration that it
runs in a spoofable dev environment. It is not required by any endpoint; its
presence gates the discord-bot's own startup guard (which refuses to enable
its "Acting as any Discord ID" mode without it). Team-tracking, in turn,
refuses to *issue* keys with this scope against `TT_ENV=production` — and
refuses to *serve* requests bearing them against production, even if the key
somehow slipped in via a copied DB. See [DEPLOYMENT.md](DEPLOYMENT.md) for
`TT_ENV` semantics.

### Attested actor (`created_by` / `updated_by`)

Writes stamp `created_by` and `updated_by` with the **name of the key that made the request** — cryptographically attested, not self-declared. There is no need to send an actor header: if you issue a key named `discord-bot`, every write it makes is recorded as `discord-bot`.

> The `X-Actor` header exists only for backward compatibility with the deprecated env bootstrap key, and is **ignored for DB-issued keys**. A leaked key can no longer impersonate someone else by setting a header. The `X-Actor` values shown in some curl examples below are inert for scoped keys — they document the legacy behavior only.

```bash
# Read request — the key must carry the endpoint's :read scope
curl -sS http://localhost:8000/people \
  -H "X-API-Key: tt_ab12cd34_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# Write request — actor is taken from the key name; no header needed
curl -sS -X POST http://localhost:8000/people \
  -H "X-API-Key: tt_ab12cd34_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"display_name": "Alex Chen", "primary_email": "alex@utmist.ca"}'
```

## Error conventions

| Status | When |
|--------|------|
| `400 Bad Request` | Invalid FK on membership create/update (person_id, team_id, or role_kind_id not found); unknown provider on identifier create |
| `401 Unauthorized` | Missing, malformed, or unrecognized `X-API-Key` |
| `403 Forbidden` | Key is valid but lacks the scope the endpoint requires |
| `404 Not Found` | Resource with the given ID or slug does not exist; unknown person/identifier link, or unlinked reverse lookup |
| `409 Conflict` | Uniqueness violation: `primary_email` already taken, team `slug` already taken, person already has that provider linked, or (provider, external_id) belongs to another person |
| `422 Unprocessable Entity` | Pydantic validation failure: wrong type, missing required field, or field rejected by validator (e.g., slug contains uppercase) |

Error responses always include a `detail` field in the JSON body describing the problem.

> The curl examples below use the dev bootstrap key `dev-api-key-change-me` so they work against a fresh local `.env`. In production, substitute a real issued `tt_<prefix>_<secret>` key with the scope the endpoint requires.

---

## API keys (self-introspection)

### GET /api-keys/self

Return the calling key's own name and scopes. **Scope:** none beyond a valid
API key — any authenticated caller can introspect its own key.

Used primarily by consumers (e.g., the discord-bot) to decide at startup
whether they hold the scopes required for the mode they intend to run in.

**Response** (`200 OK`):

```json
{
  "name": "discord-bot-playground",
  "scopes": ["dev:spoof", "identifiers:read", "identifiers:write", "people:read", "people:write"]
}
```

`scopes` is sorted alphabetically. `name` is the exact string used when the
key was issued via `team-tracking-keys issue --name ...`.

**Example:**

```bash
curl -sS http://localhost:8000/api-keys/self \
  -H "X-API-Key: dev-api-key-change-me"
```

Returns `401` if the key is missing or invalid. Never returns `403` (there
is no scope to lack).

---

## People

### POST /people

Create a new person. **Scope:** `people:write`.

**Request body:**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `display_name` | string | yes | Free-form; handles any name convention |
| `primary_email` | string | yes | Normalized to lowercase; must be unique |

**Response:** `Person` object, HTTP 201.

**Errors:** 409 if `primary_email` already exists. 422 if required fields are missing.

```bash
curl -sS -X POST http://localhost:8000/people \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "X-Actor: bootstrap-script" \
  -H "Content-Type: application/json" \
  -d '{"display_name": "Alex Chen", "primary_email": "alex@utmist.ca"}'
```

**Response shape:**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "display_name": "Alex Chen",
  "primary_email": "alex@utmist.ca",
  "active": true,
  "created_at": "2026-06-30T12:00:00Z",
  "updated_at": "2026-06-30T12:00:00Z",
  "created_by": "bootstrap-script",
  "updated_by": "bootstrap-script"
}
```

---

### GET /people/by-email/{email}

Resolve a person by their `primary_email` (case-insensitive). Declared before
`GET /people/{person_id}` so the literal path is not parsed as a UUID.

- **Scope:** `people:read`
- **200** → `Person`
- **404** → no person with that email

Used by the Discord bot to resolve a member's email to their directory Person
during `/link`.

```bash
curl -sS "http://localhost:8000/people/by-email/alex@utmist.ca" \
  -H "X-API-Key: dev-api-key-change-me"
```

---

### GET /people

List all people. **Scope:** `people:read`.

**Query parameters:**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `active_only` | boolean | `false` | If `true`, returns only people with `active = true` |

**Response:** Array of `Person` objects, HTTP 200.

```bash
# All people
curl -sS "http://localhost:8000/people" \
  -H "X-API-Key: dev-api-key-change-me"

# Active people only
curl -sS "http://localhost:8000/people?active_only=true" \
  -H "X-API-Key: dev-api-key-change-me"
```

---

### GET /people/{person_id}

Get a single person by UUID. **Scope:** `people:read`.

**Path parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `person_id` | UUID | |

**Response:** `Person` object, HTTP 200.

**Errors:** 404 if not found.

```bash
curl -sS "http://localhost:8000/people/550e8400-e29b-41d4-a716-446655440000" \
  -H "X-API-Key: dev-api-key-change-me"
```

---

### PATCH /people/{person_id}

Update one or more fields on a person. Only fields present in the request body are changed (standard partial-update semantics). **Scope:** `people:write`.

**Path parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `person_id` | UUID | |

**Request body (all fields optional):**

| Field | Type | Notes |
|-------|------|-------|
| `display_name` | string | |
| `primary_email` | string | Normalized to lowercase; must be unique |
| `active` | boolean | Set to `false` to soft-retire a person |

**Response:** Updated `Person` object, HTTP 200.

**Errors:** 404 if not found. 409 if new `primary_email` conflicts with an existing record. 422 if a field fails validation.

```bash
# Soft-retire a person
curl -sS -X PATCH "http://localhost:8000/people/550e8400-e29b-41d4-a716-446655440000" \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "X-Actor: admin" \
  -H "Content-Type: application/json" \
  -d '{"active": false}'

# Update display name and email together
curl -sS -X PATCH "http://localhost:8000/people/550e8400-e29b-41d4-a716-446655440000" \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"display_name": "Alexandra Chen", "primary_email": "alexandra@utmist.ca"}'
```

---

## Teams

### POST /teams

Create a new team. **Scope:** `teams:write`.

**Request body:**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `slug` | string | yes | Pattern `[a-z0-9_.]+`; must be unique |
| `label` | string | yes | Human-readable name |
| `description` | string | no | One-line summary |
| `parent_id` | UUID | no | FK → teams.id; null = top-level team |

**Response:** `Team` object, HTTP 201.

**Errors:** 409 if `slug` already exists. 422 if slug format is invalid or required fields are missing.

```bash
curl -sS -X POST http://localhost:8000/teams \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "X-Actor: bootstrap-script" \
  -H "Content-Type: application/json" \
  -d '{"slug": "partnerships", "label": "Partnerships", "description": "External relations and sponsorships"}'
```

**Response shape:**

```json
{
  "id": "660e8400-e29b-41d4-a716-446655440001",
  "slug": "partnerships",
  "label": "Partnerships",
  "description": "External relations and sponsorships",
  "parent_id": null,
  "active": true,
  "created_at": "2026-06-30T12:00:00Z",
  "updated_at": "2026-06-30T12:00:00Z",
  "created_by": "bootstrap-script",
  "updated_by": "bootstrap-script"
}
```

---

### GET /teams

List all teams. **Scope:** `teams:read`.

**Query parameters:**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `active_only` | boolean | `false` | If `true`, returns only teams with `active = true` |

**Response:** Array of `Team` objects, HTTP 200.

```bash
curl -sS "http://localhost:8000/teams?active_only=true" \
  -H "X-API-Key: dev-api-key-change-me"
```

---

### GET /teams/{team_id}

Get a single team by UUID. **Scope:** `teams:read`.

**Path parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `team_id` | UUID | |

**Response:** `Team` object, HTTP 200.

**Errors:** 404 if not found.

```bash
curl -sS "http://localhost:8000/teams/660e8400-e29b-41d4-a716-446655440001" \
  -H "X-API-Key: dev-api-key-change-me"
```

---

### GET /teams/by-slug/{slug}

Get a team by its slug. Useful when you know the slug but not the UUID. **Scope:** `teams:read`.

**Path parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `slug` | string | |

**Response:** `Team` object, HTTP 200.

**Errors:** 404 if no team has that slug.

Note: FastAPI routes are matched in declaration order. `GET /teams/by-slug/{slug}` is declared before `GET /teams/{team_id}` in the router, so the literal segment `by-slug` is matched first and never interpreted as a UUID.

```bash
curl -sS "http://localhost:8000/teams/by-slug/partnerships" \
  -H "X-API-Key: dev-api-key-change-me"
```

---

### PATCH /teams/{team_id}

Update one or more fields on a team. **Scope:** `teams:write`.

**Path parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `team_id` | UUID | |

**Request body (all fields optional):**

| Field | Type | Notes |
|-------|------|-------|
| `slug` | string | Pattern `[a-z0-9_.]+`; must be unique |
| `label` | string | |
| `description` | string | Pass `null` to clear |
| `parent_id` | UUID | FK → teams.id; pass `null` to make top-level |
| `active` | boolean | Set to `false` to retire a team |

**Response:** Updated `Team` object, HTTP 200.

**Errors:** 404 if not found. 409 if new `slug` conflicts. 422 if slug format is invalid.

```bash
curl -sS -X PATCH "http://localhost:8000/teams/660e8400-e29b-41d4-a716-446655440001" \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"description": "Sponsors, alumni, and external partnerships"}'
```

---

## Role kinds

Role kinds are the controlled vocabulary for the seniority axis of team roles. The four seed values are `executive`, `director`, `lead`, and `member`. Role kinds are read-only through the API (no create/update endpoints). Both endpoints require scope `role_kinds:read`.

### GET /role_kinds

List all role kinds.

**Query parameters:**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `active_only` | boolean | `false` | If `true`, returns only active role kinds |

**Response:** Array of `RoleKind` objects, HTTP 200.

```bash
curl -sS "http://localhost:8000/role_kinds" \
  -H "X-API-Key: dev-api-key-change-me"
```

**Response shape (one item):**

```json
{
  "id": "lead",
  "label": "Lead",
  "description": null,
  "active": true,
  "created_at": "2026-06-30T12:00:00Z",
  "updated_at": "2026-06-30T12:00:00Z",
  "created_by": "system",
  "updated_by": "system"
}
```

---

### GET /role_kinds/{role_kind_id}

Get a single role kind by its slug ID.

**Path parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `role_kind_id` | string | e.g. `lead`, `executive` |

**Response:** `RoleKind` object, HTTP 200.

**Errors:** 404 if not found.

```bash
curl -sS "http://localhost:8000/role_kinds/lead" \
  -H "X-API-Key: dev-api-key-change-me"
```

---

## Providers

Providers are the controlled vocabulary of identity provider types. The five seed values are `discord`, `github`, `notion`, `uoft_email`, and `email`. They are read-only through the API (no create/update endpoints). Both endpoints require scope `providers:read`.

The `email` provider is special-cased: it is **multi-valued** (a person may have many `email` identifiers, unlike the "one link per provider" rule that governs every other provider) and is managed through its own dedicated endpoint, `POST /people/{id}/emails`, rather than the generic identifier `POST`/`PATCH`/`DELETE` endpoints below. See that section for details.

### GET /providers

List all providers.

**Query parameters:**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `active_only` | boolean | `false` | If `true`, returns only active providers |

**Response:** Array of `Provider` objects, HTTP 200.

```bash
curl -sS "http://localhost:8000/providers" \
  -H "X-API-Key: dev-api-key-change-me"

# Active providers only
curl -sS "http://localhost:8000/providers?active_only=true" \
  -H "X-API-Key: dev-api-key-change-me"
```

**Response shape (one item):**

```json
{
  "id": "discord",
  "label": "Discord",
  "description": "Discord server member ID",
  "active": true,
  "created_at": "2026-06-30T12:00:00Z",
  "updated_at": "2026-06-30T12:00:00Z",
  "created_by": "system",
  "updated_by": "system"
}
```

---

### GET /providers/{provider_id}

Get a single provider by its ID.

**Path parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `provider_id` | string | e.g. `discord`, `github`, `notion` |

**Response:** `Provider` object, HTTP 200.

**Errors:** 404 if not found.

```bash
curl -sS "http://localhost:8000/providers/discord" \
  -H "X-API-Key: dev-api-key-change-me"
```

---

## Person identifiers

A `PersonIdentifier` row records that a person holds an external account on a given provider (e.g., Discord account snowflake, GitHub username). Each person can have at most one link per provider — **except `email`**, which is multi-valued (see [`POST /people/{id}/emails`](#post-peopleperson_idemails) below) and is not addressable through the generic endpoints on this page. Identity mappings are current state, not history: unlinking hard-deletes the row via the DELETE endpoint (a deliberate exception to the "never hard-delete" convention for people/teams/memberships). Re-linking requires unlink-then-relink.

Identity operations require scopes `identifiers:read` (for GET) and `identifiers:write` (for POST/PATCH/DELETE).

### GET /people/by-identifier/{provider}/{external_id}

Reverse lookup: find the person who owns a given external identifier on a provider. This is the primary identity call for external systems (e.g., Discord bot looking up a user by their snowflake). Returns 404 if the identifier is unlinked.

**Path parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `provider` | string | Provider ID (e.g. `discord`) |
| `external_id` | string | The external identifier (e.g. snowflake, username, email) |

**Response:** `Person` object, HTTP 200.

**Errors:** 404 if the identifier is not linked or the provider does not exist.

```bash
curl -sS "http://localhost:8000/people/by-identifier/discord/123456789" \
  -H "X-API-Key: dev-api-key-change-me"
```

---

### GET /people/{person_id}/identifiers

List all linked external identifiers for a person.

**Path parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `person_id` | UUID | |

**Response:** Array of `PersonIdentifier` objects, HTTP 200.

**Errors:** 404 if person not found.

```bash
curl -sS "http://localhost:8000/people/550e8400-e29b-41d4-a716-446655440000/identifiers" \
  -H "X-API-Key: dev-api-key-change-me"
```

**Response shape:**

```json
[
  {
    "id": "880e8400-e29b-41d4-a716-446655440003",
    "person_id": "550e8400-e29b-41d4-a716-446655440000",
    "provider": "discord",
    "external_id": "123456789",
    "handle": "alexchen",
    "created_at": "2026-06-30T12:00:00Z",
    "updated_at": "2026-06-30T12:00:00Z",
    "created_by": "discord-bot",
    "updated_by": "discord-bot"
  }
]
```

---

### POST /people/{person_id}/emails

Add a **verified additional email** to a person, as an `email`-provider `PersonIdentifier`. Unlike every other provider, `email` is **multi-valued**: a person can have many `email` identifiers (e.g. an alumni address, a work address, a secondary school address), on top of their single `primary_email` on the `Person` record. This is the only endpoint that can create an `email` identifier — the generic `POST/PATCH/DELETE /people/{id}/identifiers/{provider}` endpoints below reject `provider="email"`.

**Scope:** `identifiers:write`.

**Path parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `person_id` | UUID | |

**Request body:**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `email` | string | yes | Normalized to lowercase before storage and comparison |

**Response:** `PersonIdentifier` object (`provider: "email"`), HTTP 201.

**Uniqueness — one person per email.** The submitted address is checked against **both** namespaces before it is accepted: every person's `primary_email`, and every existing `email` identifier (belonging to anyone). If the (normalized) address already belongs to a *different* person in either namespace, the request fails with `409 Conflict` (`email_registered_to_another`).

**Idempotent.** If the address is already linked as an `email` identifier for the *same* person, the call succeeds and returns the existing identifier unchanged rather than erroring or creating a duplicate.

**Errors:**
- 404 if person not found
- 409 if the email belongs to a different person (as `primary_email` or as another person's `email` identifier)
- 422 if `email` is missing

```bash
curl -sS -X POST "http://localhost:8000/people/550e8400-e29b-41d4-a716-446655440000/emails" \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "X-Actor: discord-bot" \
  -H "Content-Type: application/json" \
  -d '{"email": "alex.chen@alumni.utoronto.ca"}'
```

---

### POST /people/{person_id}/identifiers

Link an external account to a person.

**Path parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `person_id` | UUID | |

**Request body:**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `provider` | string | yes | Must be an active provider ID; **not** `email` (rejected — use [`POST /people/{id}/emails`](#post-peopleperson_idemails)) |
| `external_id` | string | yes | The stable external identifier (e.g. snowflake, numeric id) |
| `handle` | string | no | Optional human-readable handle or display name |

**Response:** `PersonIdentifier` object, HTTP 201.

**Errors:** 
- 400 if `provider` does not exist or is inactive
- 404 if person not found
- 409 if person already has that provider linked, if (provider, external_id) is linked to a different person, or if `provider="email"` (rejected — use [`POST /people/{id}/emails`](#post-peopleperson_idemails) instead)
- 422 if required fields are missing or validation fails

```bash
curl -sS -X POST "http://localhost:8000/people/550e8400-e29b-41d4-a716-446655440000/identifiers" \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "X-Actor: discord-bot" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "discord",
    "external_id": "123456789",
    "handle": "alexchen"
  }'
```

---

### PATCH /people/{person_id}/identifiers/{provider}

Update an existing link (change `external_id` and/or `handle`).

**Path parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `person_id` | UUID | |
| `provider` | string | Provider ID |

**Request body (all fields optional):**

| Field | Type | Notes |
|-------|------|-------|
| `external_id` | string | New external identifier |
| `handle` | string | New handle; pass `null` to clear |

**Response:** Updated `PersonIdentifier` object, HTTP 200.

**Errors:**
- 404 if person or identifier link not found
- 409 if new `external_id` is already linked to a different person, or if `provider="email"` (rejected — `email` identifiers are managed via [`POST /people/{id}/emails`](#post-peopleperson_idemails), not PATCH)
- 422 if validation fails

```bash
curl -sS -X PATCH "http://localhost:8000/people/550e8400-e29b-41d4-a716-446655440000/identifiers/discord" \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "X-Actor: discord-bot" \
  -H "Content-Type: application/json" \
  -d '{
    "handle": "alexchen2024"
  }'
```

---

### DELETE /people/{person_id}/identifiers/{provider}

Unlink an external account from a person.

**Path parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `person_id` | UUID | |
| `provider` | string | Provider ID |

**Response:** HTTP 204 (No Content).

**Errors:** 404 if person or identifier link not found. 409 if `provider="email"` (rejected — the generic DELETE cannot remove an `email` identifier).

```bash
curl -sS -X DELETE "http://localhost:8000/people/550e8400-e29b-41d4-a716-446655440000/identifiers/discord" \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "X-Actor: admin"
```

---

## Memberships

A `TeamMembership` row records that a person holds a role on a team for a date range. Rows are never deleted; when someone leaves, the row gains an `ended_at` date.

### POST /memberships

Create a new membership. **Scope:** `memberships:write`.

**Request body:**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `person_id` | UUID | yes | FK → people.id; must exist |
| `team_id` | UUID | yes | FK → teams.id; must exist |
| `role_kind_id` | string | no | FK → role_kinds.id; defaults to `"member"` |
| `is_team_admin` | boolean | no | Default `false` |
| `started_at` | date | no | ISO 8601 date (e.g. `"2026-01-15"`); defaults to today at storage layer |
| `ended_at` | date | no | ISO 8601 date; null = currently active |

**Response:** `TeamMembership` object, HTTP 201.

**Errors:** 400 if `person_id`, `team_id`, or `role_kind_id` does not exist. 422 if required fields are missing or types are wrong.

```bash
curl -sS -X POST http://localhost:8000/memberships \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "X-Actor: bootstrap-script" \
  -H "Content-Type: application/json" \
  -d '{
    "person_id": "550e8400-e29b-41d4-a716-446655440000",
    "team_id": "660e8400-e29b-41d4-a716-446655440001",
    "role_kind_id": "executive",
    "is_team_admin": true,
    "started_at": "2026-01-01"
  }'
```

**Response shape:**

```json
{
  "id": "770e8400-e29b-41d4-a716-446655440002",
  "person_id": "550e8400-e29b-41d4-a716-446655440000",
  "team_id": "660e8400-e29b-41d4-a716-446655440001",
  "role_kind_id": "executive",
  "is_team_admin": true,
  "started_at": "2026-01-01",
  "ended_at": null,
  "created_at": "2026-06-30T12:00:00Z",
  "updated_at": "2026-06-30T12:00:00Z",
  "created_by": "bootstrap-script",
  "updated_by": "bootstrap-script"
}
```

---

### GET /memberships

List memberships, with optional filters. All filters are AND-combined. **Scope:** `memberships:read`.

**Query parameters:**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `team_id` | UUID | — | Filter to memberships on this team |
| `person_id` | UUID | — | Filter to memberships held by this person |
| `active_only` | boolean | `false` | If `true`, returns only rows where `ended_at IS NULL` |
| `as_of` | date | — | ISO 8601 date; returns rows where `started_at <= as_of AND (ended_at IS NULL OR ended_at > as_of)` |
| `is_team_admin` | boolean | — | If provided, filters to rows matching this admin flag |

`active_only` and `as_of` can be combined with `team_id` and `person_id` but should not both be used together (they express different active-at conditions).

**Response:** Array of `TeamMembership` objects, HTTP 200.

```bash
# Current members of the Partnerships team
curl -sS "http://localhost:8000/memberships?team_id=660e8400-e29b-41d4-a716-446655440001&active_only=true" \
  -H "X-API-Key: dev-api-key-change-me"

# All memberships Alex currently holds
curl -sS "http://localhost:8000/memberships?person_id=550e8400-e29b-41d4-a716-446655440000&active_only=true" \
  -H "X-API-Key: dev-api-key-change-me"

# Roster as of a past date
curl -sS "http://localhost:8000/memberships?team_id=660e8400-e29b-41d4-a716-446655440001&as_of=2024-12-15" \
  -H "X-API-Key: dev-api-key-change-me"

# Current admins of a team
curl -sS "http://localhost:8000/memberships?team_id=660e8400-e29b-41d4-a716-446655440001&is_team_admin=true&active_only=true" \
  -H "X-API-Key: dev-api-key-change-me"
```

---

### GET /memberships/{membership_id}

Get a single membership by UUID. **Scope:** `memberships:read`.

**Path parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `membership_id` | UUID | |

**Response:** `TeamMembership` object, HTTP 200.

**Errors:** 404 if not found.

```bash
curl -sS "http://localhost:8000/memberships/770e8400-e29b-41d4-a716-446655440002" \
  -H "X-API-Key: dev-api-key-change-me"
```

---

### PATCH /memberships/{membership_id}

Update one or more fields on a membership. Use this for general edits (changing role, toggling admin flag, or setting `ended_at`). For the specific action of closing a membership, `POST /memberships/{id}/end` is more semantically explicit. **Scope:** `memberships:write`.

**Path parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `membership_id` | UUID | |

**Request body (all fields optional):**

| Field | Type | Notes |
|-------|------|-------|
| `role_kind_id` | string | FK → role_kinds.id; must exist |
| `is_team_admin` | boolean | |
| `ended_at` | date | ISO 8601 date; set to close the membership |

**Response:** Updated `TeamMembership` object, HTTP 200.

**Errors:** 404 if not found. 400 if `role_kind_id` does not exist.

```bash
# Promote to lead
curl -sS -X PATCH "http://localhost:8000/memberships/770e8400-e29b-41d4-a716-446655440002" \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "X-Actor: admin" \
  -H "Content-Type: application/json" \
  -d '{"role_kind_id": "lead"}'

# Grant team admin
curl -sS -X PATCH "http://localhost:8000/memberships/770e8400-e29b-41d4-a716-446655440002" \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"is_team_admin": true}'
```

---

### POST /memberships/{membership_id}/end

End a membership by setting its `ended_at` date. This is a semantic shortcut over `PATCH /memberships/{id}` with only `ended_at` — it is clearer at the call site that you are closing the membership, not making a general edit. **Scope:** `memberships:write`.

**Path parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `membership_id` | UUID | |

**Request body:**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `ended_at` | date | yes | ISO 8601 date; the last day of membership |

**Response:** Updated `TeamMembership` object with `ended_at` set, HTTP 200.

**Errors:** 404 if not found.

```bash
curl -sS -X POST "http://localhost:8000/memberships/770e8400-e29b-41d4-a716-446655440002/end" \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "X-Actor: admin" \
  -H "Content-Type: application/json" \
  -d '{"ended_at": "2026-04-30"}'
```

---

## Common queries recipe book

These examples use placeholder UUIDs. Substitute real IDs from your data.

**Who is currently on the Partnerships team?**

```bash
curl -sS "http://localhost:8000/memberships?team_id=<partnerships-uuid>&active_only=true" \
  -H "X-API-Key: dev-api-key-change-me"
```

Returns all membership rows with `ended_at IS NULL` for that team. To resolve person details, call `GET /people/{person_id}` for each `person_id` in the results.

**Who are the current team admins of Partnerships?**

```bash
curl -sS "http://localhost:8000/memberships?team_id=<partnerships-uuid>&is_team_admin=true&active_only=true" \
  -H "X-API-Key: dev-api-key-change-me"
```

**What was the Partnerships roster as of December 15, 2024?**

```bash
curl -sS "http://localhost:8000/memberships?team_id=<partnerships-uuid>&as_of=2024-12-15" \
  -H "X-API-Key: dev-api-key-change-me"
```

Returns rows where `started_at <= 2024-12-15` AND (`ended_at IS NULL` OR `ended_at > 2024-12-15`).

**What teams is Alex currently on?**

```bash
curl -sS "http://localhost:8000/memberships?person_id=<alex-uuid>&active_only=true" \
  -H "X-API-Key: dev-api-key-change-me"
```

**Historical list of VP Partnerships (everyone who has ever held the executive role on Partnerships):**

The schema does not store the string "VP Partnerships." The title is the composite of `role_kind_id = 'executive'` on the Partnerships team. To retrieve the list:

```bash
curl -sS "http://localhost:8000/memberships?team_id=<partnerships-uuid>" \
  -H "X-API-Key: dev-api-key-change-me"
```

Then filter client-side on `role_kind_id == "executive"`. There is no server-side `role_kind_id` filter on `GET /memberships` in v1; if this query is frequent, it is a good candidate for a future query parameter.

**Which person owns this Discord account?**

```bash
curl -sS "http://localhost:8000/people/by-identifier/discord/<snowflake>" \
  -H "X-API-Key: dev-api-key-change-me"
```

This is the reverse-lookup endpoint: given an external identifier (Discord snowflake, GitHub username, UofT email), it returns the `Person` who owns it. Returns 404 if the identifier is not linked. This is the Discord bot's primary identity call.

---

## OpenAPI

The service generates an OpenAPI 3.x schema automatically from the FastAPI routes and Pydantic models.

- `GET /openapi.json` — machine-readable schema (JSON)
- `GET /docs` — Swagger UI (interactive browser)
- `GET /redoc` — ReDoc UI (alternative browser)

The schema reflects the live code and is always up to date. For integration code generation (e.g., a typed client library), use `/openapi.json` as the source.
