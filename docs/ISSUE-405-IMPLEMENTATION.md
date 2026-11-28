# #405: Managed GitHub team mappings

Durable one-to-one mappings, explicit adoption, bounded provisioning and read-only recovery after uncertain creates. No membership or repository-permission writes.

## Review boundaries

This work deliberately spans connectors and team-tracking; root lock/config guidance and deployment docs must accompany it.
For implementation PRs, review connector contracts/provider behavior first, then
merge the directory contract/migration/adapters, and finally enable the consumer.
Split commits/PRs by zone around the HTTP contracts documented in the service
runbook. This working branch keeps the pieces together for end-to-end review;
local commits group the implementation by responsibility. It has not been pushed,
deployed, or used to modify GitHub accounts.

Both independent worktrees start at staging `52246a4` and allocate directory
revision 012 from head 011. Whichever merges second must rebase, renumber its
migration, and point `down_revision` at the new head before integration. Do not
merge two different revisions named 012.

## Verification

The affected Python services have offline provider/API tests and directory
adapter coverage, with imports explicitly pinned to this worktree. Migration
round trips use dedicated disposable PostgreSQL databases, including an existing
directory row to verify unrelated data survives downgrade/re-upgrade. No real
GitHub, Discord, AWS, or LLM calls are part of automated tests. Run the required
service pytest, ruff check, and ruff format checks before publishing. The linking
branch also requires the bot tests, ESLint, and Prettier checks. Live App and
callback/ingress configuration remain staging rollout tasks, not automated tests.

## Local verification results

- Full team-tracking suite passed against a disposable PostgreSQL 18 database;
  both migration round trips preserved an existing unrelated directory row.
- Full connectors suite passed with fake providers; final security/provider
  changes also have focused regression coverage.
- Both affected Python services passed the lockfile-pinned Ruff lint and format
  checks. Source package paths were pinned to this worktree before collection.

CI still needs to run its PostgreSQL 16/container checks. Live GitHub App,
callback ingress and credentials have not been provisioned or tested; enabling
those remains an explicit staging rollout task in the runbook.

## Changed files

| File | Why it is necessary |
|---|---|
| `AGENTS.md` | Update migration head and add the new GitHub lifecycle invariant for future agents. |
| `docs/ISSUE-405-IMPLEMENTATION.md` | Record file-level rationale, review boundaries, validation, and rollout requirements. |
| `docs/RAILWAY-DEPLOYMENT.md` | Keep the corresponding API, architecture, deployment, or contributor documentation accurate and link the focused runbook. |
| `services/connectors/.env.example` | Document/validate optional configuration and fail safely on incomplete enabled setup. |
| `services/connectors/README.md` | Expose configuration and user/admin entry points and distinguish implemented behavior from source retrieval. |
| `services/connectors/contracts/github_teams.py` | Define strict DTOs and injectable Protocol semantics before concrete implementations. |
| `services/connectors/docs/API.md` | Keep the corresponding API, architecture, deployment, or contributor documentation accurate and link the focused runbook. |
| `services/connectors/docs/ARCHITECTURE.md` | Keep the corresponding API, architecture, deployment, or contributor documentation accurate and link the focused runbook. |
| `services/connectors/docs/DEPLOYMENT.md` | Keep the corresponding API, architecture, deployment, or contributor documentation accurate and link the focused runbook. |
| `services/connectors/src/api/app.py` | Register the new scoped routes or authenticated command. |
| `services/connectors/src/api/deps.py` | Wire concrete providers and the optional feature at the existing centralized composition point. |
| `services/connectors/src/api/routers/github_teams.py` | Expose the scoped service contract with domain-to-HTTP error mapping and trusted actor handling. |
| `services/connectors/src/config.py` | Document/validate optional configuration and fail safely on incomplete enabled setup. |
| `services/connectors/src/sources/github_teams.py` | Keep bounded vendor/HTTP calls and static provider errors behind injected boundaries. |
| `services/connectors/tests/test_github_teams.py` | Verify lifecycle, authorization, provider failures, or shared adapter behavior using offline fakes/disposable databases. |
| `services/team-tracking/.env.example` | Document/validate optional configuration and fail safely on incomplete enabled setup. |
| `services/team-tracking/README.md` | Expose configuration and user/admin entry points and distinguish implemented behavior from source retrieval. |
| `services/team-tracking/contracts/github.py` | Define strict DTOs and injectable Protocol semantics before concrete implementations. |
| `services/team-tracking/contracts/storage.py` | Define strict DTOs and injectable Protocol semantics before concrete implementations. |
| `services/team-tracking/docs/API.md` | Keep the corresponding API, architecture, deployment, or contributor documentation accurate and link the focused runbook. |
| `services/team-tracking/docs/ARCHITECTURE.md` | Keep the corresponding API, architecture, deployment, or contributor documentation accurate and link the focused runbook. |
| `services/team-tracking/docs/DEPLOYMENT.md` | Keep the corresponding API, architecture, deployment, or contributor documentation accurate and link the focused runbook. |
| `services/team-tracking/docs/GITHUB-TEAMS.md` | Keep the corresponding API, architecture, deployment, or contributor documentation accurate and link the focused runbook. |
| `services/team-tracking/migrations/versions/012_github_team_mappings.py` | Create the new directory-owned identity table with a reversible migration. |
| `services/team-tracking/pyproject.toml` | Declare the new runtime dependency rather than relying on a development or transitive install. |
| `services/team-tracking/src/api/app.py` | Register the new scoped routes or authenticated command. |
| `services/team-tracking/src/api/deps.py` | Wire concrete providers and the optional feature at the existing centralized composition point. |
| `services/team-tracking/src/api/routers/github_teams.py` | Expose the scoped service contract with domain-to-HTTP error mapping and trusted actor handling. |
| `services/team-tracking/src/config.py` | Document/validate optional configuration and fail safely on incomplete enabled setup. |
| `services/team-tracking/src/providers/github_teams.py` | Keep bounded vendor/HTTP calls and static provider errors behind injected boundaries. |
| `services/team-tracking/src/storage/in_memory.py` | Implement identical identity/retirement semantics in both directory adapters. |
| `services/team-tracking/src/storage/postgres.py` | Implement identical identity/retirement semantics in both directory adapters. |
| `services/team-tracking/src/storage/schema.py` | Declare the directory table, audit fields, and identity uniqueness constraints as the schema source of truth. |
| `services/team-tracking/tests/github_mapping_contract.py` | Verify lifecycle, authorization, provider failures, or shared adapter behavior using offline fakes/disposable databases. |
| `services/team-tracking/tests/test_github_mappings.py` | Verify lifecycle, authorization, provider failures, or shared adapter behavior using offline fakes/disposable databases. |
| `services/team-tracking/tests/test_postgres_adapter.py` | Verify lifecycle, authorization, provider failures, or shared adapter behavior using offline fakes/disposable databases. |
| `uv.lock` | Lock the production HTTP/encryption dependency explicitly required by this issue. |

## Review recovery fixes

The existing contract, HTTP client/router, provider and adapter files now preserve
explicit no-write retryability end to end. Both adapters retain audited retryable
reservations with version-checked release and atomic reclaim. Schema and the
unreleased migration 012 accept the new state. Connector inspection distinguishes
auxiliary lookup failures from missing numeric team IDs. The shared adapter
contract, mapping API tests, connector provider tests and new
`services/team-tracking/tests/test_github_teams_client.py` cover definitive,
ambiguous, stale-attempt and auxiliary-404 behavior. The focused runbook,
connector API reference and agent invariant document these recovery semantics.
