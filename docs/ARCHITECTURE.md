# Platform Architecture

How the UTMIST ops platform's services fit together. This document is for someone
deciding *how the pieces relate* — not the internals of either service. For those,
see each service's own `docs/ARCHITECTURE.md`:

- [`services/team-tracking/docs/ARCHITECTURE.md`](../services/team-tracking/docs/ARCHITECTURE.md)
- [`services/documentation-system/docs/ARCHITECTURE.md`](../services/documentation-system/docs/ARCHITECTURE.md)
- [`services/verification/docs/ARCHITECTURE.md`](../services/verification/docs/ARCHITECTURE.md)
- [`services/llm/docs/ARCHITECTURE.md`](../services/llm/docs/ARCHITECTURE.md)
- [`services/meeting/docs/ARCHITECTURE.md`](../services/meeting/docs/ARCHITECTURE.md)
- [`services/connectors/docs/ARCHITECTURE.md`](../services/connectors/docs/ARCHITECTURE.md)
- [`discord-bot/README.md`](../discord-bot/README.md) — the Discord bot doesn't have a
  standalone ARCHITECTURE.md yet; the README covers its neutral command shape (a
  single handler serves both the Discord surface and a browser-based web
  playground), and its own "Consumer" section below sketches how it sits atop the
  directory.

## The services

The platform is **six backend services** — four source-of-truth services, each
owning one domain, plus the stateful `meeting` processing service and the
stateless `connectors` outbound adapter — plus the Discord bot as a consumer:

| Service | Domain | Key surface |
|---------|--------|-------------|
| [`team-tracking`](../services/team-tracking) | The directory — people, teams, roles, memberships | the org model everything else references |
| [`documentation-system`](../services/documentation-system) | The docs catalog — the org's index of URLs | validates owner ids against team-tracking |
| [`services/llm`](../services/llm) | Stateless LLM proxy | `POST /chat` over Amazon Bedrock; requires the `chat` scope; holds no DB/state |
| [`services/verification`](../services/verification) | Email ownership verification | request/confirm an email code; `verification:write` scope |
| [`services/meeting`](../services/meeting) | **Stateful** live meeting transcription + minutes | WS audio stream in → rolling transcript → minutes/PDF on stop; `meetings` scope. The one stateful service — see [MEETING-RECORDING.md](MEETING-RECORDING.md) |
| [`services/connectors`](../services/connectors) | Outbound document fetch | `POST /fetch` returns a Google Doc/Sheet/Slides/Drive file as text; `fetch` scope; no DB/state. Not a gateway and not an authorization boundary — see its [ARCHITECTURE](../services/connectors/docs/ARCHITECTURE.md) |

> **Meeting recording** spans two components (the `meeting` service + a Discord *voice surface* in the bot) and is the platform's one stateful service and its one non-neutral-command bot path. Its cross-cutting design — and *why* it breaks both conventions — is documented separately in **[MEETING-RECORDING.md](MEETING-RECORDING.md)**.

The rest of this document details the team-tracking ↔ documentation-system
relationship, the platform's main cross-service data flow. It has **two** legs:

1. **Ownership validation** at ingest — resolving an owner id to a label (detailed below).
2. **Visibility resolution** on reads — the catalog asks the directory for an
   actor's active team ids to decide which docs that person may see.

Both are plain authenticated HTTP calls through the same `DirectoryClient`
Protocol, and both degrade rather than fail when the directory is down — see
"degrade-on-directory-down" below and
[documentation-system's ARCHITECTURE](../services/documentation-system/docs/ARCHITECTURE.md)
for the visibility rule itself.

The other services are independent and share the conventions below:
`verification` owns its own database and migrations; `llm` and `connectors` are
stateless with no database at all; `meeting` has no database either but *is*
stateful in memory.

There are **two** service-to-service dependencies outside the catalog →
directory pair, and they differ in how hard they are:

- **`meeting` → `llm`** (hard). `meeting` calls `/chat` for minutes generation
  and refuses to boot without `LLM_BASE_URL`/`LLM_API_KEY` outside `local`.
  Deploy `llm` first.
- **`documentation-system` → `connectors`** (soft). The catalog calls `/fetch`
  for Google source content. With connectors unreachable or unconfigured, the
  fetch degrades to a warning on the ingested doc and the catalog works
  normally — the same degrade-on-dependency-down posture it takes toward the
  directory.

## The core principle: a source of truth is API-only

Each service is a **source of truth** for one domain — the directory owns the org
model (people/teams/roles/memberships); the catalog owns the index of the org's URLs.
The defining rule is that **nothing runs inside a source of truth**. There are no
in-process plugins, no shared library that consumers import, no batch jobs reaching
into the database. Every consumer — a Discord bot, a dashboard, the docs catalog
itself — talks to the service the same way: over HTTP, against its published OpenAPI
contract.

Why enforce this:

- **The internal language stays invisible.** A service's Pydantic types, its storage
  schema, its `Protocol` boundaries — none of that leaks to consumers. Consumers see
  only the HTTP surface. That means the team can refactor internals (swap a storage
  adapter, restructure tables, rename a class) without breaking anyone downstream.
- **The OpenAPI contract *is* the boundary.** If it's not in `/openapi.json`, it's
  not part of the contract. This gives a rotating, mixed-fluency org a single, honest,
  machine-readable description of what each service promises — no tribal knowledge
  required, no "ask the person who wrote it."
- **One integration pattern everywhere.** Because there's no privileged in-process
  path, the docs catalog integrates with the directory exactly the way a third-party
  bot would: authenticated HTTP calls. Nothing gets special access it shouldn't have.

## Cross-service data flow: cataloguing a doc

The first of the two legs above is **ownership validation**. When someone
ingests a URL into the catalog and attributes it to a team or person, the catalog
resolves that owner against the directory. Here's the path:

```
  ┌──────────────┐        POST /docs           ┌───────────────────────┐
  │   consumer   │  (url, owner ids, tags)      │  documentation-system │
  │ (bot / human │ ───────────────────────────▶ │     (docs catalog)    │
  │  via curl)   │                              │                       │
  └──────────────┘                              │   ingest_doc():       │
         ▲                                       │   normalize → dedup   │
         │  201/200 + warnings                   │   → source → fetch    │
         │                                       │   → resolve owner ────┼──┐
         └───────────────────────────────────────┤                       │  │ GET /teams/{id}
                                                 └───────────────────────┘  │ GET /people/{id}
                                                                            │ X-API-Key
                                                                            ▼
                                                          ┌────────────────────────────┐
                                                          │        team-tracking       │
                                                          │  (directory / source of    │
                                                          │   truth for the org)       │
                                                          └────────────────────────────┘
```

Step by step, when `POST /docs` arrives at the catalog
([`services/documentation-system/src/ingest.py`](../services/documentation-system/src/ingest.py)):

1. **Normalize + dedup.** The URL is normalized; if it's already catalogued, ingest
   is idempotent — new tags merge onto the existing doc and nothing else happens.
2. **Derive source + fetch.** The URL's source kind (`web`, `github`, `gdrive`, …) is
   derived, and if that source has fetching enabled the catalog grabs a best-effort
   title/content snapshot. Fetch failures become warnings, not errors.
3. **Validate ownership against the directory.** For each supplied `owning_team_id` /
   `owning_person_id`, the catalog calls the directory over HTTP
   ([`src/directory/http_client.py`](../services/documentation-system/src/directory/http_client.py))
   — `GET /teams/{id}` or `GET /people/{id}` — to fetch a display label. Three
   outcomes:
   - **Found (2xx):** the label is stored alongside the id. The doc now carries a
     human-readable owner.
   - **Not found (404):** the id is genuinely wrong. Ingest rejects the request with
     `400 Bad Reference` — the directory was reachable enough to *confirm* the id
     doesn't exist.
   - **Directory unavailable (connection error / 5xx):** this is
     `degrade-on-directory-down`. The client raises `DirectoryUnavailable`, ingest
     catches it, **stores the owner id, leaves the label null, and attaches a
     warning**. The doc is still created.
4. **Backfill later.** A degraded label isn't permanent. The next time that doc is
   read or updated while the directory is reachable, the catalog resolves and
   persists the missing label (`_backfill_labels` in
   `services/documentation-system/src/api/routers/docs.py`).

The crucial distinction: **a wrong id is a client error; an unreachable directory is
not.** The catalog refuses to invent ownership, but it also refuses to let a
directory outage block cataloguing work. Availability of the catalog never depends on
availability of the directory.

## Shared architectural conventions

Both services are built to the same template, so learning one transfers to the other.
These are platform-wide conventions; each service's own `docs/ARCHITECTURE.md`
describes how it applies them concretely.

- **`contracts/` Protocol boundary.** Each service has a `contracts/` package holding
  Pydantic domain types and `Protocol` interfaces. Application code (ingest, routers)
  depends on the Protocol, never a concrete class. `contracts/` imports nothing from
  `src/`, so the boundary can't erode. The catalog has three such Protocols —
  `StorageAdapter`, `Fetcher`, and `DirectoryClient` — and the directory dependency
  described above is just the `DirectoryClient` Protocol with an HTTP implementation.
- **Storage adapter swap.** Every Protocol has a fast in-memory implementation for
  tests and a real one for production. `InMemoryStorageAdapter` vs
  `PostgresStorageAdapter` is the canonical example: swapping to Postgres required
  zero changes to ingest logic or routers. The concrete adapter is wired to its
  Protocol in exactly one place per service (`src/api/deps.py`).
- **Scoped API-key auth.** Every request carries `X-API-Key`. Keys are Argon2-hashed
  in the database and carry a set of per-resource scopes (e.g. `people:read`,
  `docs:write`, with `admin` as a wildcard). Specific privileged operations are
  gated behind their own dedicated scopes rather than a broad write scope — e.g.
  setting a non-`member` `access_level` on a person requires `people:elevate`
  (plain `people:write` cannot escalate), and the `llm` service's `POST /chat`
  requires the `chat` scope; `admin` still satisfies these. A shared bootstrap env key exists for
  local dev; production uses per-consumer keys issued via each service's CLI
  (`team-tracking-keys`, `doc-keys`). The auth machinery itself (key hashing, the
  scope model, the `build_auth(...)` FastAPI deps, and the audit-log middleware)
  lives once in the shared [`packages/auth`](../packages/auth) package
  (`platform_auth`) — a pure leaf with no dependency on either service's `src/` or
  `contracts/` — and each service consumes it via a ~15-line shim that binds its own
  key prefix (`tt_` for team-tracking, `doc_` for documentation-system) and config.
  This is a repo-level workspace dependency shared between the two services, not a
  dependency of one service on the other.
- **Attested actor.** The actor recorded on audit fields (`created_by` / `updated_by`)
  is the authenticated key's own identity — not a value the caller supplies. A
  consumer cannot claim to be someone else. (The directory additionally accepts an
  `X-Actor` hint; the catalog does not — it always stamps the key's name.)
- **Per-request audit middleware.** An audit-log middleware records every request with
  its resolved actor, giving a per-request trail across a org where the operator set
  turns over.
- **Alembic migrations.** Schema is versioned as Alembic migrations applied with
  `alembic upgrade head`. The SQLAlchemy Core table definitions in
  `src/storage/schema.py` are the schema source of truth. This applies to the
  three services that own a database (team-tracking **007**,
  documentation-system **006**, verification **001**); `llm`, `meeting`, and
  `connectors` have no schema and therefore no `preDeployCommand`.
- **Three auth storage models.** The convention is scoped, argon2-hashed keys
  behind `platform_auth`'s `ApiKeyStore` protocol — but where they *live* varies
  with how many consumers a service has:
  - **team-tracking, documentation-system** — an `api_keys` table, minted by a
    CLI that writes to it (`team-tracking-keys`, `doc-keys`). Revocation is a
    row update.
  - **`llm`, `meeting`, `connectors`** — no table. Keys are seeded at boot from
    a `CONSUMER_KEYS` JSON array env var, so their "mint" CLIs only *print* a key
    and its JSON entry. Adding or revoking one is a variable edit plus a
    redeploy, and a malformed `CONSUMER_KEYS` fails the deploy at boot by design.
  - **`verification`** — a `NullApiKeyStore`: no per-consumer keys exist, and
    only the bootstrap `API_KEY` env var authenticates. It has exactly one
    consumer (the bot), so issuing keys would be ceremony without benefit.

  The protocol is what makes this vary cleanly — each service picks a store, and
  no route code changes.
- **Row-level visibility, where a domain needs it.** Scopes gate *endpoints*;
  documentation-system adds a second layer gating *rows* (who may see which doc),
  driven by an actor supplied via `X-On-Behalf-Of` rather than by the key alone.
  No other service needs this today.

## Why the directory is built first

The build order — **directory → docs catalog → search** — isn't arbitrary. It follows
a hidden dependency:

> You can't tag a doc with a *meaningful* org owner until there's an org model to
> point at.

Ownership is the whole reason the catalog is more than a bookmark folder. "The
Partnerships team owns this budget sheet" is only useful if "the Partnerships team" is
a real, resolvable, durable entity — one that outlives the person who added the doc.
That entity lives in the directory. Build the catalog first and "owner" degenerates
into a free-text string that rots exactly the way the links it's trying to organize
do.

So the directory comes first because it supplies *identity and meaning* to everything
downstream. The catalog comes second because it consumes that meaning. Search comes
last (and is deferred) because it indexes what the catalog has already captured —
there's nothing to search until docs are catalogued, and the content snapshots search
will index are already being stored in anticipation.

## What's deferred

The **search / retrieval plugin** is designed but not built. When it lands it will
index the catalog's stored content snapshots for full-text and semantic search.
Consistent with the API-only principle, it will be another HTTP consumer of the
catalog — not something running inside it. It is out of scope until the catalog has
enough content to make search worthwhile.
