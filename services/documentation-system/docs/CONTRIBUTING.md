# documentation-system — contributing

Task-oriented walkthroughs for people changing the code. Written for a rotating student-org
team with mixed FastAPI/Postgres experience: if a step feels obvious, skip it; if it
doesn't, it's spelled out.

Read [`ARCHITECTURE.md`](ARCHITECTURE.md) first — the walkthroughs below assume you know
the three Protocol boundaries.

## Conventions (read once)

- **Follow the Protocol boundary.** `contracts/` is the stable interface layer and imports
  nothing from `src/`. Domain logic (`src/ingest.py`, `src/url_norm.py`) and routers depend
  on Protocols, never on a concrete adapter. Concrete wiring lives only in
  `src/api/deps.py`.
- **If it touches persistence, it goes through `StorageAdapter`** — and therefore through
  *both* adapters (in-memory and Postgres), or the fast tests and prod diverge.
- **Terminology:** "storage adapter", "Protocol boundary", "scoped API key", "source of
  truth", "degrade-on-directory-down", "ingest". Match the existing docs.
- **Keep the fast suite fast and Docker-free.** New behavior gets an in-memory test; only
  storage-adapter parity needs the Postgres suite.
- **Lint before you push:** `uv run ruff check .` **and** `uv run ruff format --check .` (line length 100, target py311). CI gates both.

## Setup

```bash
# From the repo root, enter the service directory first
cd services/documentation-system

uv sync --extra dev
docker compose up -d postgres      # only needed for the Postgres test suite / running the server
```

> Note: the repo is a uv workspace (one root `pyproject.toml`, one root `uv.lock` shared with `packages/auth` and team-tracking). documentation-system depends on the shared `platform-auth` package via `[tool.uv.sources] platform-auth = { workspace = true }`, but `uv sync`/`uv run` from this directory work exactly as shown above.

---

## Add an endpoint

Endpoints live in `src/api/routers/` (`docs.py`, `sources.py`), grouped by resource.

1. **Pick the router** (or add a new `APIRouter` and `include_router` it in
   `src/api/app.py`).
2. **Declare request/response models.** Response models are the domain types in
   `contracts/types.py`; request bodies are small Pydantic models with
   `model_config = ConfigDict(extra="forbid")` so unknown fields 422 instead of being
   silently dropped (see `TagBody` in `docs.py`).
3. **Gate it with a scope.** Add `_: AuthedKey = Depends(require_scope("docs:read"))` for
   reads or `"docs:write"` for writes. If it mutates and needs an actor, also take
   `actor: str = Depends(get_actor)` and pass it to the storage call.
4. **Inject dependencies** via `Depends`: `get_storage`, and `get_fetchers` /
   `get_directory` if you need them. Never construct adapters inline — that's what makes
   the route testable.
5. **Map domain errors to HTTP.** `None` from storage → `raise HTTPException(404, …)`;
   catch `BadReference` → 400; catch `FetchError` → 502. Follow the patterns already in
   `docs.py`.
6. **Test it** in `tests/test_api_docs.py` (or a new file), using the fixtures that
   override `get_storage` with `InMemoryStorageAdapter`. Assert status codes and body.

---

## Add a new Fetcher

Goal: give an auth-gated source (e.g. Notion) a real content snapshot.

1. **Implement the `Fetcher` Protocol** — a class with
   `def fetch(self, url: str) -> FetchResult`. Put it in `src/fetch/<source>.py`. Return a
   `FetchResult(title=…, content_snapshot=…)`; raise `FetchError` on any retrieval/parse
   failure (never return a half-broken result). Use a timeout on network calls — see
   `WebFetcher` for the httpx pattern.
2. **Register it by `source_id`** in `default_registry()` (`src/fetch/registry.py`):
   ```python
   return FetcherRegistry({
       "web": WebFetcher(),
       "github": GithubFetcher(),
       "notion": NotionFetcher(),   # new
   })
   ```
   The key **must** equal the source's `id` — that's how `fetch_for` finds it.
3. **Enable fetching for that source.** Flip `content_fetch_enabled` to `true` for the
   source. That's a data change in the `sources` table, so write a migration that updates
   the seeded row (see below). Ingest only attempts a fetch when the source's
   `content_fetch_enabled` is true *and* a fetcher is registered.
4. **Handle auth.** If the source `requires_auth`, your fetcher needs credentials. There's
   no fetcher-specific config wired in v1 — add a setting to `src/config.py` (and document
   it in [`DEPLOYMENT.md`](DEPLOYMENT.md)) and thread it through `default_registry()`.
5. **Test** in `tests/test_fetchers.py`: parse-a-known-page, and the `FetchError` path.
   Fetchers accept an injected httpx client, so tests pass a fake instead of hitting the
   network.

---

## Add a storage-adapter method

Any new persistence operation is a **three-part change** — skip a part and the adapters
drift.

1. **Declare it on the Protocol** in `contracts/storage.py`, with a docstring specifying
   the exact semantics (return type, what "not found" returns, idempotency). This is the
   contract both adapters must honor.
2. **Implement it in both adapters, identically:**
   - `src/storage/in_memory.py` — operate on the dicts (`self._docs`, `self._tags`, …).
     Re-hydrate docs with their tags via `_hydrate` before returning.
   - `src/storage/postgres.py` — SQLAlchemy Core against the tables in
     `src/storage/schema.py`. Keep the same return contract (e.g. `None` for missing).
3. **Test parity.** Add the behavioral assertion to the in-memory test
   (`tests/test_in_memory_adapter.py`) *and* mirror it in
   `tests/test_postgres_adapter.py` so the `RUN_PG_TESTS` suite proves the two agree. Use
   `build_seed_sources()` from `conftest.py` for a consistent starting state.

Rule of thumb: if the in-memory suite passes but you didn't touch `postgres.py`, you
probably introduced a divergence.

---

## Write an Alembic migration

Schema and seed data changes go through Alembic (`migrations/versions/`). Migrations are
numbered sequentially (`001`, `002`, …) and chained by `down_revision`.

1. Create `migrations/versions/003_<short_name>.py`. Set `revision = "003"` and
   `down_revision = "002"` (the current head). Copy the header/imports from an existing
   migration.
2. Write `upgrade()` and a real `downgrade()` — every migration must be reversible. Use
   `op.create_table`, `op.add_column`, `op.bulk_insert`, `op.execute(sa.text(...))`, etc.
   Migration `002` is a good template for seed-data changes.
3. **Keep `src/storage/schema.py` in sync.** The Core table definitions there must match
   the post-migration schema — the Postgres adapter and the migrations share that shape.
4. Apply and verify:
   ```bash
   uv run alembic upgrade head
   uv run alembic downgrade -1 && uv run alembic upgrade head   # prove downgrade works
   ```

---

## Running the tests

Two modes:

**Fast (default — in-memory, no Docker):**

```bash
uv run pytest --ignore=tests/test_postgres_adapter.py -q
# 59 passed
```

Uses `InMemoryStorageAdapter` injected via `dependency_overrides`, plus fakes for the
`Fetcher` and `DirectoryClient`. Runs in well under a second. This is the suite you run on
every change.

**Full (adds Postgres integration):**

```bash
docker compose up -d postgres
RUN_PG_TESTS=1 uv run pytest -q
```

`tests/test_postgres_adapter.py` runs the same behavioral assertions against a live
database and is gated behind `RUN_PG_TESTS=1` — without the flag it's skipped (running the
whole suite without the flag reports **59 passed, 7 skipped**). Run this before any change
that touches `postgres.py`, `schema.py`, or a migration.

## Lint

```bash
uv run ruff check .          # lint
uv run ruff check --fix .    # auto-fix what it can
uv run ruff format .         # format
uv run ruff format --check . # verify without writing (CI-style)
```

Ruff config lives in `pyproject.toml` (line length 100, target py311). Keep the tree
warning-clean.
