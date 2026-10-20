# Ingestion-to-authorized-retrieval verification (#277)

Acceptance record for [#277](https://github.com/UTMIST/Misty/issues/277).
Indexing (#175), retrieval (#176), and source-access reconciliation (#76) already
meet at the catalog API. This change verifies that boundary without adding another
indexer, grant model, scheduler, or consumer integration.

## What the automated acceptance drives

`tests/ingestion_retrieval_cases.py` is shared by the in-memory and Postgres suites.
Only storage selection is overridden in the application. The normal dependency
factories read Settings and unwrap synthetic credentials; real connector,
directory, indexing-embedding and query-embedding HTTP clients use MockTransport.
Unexpected live HTTP transport calls fail the test. Chunking, index replacement,
grant resolution, context construction, query filtering, ranking and response
serialization all execute normally. No case seeds chunks or substitutes an
indexer/search result.

The existing [portable retrieval fixture](fixtures/retrieval.json) supplies people,
teams, keys, document bodies, deterministic vectors and exact request/response
exchanges. Its documents enter through `POST /docs`. For manual-only examples,
a temporary mapped source share allows ingestion; a real refetch then reconciles
that source share away. Only generated document UUIDs are substituted in expected
responses. The fixture itself is unchanged.

| Acceptance | Evidence |
|---|---|
| Newly ingested content is searchable | Catalog POST runs real chunking, authenticated `/embed`, chunk persistence and `/search`; all shared fixture exchanges match. |
| Citations resolve | IDs, titles, URLs, source IDs, ordinals, Unicode code-point slices and SHA-256 hashes match the stored document/chunks, including emoji, shortened content and renamed titles. Person citations resolve through the authorized catalog GET. |
| Idempotency and replacement | Repeated ingest/index returns the same results without new embeddings; ingest, refetch and refresh-due replace content; shortening removes tail chunks and obsolete keyword results. |
| Retirement and disappearance | Retirement hides both contexts and clears chunks; reactivation rebuilds. Recognized connectors `404 file not found for this url` removes text/chunks. Other 404s, 403s and 503s retain searchable content; a later successful fetch recovers. |
| Real authorization | Two people, primary/verified alternate email lookup, direct shares, source-team grants, manual grants, revocation, expiry, membership changes, channel union/reconfiguration and failed group sync use the real clients and grant predicates. |
| Filter before ranking | More than each candidate limit's worth of better-ranked, ingested unauthorized chunks cannot displace the allowed result in vector, keyword or hybrid search. |
| Fail closed | Shared exchanges cover missing context/scopes, unconfigured channels and no matches. Person/channel directory failures stop before query embedding; embedding errors/timeouts produce safe errors without keyword fallback. |
| Recovery and races | A later embedding batch can fail after earlier success without committing partial chunks. Index/storage failures retain one complete version. Revocation during embedding or failed re-indexing still applies immediately; a newer content fetch wins over a delayed index writer. Retries recover without duplicates. |
| Postgres fault behavior | An injected INSERT failure after the real replacement DELETE rolls back the old set; `/docs/index` reports a safe failure and retries successfully. Real retrieval SQL failures return safe 503s for person/channel contexts and recover once the fault is removed. |

## Accepted freshness and failure behavior

The [API contract](API.md#authorized-retrieval-post-search) defines returned chunk
revisions and safe search errors. The
[source refresh schedule](DEPLOYMENT.md#source-access-refresh-schedule) defines
the reconciliation window; directory membership/channel scope is resolved anew
on each search. Defaults schedule source refresh after 24 hours and expire source
grants after 48 hours, bounded further by any upstream permission expiration.
The service has no internal scheduler. Operators must run the documented refresh
job. Upstream revocation is known only after a successful reconciliation or expiry;
there is no claim of instantaneous Google permission propagation.

A failed or partially resolved ACL retains the last complete snapshot and its
original expiry. A successful empty ACL removes source grants. Independent manual
grants remain until explicitly removed. The expiry test supplies an elapsed
deadline at the API's source-grant-expiry boundary, then lets both real adapters
filter it; it does not sleep or insert expired grant rows by hand.

Indexing is all-or-nothing per document, separately from the completed catalog,
content and ACL writes. If replacement embedding or storage fails, search may
serve the old complete set under **current** grants until retry. The old body hash
and offsets remain attached to those hits; the newest full content may already be
stored. Newly ingested docs with failed indexing have no searchable chunks.
Successful replacement removes all old chunks. Catalog retirement filters at query
time, and confirmed source disappearance removes content and chunks atomically.

Ingest reports fetch/index failures as warnings while preserving the catalog row.
Refetch reports fetch/ACL failure as 502; indexing failure keeps its successful
catalog response and logs the failure. Refresh-due counts `failed`/`index_failed`.
`POST /docs/index` reports per-document status with 200, including sanitized
failures, and is the retry path. See the existing
[index API](API.md#post-docsindex--re-index-stored-content) for pagination and scopes.

## Reproduce the checks

From this service directory, using the repository lockfile:

```bash
uv sync --frozen --extra dev
uv run pytest tests/test_ingestion_retrieval.py -q
uv run pytest --ignore=tests/test_postgres_adapter.py -q
uv run ruff check .
uv run ruff format --check .
```

For Postgres use a **disposable** pgvector-enabled database, as in
[README testing](../README.md#testing). The suite truncates catalog tables; migration
checks also perform downgrades and create/drop a test role/database. CI's
`documentation-system-test` job provisions `pgvector/pgvector:0.8.2-pg16` and runs
the complete suite with both opt-ins.

```bash
docker compose up -d postgres
export DATABASE_URL=postgresql+psycopg://docs:dev_password@localhost:5434/docs
uv run alembic upgrade head
RUN_PG_TESTS=1 uv run pytest tests/test_postgres_adapter.py::TestIngestionRetrieval -q
RUN_PG_TESTS=1 RUN_PG_MIGRATION_TESTS=1 uv run pytest -q
```

Expected: both adapters pass the lifecycle cases; SQL fault tests recover; the
complete service suite and Ruff checks pass. Without `RUN_PG_TESTS=1`, database
acceptance skips before connecting. No AWS, Google or live LLM calls are needed.
When other worktrees are installed in the environment, pin and verify the `src`,
`contracts` and `tests` import roots before collection, as required by
[AGENTS.md](../../../AGENTS.md#gotchas-that-will-cost-you-an-hour).

## Controlled staging/provider acceptance — pending

Before the October 23 go/no-go, attach the successful required CI run and staging
deployment identifier, then
perform controlled acceptance with synthetic source documents and test identities:

1. Ingest one personal share and two separately group-shared files. Verify their
   real titles/source links and citations for the two test people and the configured
   channel union. Record document IDs/hashes, statuses and counts, not content or keys.
2. Repeat ingest; shorten one body and refetch. Verify no duplicate chunk identities,
   current citation hashes and removal of obsolete text. Retire/reactivate a record.
3. Revoke a personal/source group share, run reconciliation, and repeat person/channel
   search. Remove a membership or channel team and verify the next request's scope.
   Record the actual refresh/sync delay and the configured expiry window.
4. Confirm a source file is gone, verify removal, then exercise controlled transient
   fetch/embedding unavailability. Restore the dependency and drain `/docs/index`;
   verify complete replacement, current authorization and duplicate-free recovery.
5. Record operator/date, results, exceptions and the release decision. Link #278's
   bot/DM/channel acceptance separately; this service-level suite does not replace it.

Automated implementation/checks are due October 21. Controlled provider/staging
evidence must be recorded before the October 23 release decision; closing #175/#176
alone does not satisfy that gate.

## Changed-file rationale

All paths belong to the `services/documentation-system` ownership zone.

| File | Necessity |
|---|---|
| `tests/ingestion_retrieval_cases.py` | Shared real API lifecycle acceptance with deterministic external HTTP fixtures; prevents seeded-only retrieval coverage from hiding integration regressions. |
| `tests/test_ingestion_retrieval.py` | Runs those cases in memory by default, without Docker or live providers. |
| `tests/test_postgres_adapter.py` | Runs identical cases on real Postgres plus transaction rollback and actual retrieval SQL error checks; existing CI already selects this file. |
| `docs/API.md` | Makes retained-version citations, current authorization and reconciliation freshness explicit for consumers. |
| `docs/ARCHITECTURE.md` | Documents how lifecycle verification uses existing boundaries and distinguishes remaining release gates. |
| `docs/DEPLOYMENT.md` | Links reproducible integration/recovery commands and the controlled staging acceptance gate. |
| `README.md` | Exposes the integration test entry points and updates the deferred-work status. |
| `docs/INGESTION-RETRIEVAL-AUDIT.md` | Records acceptance coverage, file rationale, commands, evidence and outstanding live release acceptance. |
