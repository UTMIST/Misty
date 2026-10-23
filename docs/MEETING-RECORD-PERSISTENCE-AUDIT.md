# Meeting record persistence — issue 222 audit

Implements the post-meeting path specified by [issue 212](https://github.com/UTMIST/Misty/issues/212)
and [issue 222](https://github.com/UTMIST/Misty/issues/222).
This deliberately spans documentation-system, connectors, meeting, the bot,
and deployment documentation: the stop wire contract and the authenticated
writer boundary must ship together. Verification is local, with no deployment
or real Google/AWS/LLM request.

## Validation and operational limits

The complete catalog suite runs against an isolated Postgres cluster, including
adapter parity and upgrade/downgrade/upgrade migration checks. Google, catalog
transport and transcription/LLM boundaries use offline fakes. The meeting and
connector suites cover the new hand-off and writer paths. Each affected service
uses an isolated Python 3.12 environment with only its declared development
dependencies; this prevents another workspace member from masking a missing
test plugin. Meeting tests use the existing `asyncio.run` pattern rather than
requiring the undeclared `pytest-asyncio` plugin. Python test runners pin and
verify src/contracts/tests module paths to this checkout.

Bot verification uses the pinned npm lockfile, ESLint and Prettier. Mounted-drive
startup subprocesses can exceed the existing test timeout in this environment;
the full suite runs from this checkout with `node_modules` linked to dependencies
installed on the Linux filesystem. Install those dependencies in the Linux
directory: running `npm ci` in the mounted checkout replaces the link.

Operational prerequisites and accepted crash windows remain in the
[rollout runbook](RAILWAY-DEPLOYMENT.md#meeting-record-persistence) and
[durable record design](MEETING-RECORDING.md#durable-records-design-212).
Real Workspace OAuth/root access still needs a consented staging verification.
The generic public catalog purge API and Doc-link delivery remain their identified
follow-ups; meeting removal already purges internally. The Discord PDF is retained
as the compatibility output and recovery fallback.

## File necessity

The bot surface gates audio and finalizes saved recordings on moves; its lifecycle
tests verify that destination speech is excluded and PDF-only moves still work.
The Discord adapter's move comment now matches that lifecycle behavior.
Its integration tests cover saved finalization and preserve PDF-only move/auto-stop coverage.
The bot README and shared recording design describe the audience boundary.

The indexing fix adds migration
[`013_meeting_recovery.py`](../services/documentation-system/migrations/versions/013_meeting_recovery.py)
and its [round-trip test](../services/documentation-system/tests/test_migration_013.py)
for durable initial-index work. The schema, model, storage contract and both
adapters carry the retry flag; the coordinator and parity cases verify immediate
authorized retrieval and index recovery after restart. API wiring supplies the
existing embedder, with route tests using offline fakes. The migration 012 test
explicitly targets 011 when testing removal of its table. The fake writer supports
unique IDs so migration tests coexist with unrelated catalog rows. Agent guidance
and API/architecture/deployment/design docs describe the schema and retry behavior.

The removal fix adds migration
[`014_meeting_deletion_receipt.py`](../services/documentation-system/migrations/versions/014_meeting_deletion_receipt.py)
and its [round-trip test](../services/documentation-system/tests/test_migration_014.py)
for confirmed canonical-deletion receipts. The schema, model and shared storage
validation preserve these receipts; the coordinator and parity cases verify
recovery after partial cleanup or a failed final write. The fake writer rejects
unconfirmed deletion so retries exercise the production 404 contract. Agent and
API/architecture/deployment/design docs describe the new schema head and recovery
limits.

The identity-removal review fix adds
[`015_meeting_removal_identities.py`](../services/documentation-system/migrations/versions/015_meeting_removal_identities.py)
and its [round-trip test](../services/documentation-system/tests/test_migration_015.py)
for permanent fences covering canonical and discovered duplicate Google IDs.
The meeting model/schema, storage Protocol, shared validation and both adapters
support atomic identity-based catalog purge under the ingestion locks. The
coordinator purges ordinary, legacy and late catalog copies. Shared meeting cases
exercise real ingest, index and removal endpoints against both adapters;
PostgreSQL adds an actual uncommitted-insert race. The migration 014 test excludes
the intentional fence backfill/rescheduling from migration 015 when comparing its
historical round trip. Agent/API/design/architecture/deployment docs describe the
schema and recovery contract.

The removal-marker review fix retries version conflicts before acknowledging
removal. The coordinator and shared endpoint regression cover background
finalization winning the initial version check; API and architecture docs record
the acknowledgement contract.

The retry-reporting review fix adds outcome counts to the meeting contract,
coordinator and API. The runner uses them for exit and heartbeat status. Shared
meeting cases exercise real retry and runner endpoints against both adapters;
runner tests reject incomplete or inconsistent counts. API, architecture and
deployment docs describe the response and rollout order.

The historical-duplicate removal fix updates the storage Protocol and both
adapters so atomic removal purges include all per-file deletion identities,
including previously confirmed duplicates absent from owner search. Shared meeting
cases cover ordinary ingestion, authenticated search, prior reconciliation and
removal against both adapters. API, architecture and deployment docs explain the
expanded identity set and recovery contract. No schema change is needed.

| File | Why it changes |
|---|---|
| [Makefile](../Makefile) | Fix shell quoting so the Node version precheck runs instead of passing literal line continuations to JavaScript. |
| [services/connectors/contracts/meeting.py](../services/connectors/contracts/meeting.py) | Define strict writer requests, results, safe errors and the injected writer Protocol. |
| [services/connectors/src/api/routers/meeting.py](../services/connectors/src/api/routers/meeting.py) | Expose the authenticated meeting-write and meeting-fetch boundary. |
| [services/connectors/src/sources/meeting_google.py](../services/connectors/src/sources/meeting_google.py) | Implement OAuth native-Doc conversion, destination ACL checks, paging, sharing, owner fetch and deletion. |
| [services/connectors/tests/test_meeting_writer.py](../services/connectors/tests/test_meeting_writer.py) | Verify the affected behavior offline, including adapter parity, recovery, auth, consent or configuration as applicable. |
| [services/documentation-system/contracts/meeting.py](../services/documentation-system/contracts/meeting.py) | Define validated hand-off and structured metadata, durable state and the injected writer Protocol. |
| [services/documentation-system/migrations/versions/012_meeting_records.py](../services/documentation-system/migrations/versions/012_meeting_records.py) | Add the sequential schema change with a reversible downgrade. |
| [services/documentation-system/src/api/routers/meeting_records.py](../services/documentation-system/src/api/routers/meeting_records.py) | Expose scoped acceptance, metadata/status, retry, removal and delivery-state endpoints. |
| [services/documentation-system/src/fetch/meeting.py](../services/documentation-system/src/fetch/meeting.py) | Adapt authenticated HTTP connector operations to the writer Protocol without sensitive error bodies. |
| [services/documentation-system/src/meeting_fetch.py](../services/documentation-system/src/meeting_fetch.py) | Select owner reads for meeting Docs and preserve content on unconfirmed 404s. |
| [services/documentation-system/src/meeting_records.py](../services/documentation-system/src/meeting_records.py) | Orchestrate durable acceptance, HTML sections, guarded adoption, retries, late cleanup and removal. |
| [services/documentation-system/src/storage/meeting.py](../services/documentation-system/src/storage/meeting.py) | Share pure state validation and catalog-value construction across adapters. |
| [services/documentation-system/tests/meeting_record_cases.py](../services/documentation-system/tests/meeting_record_cases.py) | Verify the affected behavior offline, including adapter parity, recovery, auth, consent or configuration as applicable. |
| [services/documentation-system/tests/test_api_meeting_records.py](../services/documentation-system/tests/test_api_meeting_records.py) | Verify the affected behavior offline, including adapter parity, recovery, auth, consent or configuration as applicable. |
| [services/documentation-system/tests/test_meeting_records.py](../services/documentation-system/tests/test_meeting_records.py) | Verify the affected behavior offline, including adapter parity, recovery, auth, consent or configuration as applicable. |
| [services/documentation-system/tests/test_migration_012.py](../services/documentation-system/tests/test_migration_012.py) | Verify migration round trips with unrelated rows; seed through the current schema before historical downgrades. |
| [services/meeting/src/pipeline/record_handoff.py](../services/meeting/src/pipeline/record_handoff.py) | Hold finalized text until acceptance or bounded retry expiry, logging only IDs/status. |
| [services/meeting/tests/test_record_handoff.py](../services/meeting/tests/test_record_handoff.py) | Verify the affected behavior offline, including adapter parity, recovery, auth, consent or configuration as applicable. |
| [AGENTS.md](../AGENTS.md) | Update the schema head and agent guidance for canonical IDs and owner fetches. |
| [discord-bot/README.md](../discord-bot/README.md) | Document the new behavior/settings and link the service contracts and deployment guidance. |
| [discord-bot/src/adapters/discord/recording.js](../discord-bot/src/adapters/discord/recording.js) | Pass the save option from the authenticated Discord interaction. |
| [discord-bot/src/commands/record.js](../discord-bot/src/commands/record.js) | Register the optional save boolean on record start. |
| [discord-bot/src/meeting/meetingClient.js](../discord-bot/src/meeting/meetingClient.js) | Carry optional record context over the existing stop HTTP request. |
| [discord-bot/src/meeting/meetingSurface.js](../discord-bot/src/meeting/meetingSurface.js) | Capture speaker identities, send stop context, announce consent and delayed saves; preserve PDF delivery. |
| [discord-bot/test/meetingSurface.test.js](../discord-bot/test/meetingSurface.test.js) | Verify the affected behavior offline, including adapter parity, recovery, auth, consent or configuration as applicable. |
| [discord-bot/test/record-command.test.js](../discord-bot/test/record-command.test.js) | Verify the affected behavior offline, including adapter parity, recovery, auth, consent or configuration as applicable. |
| [docs/MEETING-RECORDING.md](../docs/MEETING-RECORDING.md) | Reconcile the merged design with the implemented hand-off and wire contract. |
| [docs/RAILWAY-DEPLOYMENT.md](../docs/RAILWAY-DEPLOYMENT.md) | Document coordinated credentials, scopes, migration, scheduler and rollout order. |
| [services/connectors/.env.example](../services/connectors/.env.example) | Provide documented local configuration examples without storing credentials. |
| [services/connectors/README.md](../services/connectors/README.md) | Document the new behavior/settings and link the service contracts and deployment guidance. |
| [services/connectors/docs/API.md](../services/connectors/docs/API.md) | Keep the service API, architecture or deployment guidance aligned with the implemented behavior. |
| [services/connectors/docs/ARCHITECTURE.md](../services/connectors/docs/ARCHITECTURE.md) | Keep the service API, architecture or deployment guidance aligned with the implemented behavior. |
| [services/connectors/docs/DEPLOYMENT.md](../services/connectors/docs/DEPLOYMENT.md) | Keep the service API, architecture or deployment guidance aligned with the implemented behavior. |
| [services/connectors/src/api/app.py](../services/connectors/src/api/app.py) | Register the new scoped meeting routes. |
| [services/connectors/src/api/deps.py](../services/connectors/src/api/deps.py) | Wire real writer/owner-fetch collaborators at the service composition point. |
| [services/connectors/src/config.py](../services/connectors/src/config.py) | Define optional settings, protect credentials with SecretStr and reject partial enabled configuration at boot. |
| [services/connectors/tests/test_config.py](../services/connectors/tests/test_config.py) | Verify the affected behavior offline, including adapter parity, recovery, auth, consent or configuration as applicable. |
| [services/documentation-system/.env.example](../services/documentation-system/.env.example) | Provide documented local configuration examples without storing credentials. |
| [services/documentation-system/README.md](../services/documentation-system/README.md) | Document the new behavior/settings and link the service contracts and deployment guidance. |
| [services/documentation-system/contracts/directory.py](../services/documentation-system/contracts/directory.py) | Specify complete audience resolution and Discord identity lookups. |
| [services/documentation-system/contracts/storage.py](../services/documentation-system/contracts/storage.py) | Specify replay, guarded transitions, atomic catalog commit and irreversible purge contracts. |
| [services/documentation-system/contracts/types.py](../services/documentation-system/contracts/types.py) | Represent nullable purge tombstones on catalog documents. |
| [services/documentation-system/docs/API.md](../services/documentation-system/docs/API.md) | Keep the service API, architecture or deployment guidance aligned with the implemented behavior. |
| [services/documentation-system/docs/ARCHITECTURE.md](../services/documentation-system/docs/ARCHITECTURE.md) | Keep the service API, architecture or deployment guidance aligned with the implemented behavior. |
| [services/documentation-system/docs/DEPLOYMENT.md](../services/documentation-system/docs/DEPLOYMENT.md) | Keep the service API, architecture or deployment guidance aligned with the implemented behavior. |
| [services/documentation-system/railway.source-refresh.json](../services/documentation-system/railway.source-refresh.json) | Enable the meeting phase on the existing staging hourly job only. |
| [services/documentation-system/src/api/app.py](../services/documentation-system/src/api/app.py) | Register the new scoped meeting routes. |
| [services/documentation-system/src/api/deps.py](../services/documentation-system/src/api/deps.py) | Wire real writer/owner-fetch collaborators at the service composition point. |
| [services/documentation-system/src/api/routers/docs.py](../services/documentation-system/src/api/routers/docs.py) | Use the same owner-read path for refetch/refresh and refuse restoration after purge. |
| [services/documentation-system/src/config.py](../services/documentation-system/src/config.py) | Define optional settings, protect credentials with SecretStr and reject partial enabled configuration at boot. |
| [services/documentation-system/src/directory/http_client.py](../services/documentation-system/src/directory/http_client.py) | Resolve identities and fail closed on incomplete channel-group coverage over authenticated HTTP. |
| [services/documentation-system/src/ingest.py](../services/documentation-system/src/ingest.py) | Use owner fetches for meeting identities and refuse purged imports. |
| [services/documentation-system/src/source_refresh.py](../services/documentation-system/src/source_refresh.py) | Add a bounded meeting phase to the existing authenticated, monitored runner. |
| [services/documentation-system/src/storage/in_memory.py](../services/documentation-system/src/storage/in_memory.py) | Implement the storage contract for offline tests and reject restoration after purge. |
| [services/documentation-system/src/storage/postgres.py](../services/documentation-system/src/storage/postgres.py) | Persist identical semantics with CAS predicates, row locks and an atomic catalog transaction. |
| [services/documentation-system/src/storage/schema.py](../services/documentation-system/src/storage/schema.py) | Declare the new meeting table and nullable document purge marker before the migration. |
| [services/documentation-system/tests/test_directory_client.py](../services/documentation-system/tests/test_directory_client.py) | Verify the affected behavior offline, including adapter parity, recovery, auth, consent or configuration as applicable. |
| [services/documentation-system/tests/test_migration_008.py](../services/documentation-system/tests/test_migration_008.py) | Verify migration round trips with unrelated rows; seed through the current schema before historical downgrades. |
| [services/documentation-system/tests/test_migration_010.py](../services/documentation-system/tests/test_migration_010.py) | Verify migration round trips with unrelated rows; seed through the current schema before historical downgrades. |
| [services/documentation-system/tests/test_migration_011.py](../services/documentation-system/tests/test_migration_011.py) | Verify migration round trips with unrelated rows; seed through the current schema before historical downgrades. |
| [services/documentation-system/tests/test_postgres_adapter.py](../services/documentation-system/tests/test_postgres_adapter.py) | Verify the affected behavior offline, including adapter parity, recovery, auth, consent or configuration as applicable. |
| [services/documentation-system/tests/test_source_refresh.py](../services/documentation-system/tests/test_source_refresh.py) | Verify the affected behavior offline, including adapter parity, recovery, auth, consent or configuration as applicable. |
| [services/meeting/.env.example](../services/meeting/.env.example) | Provide documented local configuration examples without storing credentials. |
| [services/meeting/README.md](../services/meeting/README.md) | Document the new behavior/settings and link the service contracts and deployment guidance. |
| [services/meeting/docs/API.md](../services/meeting/docs/API.md) | Keep the service API, architecture or deployment guidance aligned with the implemented behavior. |
| [services/meeting/docs/ARCHITECTURE.md](../services/meeting/docs/ARCHITECTURE.md) | Keep the service API, architecture or deployment guidance aligned with the implemented behavior. |
| [services/meeting/docs/DEPLOYMENT.md](../services/meeting/docs/DEPLOYMENT.md) | Keep the service API, architecture or deployment guidance aligned with the implemented behavior. |
| [services/meeting/src/api/app.py](../services/meeting/src/api/app.py) | Close held hand-off tasks during application shutdown. |
| [services/meeting/src/api/routers/meetings.py](../services/meeting/src/api/routers/meetings.py) | Validate record UUID/guild/context before finalization. |
| [services/meeting/src/api/wiring.py](../services/meeting/src/api/wiring.py) | Inject the configured HTTP hand-off at the existing composition boundary. |
| [services/meeting/src/config.py](../services/meeting/src/config.py) | Define optional settings, protect credentials with SecretStr and reject partial enabled configuration at boot. |
| [services/meeting/src/contracts.py](../services/meeting/src/contracts.py) | Add optional strict record context, participant metadata and hand-off result/Protocol. |
| [services/meeting/src/sessions.py](../services/meeting/src/sessions.py) | Finalize once under cancellation and hand off before cleanup without retaining audio. |
| [services/meeting/tests/test_config.py](../services/meeting/tests/test_config.py) | Verify the affected behavior offline, including adapter parity, recovery, auth, consent or configuration as applicable. |
| [docs/MEETING-RECORD-PERSISTENCE-AUDIT.md](MEETING-RECORD-PERSISTENCE-AUDIT.md) | Record scope, verification limits and the required justification for every changed file. |

## PR 306 recovery follow-up

The meeting contract, storage Protocol documentation, shared transition validator,
coordinator and schema now retain per-file Google deletion intent and confirmations.
Both adapters already persist every meeting field through the same versioned
transition, so neither needs a separate implementation change. Migration 016 adds
the JSON state with an empty default; its round-trip test verifies retained records.
Shared meeting cases cover delayed deletion after explicit retry, restart recovery,
and clearing reconciliation errors on both adapters. Earlier migration tests exclude
this newly introduced field when downgrading past its introduction.
API, architecture and deployment docs describe deletion progress, recovery and the rollback constraint;
the Railway runbook and AGENTS migration head now point at 016.
