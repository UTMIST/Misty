# #406: Verified GitHub account linking

OAuth/PKCE with private original-session completion, encrypted connector vault, verified directory IDs, and private bot/web linking controls. No private source retrieval.

## Review boundaries

This work deliberately spans connectors and team-tracking, plus the bot; root lock/config guidance and deployment docs must accompany it.
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
- Full bot suite passed serially, with ESLint and Prettier checks. Dependencies
  were copied unchanged to a temporary Linux filesystem because Windows-drive
  imports exceeded existing process-startup test timeouts.

CI still needs to run its PostgreSQL 16/container checks. Live GitHub App,
callback ingress and credentials have not been provisioned or tested; enabling
those remains an explicit staging rollout task in the runbook.

## Changed files

| File | Why it is necessary |
|---|---|
| `AGENTS.md` | Update migration head and add the new GitHub lifecycle invariant for future agents. |
| `discord-bot/.env.example` | Document/validate optional configuration and fail safely on incomplete enabled setup. |
| `discord-bot/README.md` | Expose configuration and user/admin entry points and distinguish implemented behavior from source retrieval. |
| `discord-bot/docs/CONTRIBUTING.md` | Keep the corresponding API, architecture, deployment, or contributor documentation accurate and link the focused runbook. |
| `discord-bot/src/clients/githubLinkClient.js` | Provide private authenticated linking/status UX in Discord and the existing web command surface. |
| `discord-bot/src/commands/github.js` | Provide private authenticated linking/status UX in Discord and the existing web command surface. |
| `discord-bot/src/commands/index.js` | Register the new scoped routes or authenticated command. |
| `discord-bot/src/config.js` | Document/validate optional configuration and fail safely on incomplete enabled setup. |
| `discord-bot/src/context.js` | Wire concrete providers and the optional feature at the existing centralized composition point. |
| `discord-bot/test/githubLink.test.js` | Verify lifecycle, authorization, provider failures, or shared adapter behavior using offline fakes/disposable databases. |
| `docs/ARCHITECTURE.md` | Keep the corresponding API, architecture, deployment, or contributor documentation accurate and link the focused runbook. |
| `docs/ISSUE-406-IMPLEMENTATION.md` | Record file-level rationale, review boundaries, validation, and rollout requirements. |
| `docs/RAILWAY-DEPLOYMENT.md` | Keep the corresponding API, architecture, deployment, or contributor documentation accurate and link the focused runbook. |
| `services/connectors/.env.example` | Document/validate optional configuration and fail safely on incomplete enabled setup. |
| `services/connectors/README.md` | Expose configuration and user/admin entry points and distinguish implemented behavior from source retrieval. |
| `services/connectors/contracts/github_oauth.py` | Define strict DTOs and injectable Protocol semantics before concrete implementations. |
| `services/connectors/docs/API.md` | Keep the corresponding API, architecture, deployment, or contributor documentation accurate and link the focused runbook. |
| `services/connectors/docs/ARCHITECTURE.md` | Keep the corresponding API, architecture, deployment, or contributor documentation accurate and link the focused runbook. |
| `services/connectors/docs/CONTRIBUTING.md` | Keep the corresponding API, architecture, deployment, or contributor documentation accurate and link the focused runbook. |
| `services/connectors/docs/DEPLOYMENT.md` | Keep the corresponding API, architecture, deployment, or contributor documentation accurate and link the focused runbook. |
| `services/connectors/docs/GITHUB-LINKING.md` | Keep the corresponding API, architecture, deployment, or contributor documentation accurate and link the focused runbook. |
| `services/connectors/pyproject.toml` | Declare the new runtime dependency rather than relying on a development or transitive install. |
| `services/connectors/src/api/app.py` | Register the new scoped routes or authenticated command. |
| `services/connectors/src/api/deps.py` | Wire concrete providers and the optional feature at the existing centralized composition point. |
| `services/connectors/src/api/github_logging.py` | Keep the corresponding API, architecture, deployment, or contributor documentation accurate and link the focused runbook. |
| `services/connectors/src/api/routers/github_oauth.py` | Expose the scoped service contract with domain-to-HTTP error mapping and trusted actor handling. |
| `services/connectors/src/config.py` | Document/validate optional configuration and fail safely on incomplete enabled setup. |
| `services/connectors/src/github_linking.py` | Coordinate single-use OAuth, original-session completion, token refresh, unlink, and revocation. |
| `services/connectors/src/sources/github_directory.py` | Keep bounded vendor/HTTP calls and static provider errors behind injected boundaries. |
| `services/connectors/src/sources/github_oauth.py` | Keep bounded vendor/HTTP calls and static provider errors behind injected boundaries. |
| `services/connectors/src/sources/github_vault.py` | Own encrypted durable OAuth state/tokens with serialized transactions and explicit connection cleanup. |
| `services/connectors/tests/test_github_linking.py` | Verify lifecycle, authorization, provider failures, or shared adapter behavior using offline fakes/disposable databases. |
| `services/connectors/tests/test_github_oauth_api.py` | Verify lifecycle, authorization, provider failures, or shared adapter behavior using offline fakes/disposable databases. |
| `services/connectors/tests/test_github_oauth_provider.py` | Verify lifecycle, authorization, provider failures, or shared adapter behavior using offline fakes/disposable databases. |
| `services/team-tracking/README.md` | Expose configuration and user/admin entry points and distinguish implemented behavior from source retrieval. |
| `services/team-tracking/contracts/github_identity.py` | Define strict DTOs and injectable Protocol semantics before concrete implementations. |
| `services/team-tracking/contracts/storage.py` | Define strict DTOs and injectable Protocol semantics before concrete implementations. |
| `services/team-tracking/docs/API.md` | Keep the corresponding API, architecture, deployment, or contributor documentation accurate and link the focused runbook. |
| `services/team-tracking/docs/ARCHITECTURE.md` | Keep the corresponding API, architecture, deployment, or contributor documentation accurate and link the focused runbook. |
| `services/team-tracking/docs/DEPLOYMENT.md` | Keep the corresponding API, architecture, deployment, or contributor documentation accurate and link the focused runbook. |
| `services/team-tracking/migrations/versions/012_verified_github_identities.py` | Create the new directory-owned identity table with a reversible migration. |
| `services/team-tracking/src/api/app.py` | Register the new scoped routes or authenticated command. |
| `services/team-tracking/src/api/routers/github_identity.py` | Expose the scoped service contract with domain-to-HTTP error mapping and trusted actor handling. |
| `services/team-tracking/src/storage/in_memory.py` | Implement identical identity/retirement semantics in both directory adapters. |
| `services/team-tracking/src/storage/postgres.py` | Implement identical identity/retirement semantics in both directory adapters. |
| `services/team-tracking/src/storage/schema.py` | Declare the directory table, audit fields, and identity uniqueness constraints as the schema source of truth. |
| `services/team-tracking/tests/github_identity_contract.py` | Verify lifecycle, authorization, provider failures, or shared adapter behavior using offline fakes/disposable databases. |
| `services/team-tracking/tests/test_github_identity.py` | Verify lifecycle, authorization, provider failures, or shared adapter behavior using offline fakes/disposable databases. |
| `services/team-tracking/tests/test_postgres_adapter.py` | Verify lifecycle, authorization, provider failures, or shared adapter behavior using offline fakes/disposable databases. |
| `uv.lock` | Lock the production HTTP/encryption dependency explicitly required by this issue. |

## Review recovery fixes

`services/connectors/src/github_linking.py` keeps durable pending directory
cleanup in terminal vault records and retries it under the vault lock from
status/reconnect, including after restart. Cleanup cannot erase a subsequently
confirmed link. `services/connectors/tests/test_github_linking.py` covers outages
for unlink, refresh failure and revocation, recovery by status/reconnect, account
transfer and delayed cleanup. `services/connectors/docs/GITHUB-LINKING.md`
records these semantics and corrects the connector directory key to require
`identifiers:read` for its Discord reverse lookup.
