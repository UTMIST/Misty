# Contributing

Task walkthroughs for working on `team-tracking`. This assumes you have read the [README](../README.md) and skimmed [ARCHITECTURE.md](ARCHITECTURE.md) — that doc explains the *why*; this one is the *how*.

Written for rotating contributors who may be new to FastAPI, SQLAlchemy, or Postgres. When in doubt, copy the shape of the nearest existing example; the codebase is intentionally repetitive so patterns are easy to imitate.

## Conventions you need to know first

- **The Protocol is the hinge.** `contracts/storage.py` defines `StorageAdapter`, the interface the API depends on. Anything the API needs from the database goes through a method on this Protocol, and **every** method must exist in *both* adapters (`in_memory.py` and `postgres.py`) with identical behavior. Define the contract before you implement it.
- **Dependency direction is one-way.** `contracts/` imports nothing from `src/`. The API imports only `contracts/` + `src/config`. Only `src/api/deps.py` touches `src/storage/`. Don't add imports that cross these lines.
- **DTOs forbid extra fields.** Input models (`*Create`, `*Update`) set `extra="forbid"`, so an unexpected field in a request body is a `422`, not a silent no-op. Add fields to the DTO deliberately.
- **Writes carry an actor.** Every write method takes a keyword-only `actor: str` and stamps it into `created_by`/`updated_by`. At the HTTP layer the actor comes from the API key's name (attested), injected via the `get_actor` dependency — you never read it from a body.
- **Never hard-delete directory records.** Soft-retire people/teams with `active=false`; close memberships with `ended_at`; revoke keys. (The one exception is `person_identifiers`, which are current state and may be deleted.)
- **Errors map by convention.** Storage raises `ValueError` for rule violations; routers translate: uniqueness/duplicate → `409`, bad foreign key / unknown provider → `400`, a `None` return → `404`. Pydantic handles `422`. Auth handles `401`/`403`.

## Local setup

```bash
# From the repo root, enter the service directory first
cd services/team-tracking

cp .env.example .env
docker compose up -d postgres
uv sync --extra dev
uv run alembic upgrade head
```

> Note: the repo is a uv workspace (one root `pyproject.toml`, one root `uv.lock` shared with `packages/auth` and documentation-system). team-tracking depends on the shared `platform-auth` package via `[tool.uv.sources] platform-auth = { workspace = true }`, but `uv sync`/`uv run` from this directory work exactly as shown above.

Run the fast tests to confirm your environment works:

```bash
uv run pytest --ignore=tests/test_postgres_adapter.py
```

## Walkthrough: add a new endpoint

Do these in order — contract first, implementation next, HTTP surface last, tests throughout. Say you're adding `GET /people/{id}` variants or a brand-new operation; the pattern is the same.

1. **Add the method to the Protocol.** In `contracts/storage.py`, add the signature and a docstring describing its semantics (what it returns, what it raises, edge cases). This is the spec both adapters must satisfy.

   ```python
   def deactivate_person(self, person_id: UUID, *, actor: str) -> Person | None:
       """Set active=false. Returns the updated Person, or None if not found."""
       ...
   ```

2. **Add or reuse a Pydantic model/DTO.** If the endpoint takes a body or returns a new shape, define it in `contracts/types.py`. Reuse existing models where you can.

3. **Implement in both adapters.**
   - `src/storage/in_memory.py`: manipulate the in-memory dicts; enforce the same invariants (uniqueness, FK existence) the database would.
   - `src/storage/postgres.py`: write the SQLAlchemy Core statement (`select`/`insert`/`update`/`delete`) against the tables in `schema.py`, and convert rows with the matching `_*_row_to_model` helper. Keep the two implementations behaviorally identical — that's what the shared test suite checks.

4. **Add the route handler.** In the relevant file under `src/api/routers/` (`people.py`, `teams.py`, `role_kinds.py`, `memberships.py`, `providers.py`, `identifiers.py`), add the handler. Copy an existing one for the exact dependency wiring:

   ```python
   @router.post("/{person_id}/deactivate", response_model=Person)
   def deactivate_person(
       person_id: UUID,
       storage: StorageAdapter = Depends(get_storage),
       actor: str = Depends(get_actor),               # attested actor for writes
       _: AuthedKey = Depends(require_scope("people:write")),  # scope gate
   ) -> Person:
       updated = storage.deactivate_person(person_id, actor=actor)
       if updated is None:
           raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="person not found")
       return updated
   ```

   - Pick the right scope on the `require_scope(...)` dependency (read endpoints use `<domain>:read`, writes use `<domain>:write`).
   - Wrap storage calls that can raise `ValueError` in a `try/except` and map to `409` or `400` (see existing handlers).
   - **Route order matters.** FastAPI matches in declaration order, so a literal path (`/by-slug/{slug}`, `/by-identifier/...`) must be declared *before* a `/{id}` catch-all in the same router, or the literal segment gets parsed as an id.

5. **Mount the router if it's new.** A new resource means a new file in `src/api/routers/` and a matching `include_router(...)` line in `src/api/app.py` (also add it to the import block there).

6. **Write tests.** Add API tests to the matching `tests/test_api_<resource>.py` (they use the in-memory adapter through the `TestClient`), and adapter-level tests to `tests/test_in_memory_adapter.py`. Cover the happy path *and* the error paths (404/409/400/403). If the behavior needs real Postgres semantics (a constraint, `citext`, an FK), add a case to `tests/test_postgres_adapter.py` too.

## Walkthrough: add a storage-adapter method (no new endpoint)

Same as steps 1 and 3 above: declare it on the `StorageAdapter` Protocol, then implement it in `in_memory.py` and `postgres.py` with identical semantics, and test both. Not every adapter method has to be exposed over HTTP — some exist purely to support the API layer (e.g. `get_api_key_hash`, `touch_api_key_last_used`).

## Walkthrough: write an Alembic migration

Migrations are **hand-written** and kept in sync with `src/storage/schema.py`. The repo uses simple sequential revision ids (`001`…`004`), not autogenerated hashes — keep that convention.

1. **Change `schema.py` first.** Add/alter the `Table` (or `Column`) in `src/storage/schema.py`. This stays the source of truth for what columns exist.

2. **Create the migration file** at `migrations/versions/005_<short_name>.py`. Copy an existing one (e.g. `004_person_identifiers.py`) for the exact header shape:

   ```python
   """add preferred_name to people

   Revision ID: 005
   Revises: 004
   Create Date: 2026-07-01
   """
   from alembic import op
   import sqlalchemy as sa

   revision = "005"
   down_revision = "004"   # chain to the current head
   branch_labels = None
   depends_on = None


   def upgrade() -> None:
       op.add_column("people", sa.Column("preferred_name", sa.Text, nullable=True))


   def downgrade() -> None:
       op.drop_column("people", "preferred_name")
   ```

   - `down_revision` must point at the previous head (currently `004`).
   - Write a real `downgrade()` — it should exactly reverse `upgrade()`.
   - Adding a column to an existing table? Make it nullable or give it a `server_default` so existing rows migrate cleanly, and mirror that in the corresponding Pydantic model (optional with a default) and in both adapters.
   - Seeding rows? Use `op.bulk_insert(...)` as migrations 002 and 004 do.

3. **Apply and verify.**

   ```bash
   uv run alembic upgrade head      # apply
   uv run alembic downgrade -1      # confirm the downgrade works
   uv run alembic upgrade head      # re-apply
   ```

   Alembic reads `DATABASE_URL` (via `migrations/env.py` → `src/config`), so it targets whatever database your `.env` points at.

## Running the two-mode test suite

**Fast (in-memory, no database) — your default while iterating:**

```bash
uv run pytest --ignore=tests/test_postgres_adapter.py
```

Runs using `InMemoryStorageAdapter`, injected via `app.dependency_overrides[get_storage]`. Covers every endpoint (including `/api-keys/self`), the auth/scope paths (including the `dev:spoof` guard under `TT_ENV=production`), the CLI (including its refusal to issue `dev:spoof` against production), hashing, and the audit log. Finishes in a few seconds.

**Full (adds the Postgres integration tests) — run before you push:**

```bash
docker compose up -d postgres
uv run pytest
```

Runs the fast suite above plus `tests/test_postgres_adapter.py`, which replays adapter behavior against a live Postgres instance (real FK enforcement, `citext`, unique constraints). These require `DATABASE_URL` (in `.env`) to point at the running container; the `clean_db` fixture truncates mutable tables between tests.

> Note: there is currently no environment-variable switch to skip the Postgres tests — the split is by test file. Use `--ignore=tests/test_postgres_adapter.py` for the fast run.

Useful invocations:

```bash
uv run pytest tests/test_api_people.py            # one file
uv run pytest -k identifier                        # by name substring
uv run pytest -x -q                                # stop at first failure, quiet
```

## Linting and formatting

The project uses [ruff](https://docs.astral.sh/ruff/) (line length 100, target py311; see `pyproject.toml`).

```bash
uv run ruff check .            # lint
uv run ruff check --fix .      # lint + auto-fix the safe ones
uv run ruff format .           # format
uv run ruff format --check .   # verify formatting without writing (CI-style)
```

Run both `ruff check` and `ruff format` before opening a PR, and make sure the full test suite passes against Postgres.

## Checklist before you push

- [ ] New/changed storage behavior is on the `StorageAdapter` Protocol and implemented in **both** adapters.
- [ ] Router handler picks the correct scope and maps errors to the right status codes.
- [ ] Literal routes are declared before `/{id}` routes in the same router.
- [ ] `schema.py` and a new Alembic migration agree; `upgrade`/`downgrade` both work.
- [ ] Tests cover happy path and error paths; the full suite (`uv run pytest`) passes with Postgres up.
- [ ] `uv run ruff check .` and `uv run ruff format --check .` are clean.
