# Issue #176 implementation audit

Review record for [#176](https://github.com/UTMIST/Misty/issues/176).
The consumer contract and portable fixture live in
[API.md](API.md#authorized-retrieval-post-search); ranking, authorization, and
the keyword scan trade-off are documented in
[ARCHITECTURE.md](ARCHITECTURE.md#authorized-retrieval).

## File rationale

Paths are relative to this service unless marked repository-root.

| File | Why it changes |
|---|---|
| `contracts/retrieval.py` | Defines strict search DTOs, chunk citations, validated query embeddings, the embedding Protocol, and safe dependency errors. |
| `contracts/storage.py` | Requires an explicit person/channel for hybrid search and specifies adapter parity, candidate limits, ranking, and failure behavior. |
| `contracts/visibility.py` | Extends the existing grant rule with channel source-group entitlement, without inventing another grant store. |
| `contracts/directory.py` | Defines live channel-to-synced-group resolution at the existing directory boundary. |
| `src/retrieval.py` | Shares validation, portable keyword rules, candidate limits, and deterministic RRF. |
| `src/embedding.py` | Calls authenticated platform `/embed` with pooled HTTP, timeout handling, model compatibility, and strict response checks. |
| `src/storage/in_memory.py` | Implements explicit-context filtering before both candidate limits for offline consumers. |
| `src/storage/postgres.py` | Compiles channel grants and runs both candidate paths over an authorized materialized CTE in one snapshot. |
| `src/directory/http_client.py` | Reads #238 channel configuration and healthy managed-group mappings through authenticated HTTP. |
| `src/api/routers/search.py` | Enforces read/assertion scopes and exactly one context, then resolves scope, embeds, searches, and maps safe dependency errors. Keeps malformed Unicode validation errors serializable. |
| `src/api/deps.py` | Wires the concrete embedding client at the existing composition point. |
| `src/api/app.py` | Registers the single authorized search route. |
| `src/config.py` | Adds SecretStr LLM credentials, model selection, absolute relevance floor, and deliberate catalog-preserving boot behavior. |
| `.env.example` | Provides local defaults for the new retrieval settings. |
| `docs/fixtures/retrieval.json` | Supplies one portable synthetic seed and request/response fixture for both consumer projects. |
| `tests/retrieval_cases.py` | Loads that fixture and shares ranking, authorization, revocation, model, and citation assertions between adapters. |
| `tests/test_retrieval.py` | Runs the shared cases offline. |
| `tests/test_postgres_adapter.py` | Runs the same cases on pgvector and verifies context rejection before opening a retrieval connection. |
| `tests/test_api_search.py` | Verifies the portable HTTP exchanges, strict input, trusted contexts, live scope changes, and fail-closed dependency errors. |
| `tests/test_embedding.py` | Exercises the outbound HTTP contract and malformed/incompatible/provider-error handling with MockTransport. |
| `tests/test_directory_client.py` | Verifies authenticated channel/group resolution and fail-closed configuration errors. |
| `tests/test_config.py` | Checks new configuration bounds, model allowlist, and credential redaction. |
| `README.md` | Adds search discovery, settings, file map, and the current indexing/integration status. |
| `docs/API.md` | Publishes the shared request/response, citation, authorization, ranking, and error contract. |
| `docs/ARCHITECTURE.md` | Records context construction, filter-before-ranking SQL, floor/fusion rules, and portable keyword trade-offs. |
| `docs/CONTRIBUTING.md` | Keeps the Protocol-boundary reference accurate after adding embeddings. |
| `docs/DEPLOYMENT.md` | Documents keys, model compatibility, new variables, boot behavior, and release gates. |
| Repository-root `scripts/provision-directory-key.sh` | Adds the read-only `channels:read` permission needed by the new directory lookup. |
| Repository-root `docs/RAILWAY-DEPLOYMENT.md` | Keeps the live environment and key-provisioning instructions aligned with retrieval. |
| Repository-root `AGENTS.md` | Prevents reuse of direct-document unfiltered contexts or admin wildcard assertions in retrieval. |
| `docs/RETRIEVAL-AUDIT.md` | Supplies this file-level review and verification record. |

The `root`, `docs`, and `scripts` zone changes are deliberate consequences of
the service's new assertion/configuration contract. No service schema, migration,
provider SDK, consumer implementation, or production data changes are required.

## Verification

Verified locally on 2026-10-16 with the lockfile-pinned dependencies in an isolated
Linux virtual environment:

- Complete offline documentation-system suite.
- Fresh `alembic upgrade head` on a disposable `pgvector/pgvector:0.8.2-pg16`
  container, then the complete suite with `RUN_PG_TESTS=1` and
  `RUN_PG_MIGRATION_TESTS=1`, including existing migration round trips.
- Matching offline/Postgres seeded cases, including better-ranked unauthorized
  chunks exceeding each candidate cap, private shares, channel group union,
  no authorized results, expiry/revocation, changed scope, incompatible models,
  zero vectors, keyword-only eligibility, and Unicode citation offsets.
- All imported `src`, `contracts`, and `tests` module paths were pinned and
  verified against this checkout, before collection and after completion.
- `uv run ruff check .`, `uv run ruff format --check .`, shell syntax checking
  for the provisioning script, and `git diff --check`.

Provider-dependent automated tests use fakes or MockTransport. No live embedding,
Google, directory, Railway, or Neon calls were needed. The API/fixture were written
before the engine. Verification required no pushes, external messages, or deployments.

## Consumer handoff and remaining release gates

The checked-in contract/fixture is ready for William (#71) and Jessica (#210).
Their agreement has not been obtained or represented as complete here.
Index-to-retrieval lifecycle acceptance (#277), real bot/DM/channel acceptance
(#278), and evaluation/calibration (#178) remain separate work. The keyword branch
currently scans normalized eligible text rather than using the raw-text GIN
index; measure real-corpus latency before choosing an indexed normalization.
