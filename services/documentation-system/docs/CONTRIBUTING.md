# documentation-system — contributing

Task-oriented walkthroughs for people changing the code. Written for a rotating student-org
team with mixed FastAPI/Postgres experience: if a step feels obvious, skip it; if it
doesn't, it's spelled out.

Read [`ARCHITECTURE.md`](ARCHITECTURE.md) first — the walkthroughs below assume you know
the Protocol boundaries.

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

Goal: give a source without a fetcher (e.g. YouTube) full text for retrieval and a preview.

1. **Implement the `Fetcher` Protocol** — a class with
   `def fetch(self, url: str) -> FetchResult`. Put it in `src/fetch/<source>.py`. Return a
   `FetchResult(title=title, content=text, content_snapshot=text[:2000])` when text is
   extracted. `content` is the full extracted text; the snapshot is only a bounded preview
   and cannot supply searchable content by itself. Return `None` for both content fields
   when nothing is extracted; follow the [Fetcher contract](../contracts/fetcher.py).
   Raise `FetchError` on any retrieval/parse failure (never return a half-broken result).
   Use a timeout on network calls — see `WebFetcher` for the httpx pattern.
2. **Register it by `source_id`** in `default_registry()` (`src/fetch/registry.py`):
   ```python
   mapping: dict[str, Fetcher] = {
       "web": WebFetcher(),
       "youtube": YouTubeFetcher(),   # new
   }
   ```
   Extend the existing mapping; keep its Google/GitHub connector entries.
   The key **must** equal the source's `id` — that's how `fetch_for` finds it.
3. **Enable fetching for that source.** Flip `content_fetch_enabled` to `true` for the
   source. That's a data change in the `sources` table, so write a migration that updates
   the seeded row (see below). Ingest only attempts a fetch when the source's
   `content_fetch_enabled` is true *and* a fetcher is registered.
4. **Handle auth at the owning boundary.** Google and GitHub fetchers already use
   `CONNECTORS_BASE_URL` / `CONNECTORS_API_KEY`; provider credentials belong to
   connectors. Follow that path for another connector-backed source. If a new
   in-process fetcher needs a credential, add a `SecretStr` setting in `src/config.py`,
   `.env.example`, the README and [`DEPLOYMENT.md`](DEPLOYMENT.md), then unwrap it
   only when wiring the fetcher in `default_registry()`.
5. **Test** in `tests/test_fetchers.py`: parse-a-known-page, and the `FetchError` path.
   Fetchers accept an injected httpx client, so tests pass a fake instead of hitting the
   network. Add an ingest test asserting the full extracted text reaches `doc_content`,
   not just the response snapshot, so retrieval has text to index.

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

1. Inspect `migrations/versions/` and run `uv run alembic heads` to find the current
   head. Choose the next unused sequential revision ID, create
   `migrations/versions/<revision>_<short_name>.py`, and set `revision` to that ID
   and `down_revision` to the head you inspected. Copy an existing migration's
   header/imports, replacing its revision, parent, and date.
2. Write `upgrade()` and a real `downgrade()` following the
   [migration rollback rules and explicit retirement exceptions](../../../AGENTS.md#migrations). Use
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
```

Uses `InMemoryStorageAdapter` injected via `dependency_overrides`, plus fakes for the
`Fetcher` and `DirectoryClient`. Runs in well under a second. This is the suite you run on
every change.

**Full (adds Postgres integration):**

```bash
docker compose up -d postgres
export DATABASE_URL=postgresql+psycopg://docs:dev_password@localhost:5434/docs
RUN_PG_TESTS=1 RUN_PG_MIGRATION_TESTS=1 uv run pytest -q
```

`tests/test_postgres_adapter.py` runs the same behavioral assertions against a live
database and is gated behind `RUN_PG_TESTS=1`. That flag also enables older
destructive migration tests; additional migration suites require
`RUN_PG_MIGRATION_TESTS=1` too. Run the full suite before any change that
touches `postgres.py`, `schema.py`, or a migration, using a disposable cluster as
described in the [README testing instructions](../README.md#testing).

## Lint

```bash
uv run ruff check .          # lint
uv run ruff check --fix .    # auto-fix what it can
uv run ruff format .         # format
uv run ruff format --check . # verify without writing (CI-style)
```

Ruff config lives in `pyproject.toml` (line length 100, target py311). Keep the tree
warning-clean.
