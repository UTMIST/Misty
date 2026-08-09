# AGENTS.md

Instructions for AI coding agents working in this repository. Humans should read [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) instead — it's the onboarding guide. This file is the compressed version plus the things an agent gets wrong.

## What this repo is

The UTMIST ops platform: six Python HTTP services, a Node Discord bot, and one shared auth library, in a single uv workspace. It's deployed and running in production — **changes here reach a live system.** Merges to `staging` auto-deploy to Railway staging; merges to `main` auto-deploy to production.

```
services/team-tracking/         directory (people/teams/roles) — 8000, Postgres 5433
services/documentation-system/  docs catalog                   — 8001, Postgres 5434
services/llm/                   Bedrock /chat proxy            — 8002, no DB
services/verification/          email one-time codes           — 8003, Postgres 5434 ⚠
services/meeting/               live transcription → minutes   — 8004, no DB, STATEFUL
services/connectors/            Google doc fetch adapter       — 8005, no DB
packages/auth/                  platform_auth — shared API-key auth library
discord-bot/                    Discord frontend + web playground
```

## Before you start

1. **Read the service's own `docs/CONTRIBUTING.md`** before changing it. Every service has one, and each ends with a pre-push checklist specific to that service. They are not boilerplate — `verification`'s opens with a warning about the confirm-code state machine, `meeting`'s explains why you must not buffer audio.
2. **Copy the nearest existing example.** The codebase is intentionally repetitive so patterns are easy to imitate. Find the closest existing case and mirror its shape rather than inventing a new one.
3. **Prefer the smallest change that works.** This is a student org with rotating maintainers; clever is a liability.

## Hard invariants

Violating any of these is a bug even if tests pass.

### Architecture

- **The `contracts/` Protocol boundary is one-way.** `contracts/` imports nothing from `src/`. Application code depends on Protocols, never on a concrete implementation. `src/api/deps.py` (or `wiring.py` in `meeting`) is the **single wiring point** where concrete implementations get attached — that's what makes tests able to inject fakes.
- **Every storage Protocol method exists in BOTH adapters** (`in_memory.py` and `postgres.py`) with identical behavior. Adding one to only the Postgres adapter breaks the fast test suite silently. Define the Protocol first, then implement twice.
- **Adapters and providers never import FastAPI.** If you want `HTTPException` inside `src/storage/`, `src/providers/`, `src/sources/`, or `src/email/`, you actually want a domain error the router maps. Conversely, `src/api/` never imports a vendor SDK.
- **Services never share tables or import each other.** All communication is authenticated HTTP. The only shared code is `packages/auth`, a pure leaf.

### Security

- **Credentials are `pydantic.SecretStr`, never `str`.** Every credential field in a service's `Settings`. Unwrap with `.get_secret_value()` only at the boundary where it's used. A plain `str` once printed a real Google private key into a transcript. See [`packages/auth/README.md`](packages/auth/README.md#credential-config-convention) — forgetting to unwrap fails *silently*, not loudly.
- **The actor is attested, never claimed.** `created_by`/`updated_by` come from the authenticated key's own name via the `get_actor` dependency. Never read an actor from a request body.
- **Never log a secret, a one-time code, or document content.** The audit log records actor, endpoint, status, duration, and small structured extras — nothing else.
- **Never hard-delete directory records** (team-tracking). Soft-retire people/teams with `active=false`, close memberships with `ended_at`, revoke keys. The one exception is `person_identifiers`, which are current state.
- **Anything checkable without a paid or destructive call must fail first.** Validation (422) and scope checks (403) happen before the provider/database is touched.

### HTTP conventions

- **DTOs forbid extra fields — where the convention is actually applied.** `team-tracking`, `documentation-system`, and `verification` set `extra="forbid"` on input models, so an unexpected field is a 422 rather than a silent no-op. **`llm` and `connectors` do not** — their `contracts/` models declare no `model_config`, so Pydantic's `extra="ignore"` default applies and unknown fields are silently dropped. Follow the convention in new models; don't assume it already holds when reading an existing one.
- **Errors map by convention.** Storage raises `ValueError` for rule violations; routers translate: duplicate → `409`, bad FK / unknown provider → `400`, `None` return → `404`. Pydantic gives `422`; auth gives `401`/`403`.
- **Route order matters.** FastAPI matches in declaration order — a literal path (`/by-slug/{slug}`) must be declared **before** a `/{id}` catch-all in the same router, or the literal segment gets parsed as an id.
- **Pick the right scope** on `require_scope(...)`: reads use `<domain>:read`, writes `<domain>:write`. Privileged operations get their own scope (`people:elevate` — plain `people:write` cannot escalate access level).

### Migrations

- **Hand-written, sequential ids.** `001`, `002`, … — not autogenerated hashes. Copy an existing file's header shape.
- **Change `src/storage/schema.py` first** — the SQLAlchemy Core tables are the schema source of truth.
- **`down_revision` points at the current head**, and `downgrade()` must actually reverse `upgrade()`. Verify the round trip: `upgrade head` → `downgrade -1` → `upgrade head`.
- **New columns on existing tables are nullable or have a `server_default`**, mirrored in the Pydantic model and both adapters.
- Current heads: team-tracking **007**, documentation-system **006**, verification **001**. `llm`, `meeting`, `connectors` have no schema. (Verify before relying on these — `ls services/<service>/migrations/versions/` is authoritative, this line is not.)

### Configuration

- **`.env` is never committed.** Every service ships a `.env.example` with working local defaults. Add new settings to both `Settings` and `.env.example`.
- **Decide deliberately about the boot check.** `verify_production_secrets()` runs outside `*_ENV=local` and refuses to start on dev defaults. If a missing value would be confusing at request time, add it there — a misconfigured deploy should die at boot, not on first request.

## Workflow

- **Branch off `staging`. Never commit to `main`.** A CI guard (`main-source-guard.yml`) rejects PRs to `main` that don't originate from `staging`.

  ```bash
  git switch staging && git pull
  git switch -c your-feature
  ```

- **Keep a PR inside one CODEOWNERS zone.** `pr-zone-check.yml` warns (non-blocking) when a PR spans multiple zones. Zones are the service/package directories plus `docs/`, `scripts/`, `.github/`, and `root` (anything else, including this file). If a change genuinely spans zones — a protocol change touching both `meeting` and `discord-bot` — that's fine, but it should be deliberate, not incidental.
- **Open PRs into `staging`.** Green CI is required.
- **Don't commit or push unless asked.** Especially don't push to `staging` or `main` directly.
- **Issue bodies use `Blocked by: #40, #42`** to drive the `blocked`/`ready` labels (`blocked-ready-automation.yml`). Case-insensitive, colon optional.

### Run before you push

For a DB-backed service (team-tracking, documentation-system, verification):

```bash
cd services/<service>
uv run pytest --ignore=tests/test_postgres_adapter.py   # fast, in-memory
uv run pytest                                           # full, needs Postgres up
uv run ruff check . && uv run ruff format --check .
```

`llm`, `meeting`, and `connectors` have no Postgres adapter — just `uv run pytest`. The bot: `cd discord-bot && npm test`. The shared library: `cd packages/auth && uv run pytest`.

Run what CI runs for that service — `.github/workflows/ci.yml` is authoritative, and [`docs/DEPLOYMENT-HISTORY.md`](docs/DEPLOYMENT-HISTORY.md#ci-on-every-pr-to-staging-or-main) lists what each of the ten jobs covers — rather than the generic pair, and don't quote test counts in docs or commit messages — they go stale immediately.

> **Every Python service is gated on both `ruff check` and `ruff format --check`.** There are no deferrals left. (The one job that runs neither is `python-test`, which only runs team-tracking's pytest — team-tracking's linting lives in the separate `python-lint` job.) If a job ever needs an exemption, `ci.yml` carries a comment saying so, and that file is the authority, not this one.
>
> Run both before pushing. A formatting-only diff is fine on its own, but **never bundle a reformat with a behavior change** — the diff becomes unreviewable.
>
> Use the ruff version pinned in `uv.lock` (`uv run ruff …` does this for you). Formatter output drifts between ruff versions, so a system-wide `ruff` can produce a diff CI disagrees with.

## Gotchas that will cost you an hour

- **`uv --project` is mandatory for the key CLIs.** Both `services/team-tracking` and `services/documentation-system` declare a top-level `src` package with a console script at `src.cli:main`. In the shared workspace venv they collide, so a bare `team-tracking-keys …` can resolve documentation-system's CLI and mint a `doc_`-envelope key that team-tracking rejects. Always `uv --project services/<service> run <service>-keys …`, and verify the token's prefix.
- **documentation-system and verification both bind host port 5434.** They cannot run locally at the same time. Remap one (`-p 5435:5432`) and update its `DATABASE_URL`. If Alembic reports an unknown revision, you're almost certainly pointed at the other service's database.
- **documentation-system's Swagger is at `/swagger`, not `/docs`** — `/docs` is a real docs-resource router on that service.
- **`npm run dev:web` occupies port 8001 and 5433**, and needs Docker. Stop documentation-system first.
- **`meeting` is stateful.** One process must own a session end-to-end. Its sessions live in memory and a restart drops every in-flight meeting.
- **No `ffmpeg` binary is needed anywhere**, including `meeting` — PyAV bundles its own libraries and nothing shells out.
- **Never write a test that makes a real AWS, Google, or LLM call.** Every suite runs offline against fakes injected via `app.dependency_overrides`. Keep it that way.

## Where to look

| Question | Read |
|---|---|
| How do the services relate? | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| How do I run this locally? | [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) |
| How do I change service X? | `services/X/docs/CONTRIBUTING.md` |
| What does endpoint Y do? | `services/X/docs/API.md` |
| Why is X built this way? | `services/X/docs/ARCHITECTURE.md` |
| How do I deploy / what env vars? | `services/X/docs/DEPLOYMENT.md`, [`docs/RAILWAY-DEPLOYMENT.md`](docs/RAILWAY-DEPLOYMENT.md) |
| Why was this decided? | [`docs/DEPLOYMENT-HISTORY.md`](docs/DEPLOYMENT-HISTORY.md) |
| How does `/record` work? | [`docs/MEETING-RECORDING.md`](docs/MEETING-RECORDING.md) |

## Documentation is part of the change

This repo's docs are unusually detailed and are expected to stay true. If your change makes a statement in a doc wrong, fix the doc in the same PR:

- New endpoint → the service's `docs/API.md`.
- New config setting → the README's config table **and** `.env.example`.
- New convention or trade-off → the service's `docs/ARCHITECTURE.md`.
- New scope, new CLI step, new deploy variable → the service's `docs/DEPLOYMENT.md` and [`docs/RAILWAY-DEPLOYMENT.md`](docs/RAILWAY-DEPLOYMENT.md).
- Anything an agent would get wrong → this file.

Don't restate what another doc already says — link to it. Repeated prose is what drifts.
