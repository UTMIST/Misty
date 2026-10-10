# Issue #174 implementation audit

Review record for [#174](https://github.com/UTMIST/Misty/issues/174).
The behavior and trade-offs are documented in the
[chunk storage contract](ARCHITECTURE.md#doc_chunks); rollout requirements are in
[DEPLOYMENT.md](DEPLOYMENT.md#postgres).

## File rationale

Paths below are relative to this service unless marked as repository-root files.

| File | Why it changes |
|---|---|
| `contracts/types.py` | Defines the validated, database-independent `DocChunk` value and float32 representation. |
| `contracts/storage.py` | Specifies atomic replacement, validation, missing-document behavior, and visibility-aware reads. |
| `src/storage/schema.py` | Defines the vector table, provenance fields, constraints, and generated keyword index as schema source of truth. |
| `src/storage/chunks.py` | Shares batch validation between adapters so malformed sets fail before persistence. |
| `src/storage/in_memory.py` | Implements the new Protocol methods for offline consumers and tests. |
| `src/storage/postgres.py` | Implements transactional replacement with a parent-row lock and authorized reads. |
| `migrations/versions/007_doc_chunks.py` | Enables the extension and creates/reverses the chunk schema while preserving the database-level extension on rollback. |
| `pyproject.toml` | Adds the SQLAlchemy pgvector integration dependency. |
| Repository-root `uv.lock` | Locks pgvector and its NumPy dependency for supported Python versions. |
| `docker-compose.yml` | Supplies the database extension in local Postgres. |
| Repository-root `.github/workflows/ci.yml` | Supplies the same pgvector image in the existing documentation-system Postgres job. |
| `tests/chunk_storage_cases.py` | Shares behavioral assertions across both adapter suites. |
| `tests/test_in_memory_adapter.py` | Runs those assertions against the in-memory implementation. |
| `tests/test_postgres_adapter.py` | Runs parity assertions plus database-access ordering, rollback, and concurrent replacement checks. |
| `tests/test_migration_007.py` | Verifies upgrade/downgrade with separate extension/schema owners, preserved catalog/content and unrelated vector data, vector width, SQL constraints, keyword metadata, and cascade behavior. |
| `README.md` | Updates the storage overview, file map, and local test prerequisites. |
| `docs/ARCHITECTURE.md` | Records the implemented storage semantics and links them to the existing exact-scan decision. |
| `docs/DEPLOYMENT.md` | Documents extension availability, the new migration, and downgrade consequences. |
| Repository-root `docs/RAILWAY-DEPLOYMENT.md` | Links operators to the new Neon extension prerequisite. |
| Repository-root `AGENTS.md` | Updates the documentation-system migration head. |
| `docs/CHUNK-STORAGE-AUDIT.md` | Records file-level justification and verification for review. |

The changes outside the documentation-system zone are deliberate: its CI database
image, shared dependency lock, deployment runbook, and recorded migration head must
agree with the new storage prerequisite.

## Verification

Verified locally on 2026-10-09 using an isolated disposable
`pgvector/pgvector:0.8.2-pg16` database:

- Fresh `alembic upgrade head` from an empty database.
- The full documentation-system pytest suite with `RUN_PG_TESTS=1`, including
  `head -> 006 -> head` and upgrading when the extension is already enabled.
  The separate-owner regression first reproduced `must be owner of extension vector`
  on the original downgrade, then passed with the fix. It verifies that rollback
  preserves the extension's owner and unrelated vector data, and that re-upgrade
  succeeds for the unprivileged migration role.
- Imported `src`, `contracts`, and `tests` were pinned to this checkout before
  collection, and their loaded module paths were verified afterward.
- `uv run ruff check .` and `uv run ruff format --check .`.
- The production Dockerfile build with its frozen lockfile and Python 3.11 runtime,
  followed by the CI application-import smoke test and a pgvector import.

All provider-dependent tests use offline fakes. No embedding calls or live Neon
changes were required.

## Merge coordination

The inspected staging head was `25701c7`, with migration `006`. Open
[PR #261](https://github.com/UTMIST/Misty/pull/261) also proposes `007`; if it merges
first, renumber this migration and its test, update `down_revision`, and update
the migration references before merging. The storage DTO uses the offset convention
proposed by [PR #259](https://github.com/UTMIST/Misty/pull/259) without depending on
its unmerged chunker implementation.
