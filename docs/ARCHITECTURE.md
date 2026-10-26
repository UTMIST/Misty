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
- [`services/gateway/docs/ARCHITECTURE.md`](../services/gateway/docs/ARCHITECTURE.md)
- [`discord-bot/README.md`](../discord-bot/README.md) — the Discord bot doesn't have a
  standalone ARCHITECTURE.md yet; the README covers its neutral command shape (a
  single handler serves both the Discord surface and a browser-based web
  playground), and its own "Consumer" section below sketches how it sits atop the
  directory.

## The services

The platform has **seven backend services**: six internal domain/processing
services and the public external gateway, plus the Discord bot as a consumer:

| Service | Domain | Key surface |
|---------|--------|-------------|
| [`team-tracking`](../services/team-tracking) | The directory — people, teams, roles, memberships | the org model everything else references |
| [`documentation-system`](../services/documentation-system) | The docs catalog — the org's index of URLs | validates owner ids against team-tracking |
| [`services/llm`](../services/llm) | Stateless LLM proxy | `POST /chat` via OpenAI (default) or Bedrock; `POST /embed` for retrieval; no database |
| [`services/verification`](../services/verification) | Email ownership verification | request/confirm an email code; `verification:write` scope |
| [`services/meeting`](../services/meeting) | **Stateful** live meeting transcription + minutes | WS audio stream in → rolling transcript → minutes/PDF on stop; `meetings` scope. Owns live sessions in one process — see [MEETING-RECORDING.md](MEETING-RECORDING.md) |
| [`services/connectors`](../services/connectors) | Outbound content/discovery/writer adapter | Google/GitHub fetch, Google discovery and meeting-Doc operations; per-route scopes, no local DB. See its [API](../services/connectors/docs/API.md). |
| [`services/gateway`](../services/gateway) | Public external API | Curated `/v1` operations; per-consumer scoped keys in its own database; private HTTP calls to the owning services. |

> **Meeting recording** spans two components (the `meeting` service + a Discord *voice surface* in the bot) with live session state and a dedicated bot voice path. Its cross-cutting design is documented separately in **[MEETING-RECORDING.md](MEETING-RECORDING.md)**.

The rest of this document details the team-tracking ↔ documentation-system
relationship, the platform's main cross-service data flow. It has **two** legs:

1. **Ownership validation** at ingest — resolving an owner id to a label (detailed below).
2. **Visibility resolution** on reads — the catalog asks the directory for an
   actor's active team ids to decide which docs that person may see.

Both use authenticated HTTP through `DirectoryClient`. Owner-label lookup can
degrade during a directory outage. Direct reads preserve their documented
visibility rules; authorized retrieval and meeting acceptance fail closed when
their required directory context cannot be resolved. See the
[catalog authorization and retrieval contract](../services/documentation-system/docs/API.md#authorized-retrieval-post-search).

The other services are independent and share the conventions below:
`verification` owns its own database and migrations; `llm` and `connectors` are
stateless with no database at all; `meeting` has no database either but *is*
stateful in memory.

Other service-to-service dependencies include:

- **`meeting` → `llm`** for minutes and Q&A. Non-local startup requires LLM
  configuration. A minutes-provider failure still permits stop to return the
  transcript/PDF with a placeholder; it does not guarantee generated minutes.
- **`gateway` → `team-tracking`** for resolver requests, which return 503 when
  the directory is unavailable. Gateway's rate limits are process-local;
  [run one replica/worker](../services/gateway/docs/DEPLOYMENT.md).
- **`documentation-system` → `connectors`** for Google/GitHub content and
  Google ACLs, discovery and meeting writes. Fetch warnings can preserve a
  catalog record, but do not establish searchable content or source access.
  Meeting persistence has its own [retry/recovery contract](../services/documentation-system/docs/DEPLOYMENT.md#meeting-record-persistence).
- **`documentation-system` → `llm`** for query/index embeddings. Search fails
  closed when its required embedding capability is unavailable.
- **`meeting` → `documentation-system`** for saved text handoff; the catalog
  owns durable meeting records and uses the connector's Google writer.

## Hosting and releases

Railway owns service builds and process management so rotating maintainers do not
need to operate a host OS. Staging and production use separate environment
configuration, Discord applications and database branches. Each database-backed
service owns its own Neon database; no shared schema couples service releases.

Feature PRs target `staging`; production promotions come from `staging` to `main`.
Use the [deployment runbook](RAILWAY-DEPLOYMENT.md) for setup, verification and
rollback. Record rollout observations in the release PR, where they remain tied
to the revision that was deployed.

## The core principle: a source of truth is API-only

Each service is a **source of truth** for one domain — the directory owns the org
model (people/teams/roles/memberships); the catalog owns the index of the org's URLs.
The defining rule is that **consumers use the owning service’s HTTP API**.
Services do not share tables or import each other’s application code. A service
can own internal processing: the directory runs Google-group reconciliation,
and the catalog owns ingestion/indexing and meeting persistence. External
consumers still use the published HTTP contract.

Why enforce this:

- **The internal language stays invisible.** A service's Pydantic types, its storage
  schema, its `Protocol` boundaries — none of that leaks to consumers. Consumers see
  only the HTTP surface. That means the team can refactor internals (swap a storage
  adapter, restructure tables, rename a class) without breaking anyone downstream.
- **HTTP operations have an OpenAPI contract.** `/openapi.json` describes the
  HTTP surface. Meeting audio uses a WebSocket contract that OpenAPI cannot
  express; its [API reference](../services/meeting/docs/API.md#ws-meetingssession_idstream)
  documents frames and control messages. Consumer integrations must follow the
  relevant published contract.
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
   reuses the existing identity and merges tags/grants. Ordinary repeat ingest
   of an enabled Google source also refreshes content/ACLs and can revoke source
   grants; discovery explicitly opts out of that refresh. See the
   [ingestion contract](../services/documentation-system/docs/API.md).
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
directory outage block best-effort owner-label cataloguing. This does not apply
to operations that need authenticated access context: authorized retrieval and
meeting acceptance can return 503 when the directory is unavailable.

## Shared architectural conventions

Both services are built to the same template, so learning one transfers to the other.
These are platform-wide conventions; each service's own `docs/ARCHITECTURE.md`
describes how it applies them concretely.

- **`contracts/` Protocol boundary.** Each service has a `contracts/` package holding
  Pydantic domain types and `Protocol` interfaces. Application code (ingest, routers)
  depends on the Protocol, never a concrete class. `contracts/` imports nothing from
  `src/`, so the boundary can't erode. The catalog’s Protocols include
  `StorageAdapter`, `Fetcher`, and `DirectoryClient` — and the directory dependency
  described above is just the `DirectoryClient` Protocol with an HTTP implementation.
- **Storage adapter swap.** Every Protocol has a fast in-memory implementation for
  tests and a real one for production. `InMemoryStorageAdapter` vs
  `PostgresStorageAdapter` is the canonical example: swapping to Postgres required
  zero changes to ingest logic or routers. The concrete adapter is wired to its
  Protocol in exactly one place per service (`src/api/deps.py`).
- **Scoped API-key auth.** Protected HTTP endpoints use `X-API-Key`; health and
  schema/UI routes remain unauthenticated within the service’s network exposure. Keys are Argon2-hashed
  in the database and carry a set of per-resource scopes (e.g. `people:read`,
  `docs:write`, with `admin` as a wildcard). Specific privileged operations are
  gated behind their own dedicated scopes rather than a broad write scope — e.g.
  setting a non-`member` `access_level` on a person requires `people:elevate`
  (plain `people:write` cannot escalate), and the `llm` service's `POST /chat`
  requires the `chat` scope; `admin` still satisfies these ordinary checks.
  Person/channel assertions separately require their literal assertion scopes,
  including for admins; see the catalog API contract. A bootstrap env key exists for
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
  four services that own a database (team-tracking, documentation-system,
  verification and gateway); inspect their migration directories for the
  current heads rather than pinning a deployment to an old revision. For
  cleanup/rollback exceptions, follow the service deployment guide. `llm`,
  `meeting`, and `connectors` have no schema.
- **Three auth storage models.** The convention is scoped, argon2-hashed keys
  behind `platform_auth`'s `ApiKeyStore` protocol — but where they *live* varies
  with how many consumers a service has:
  - **team-tracking, documentation-system, gateway** — separate `api_keys`
    tables, minted/revoked with each service’s CLI. Gateway accepts only issued
    `gw_` keys and has no inbound bootstrap env key.
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

## Access architecture: two doors, not one gateway

The platform has exactly two ways in, and they're deliberately asymmetric — there is
**no internal gateway**. Internal services trust each other directly; only external
callers go through a gateway at all.

- **Internal door.** team-tracking, documentation-system, and the gateway's own
  outbound call to team-tracking all authenticate the same way: a per-consumer key,
  argon2-hashed, scoped, issued by the target service's own CLI (`team-tracking-keys`,
  `doc-keys`). The machinery is the shared [`packages/auth`](../packages/auth)
  (`platform_auth`) library described above — each internal service is its own
  authority over its own keys. There is no shared internal proxy standing in front of
  them; adding a service that trusts another means issuing it a key on that service,
  nothing more.
- **External door — [`services/gateway/`](../services/gateway/README.md), implemented.**
  Callers outside the org's trust boundary (a GitHub Action, a future third-party
  integration) never get a team-tracking key. Instead they hold a key issued by the
  gateway's own external registry (`gateway-keys`, scoped e.g. `resolve:discord`), and
  the gateway holds exactly **one** internal team-tracking key
  (`identifiers:read`) for its own outbound calls. It composes and curates — it never
  passes an internal response straight through — and adds the protections an
  externally-facing surface needs that internal services don't: a public Railway
  domain, per-key rate limiting behind a per-IP flood guard, and audit logging of
  every external request.

  The asymmetry runs one level deeper than the key registries. Every internal
  service also accepts an env-bootstrap key (`API_KEY`), which `platform_auth`
  resolves to the wildcard `admin` scope — the grace path you use to reach the
  admin API that issues the first real key, and safe because only the private
  network can reach it. **The gateway does not.** It has no admin API to
  bootstrap (`gateway-keys` writes to its database directly) and it is the one
  service on the public internet, so it passes `get_env_key=lambda: None` and
  every caller must present an issued, scoped key. When adding an
  externally-reachable service, copy that, not the internal shim.

The gateway's first (and so far only) endpoint is the resolver,
`GET /v1/resolve/discord/{github_login}` — it turns a GitHub login into the Discord id
linked to the same person in the directory, and nothing else. It is a working
example for future gateway operations; no current integration consumes it.
The Discord notifications implemented for #34 display GitHub reviewer logins,
but do not call the resolver or ping reviewers' Discord accounts. New external
use cases get new narrow endpoints on the gateway, not broader access to
team-tracking itself.

The compute platform will use the same per-consumer gateway contract when its
required operations are defined. The [gateway architecture](../services/gateway/docs/ARCHITECTURE.md#future-compute-platform-access)
describes how to add access while preserving service-owned databases and access
checks; the current permission grants only the Discord resolver.

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

So the directory supplies identity and meaning, and the catalog stores the
content and access information that retrieval uses.

## Search and retrieval

Authorized lexical/vector retrieval is implemented inside documentation-system
at `POST /search`, using full stored content and LLM embeddings. Consumers call
that HTTP API; the bot exposes `/doc search` and optional helper document tools.
Every search needs a person or channel context, with literal assertion scopes,
and filters candidates before ranking limits. See the
[retrieval contract](../services/documentation-system/docs/API.md#authorized-retrieval-post-search)
and [deployment setup](../services/documentation-system/docs/DEPLOYMENT.md).
Implementation does not establish that any particular live environment has its
embedding provider, keys, corpus and scheduled refresh configured.
