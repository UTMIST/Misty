# Documentation audit corrections — 2026-10-23

Baseline: `dafb494b8b7bbb8f3d1a52ba329aa067d1ab6459` on `staging`. This change reconciles the repository-wide documentation audit with the implementation at that revision. The audit covered all tracked Markdown files, environment examples, and operational guidance; historical release/acceptance records and synthetic fixtures were distinguished from current promises.

**Documentation only.** Changes are Markdown and explanatory comments in environment examples, workflow YAML and the Makefile help text. Application code, tests, migrations, dependency/runtime declarations, executable commands and active configuration values are unchanged.

The original area audit contained 105 findings with cross-area overlap, plus one duplicated LLM API section. IDs below preserve that audit trail; they are not a count of unique defects. This ledger is an implementation snapshot, not a new canonical source for API or configuration behavior. Follow the linked maintained guides.

## Why each file changed

| File | Reason |
| --- | --- |
| [.github/workflows/blocked-ready-automation.yml](../.github/workflows/blocked-ready-automation.yml) | Correct comments about empty blocker lists and assigned dependents; workflow script unchanged. |
| [.github/workflows/discord-preview.yml](../.github/workflows/discord-preview.yml) | Correct the timeout-budget comment; timeout and job steps unchanged. |
| [.github/workflows/pr-zone-check.yml](../.github/workflows/pr-zone-check.yml) | Correct enforcement instructions in comments; exit behavior unchanged. |
| [AGENTS.md](../AGENTS.md) | Correct the shared testing contract, CI inventory and LLM/connectors summaries. |
| [Makefile](../Makefile) | Correct the test-full help comment; prerequisites and recipes unchanged. |
| [README.md](../README.md) | Align public command, service, dependency, auth, testing and local setup summaries. |
| [discord-bot/.env.example](../discord-bot/.env.example) | Correct the directory-key scope documentation; comments only. |
| [discord-bot/README.md](../discord-bot/README.md) | Correct runtime/mode isolation, linking, registration cwd, channel append, recording config ownership, seed semantics, and implemented feature status. |
| [discord-bot/docs/CONTRIBUTING.md](../discord-bot/docs/CONTRIBUTING.md) | Link the runtime mismatch and correct command authorization examples. |
| [discord-bot/docs/helper-limits.md](../discord-bot/docs/helper-limits.md) | Match allowance accounting to the implemented tool loop. |
| [docs/ACCESS.md](../docs/ACCESS.md) | Cover independent catalog/gateway key revocation during offboarding. |
| [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) | Replace stale retrieval, dependency, auth, schema and fetch descriptions. |
| [docs/CODE-OWNERSHIP.md](../docs/CODE-OWNERSHIP.md) | Correct enforcement instructions, failing checks and zone inventory prose. |
| [docs/DEPLOYMENT-HISTORY.md](../docs/DEPLOYMENT-HISTORY.md) | Correct undated operational guidance and clearly label old deployment snapshots; preserve release entries. |
| [docs/DEVELOPMENT.md](../docs/DEVELOPMENT.md) | Correct prerequisites, service/port inventory, manual verification and database-test setup. |
| [docs/MEETING-RECORDING.md](../docs/MEETING-RECORDING.md) | Correct failed-record filter, audio protocol, move/cutoff behavior, disconnect recovery and cleanup uncertainty. |
| [docs/ONBOARDING.md](../docs/ONBOARDING.md) | State current ready-label limits so contributors check assignees. |
| [docs/RAILWAY-DEPLOYMENT.md](../docs/RAILWAY-DEPLOYMENT.md) | Correct production tier, key scopes, provider variables, verification commands, retention and migration guidance. |
| [docs/READINESS-MONITORING.md](../docs/READINESS-MONITORING.md) | Remove the nonexistent per-service failure-log fallback. |
| [docs/pr-previews.md](../docs/pr-previews.md) | Require all four isolated databases and explain the current Actions timeout limit. |
| [packages/auth/README.md](../packages/auth/README.md) | Correct store wiring, required secret unwrapping, and opt-in production scope enforcement. |
| [packages/auth/docs/CONTRIBUTING.md](../packages/auth/docs/CONTRIBUTING.md) | Include gateway throughout shared-auth changes and testing; document literal assertion scope and service-specific dev guard. |
| [services/connectors/README.md](../services/connectors/README.md) | Refresh fetch/discovery/writer responsibilities and scopes, auth boundaries, media terminology, fetch omissions, and matching overview/layout/status wording. |
| [services/connectors/docs/API.md](../services/connectors/docs/API.md) | Correct public schema/docs auth, writer inventory/cleanup semantics, wire response example, media path, and error/log diagnostics. |
| [services/connectors/docs/ARCHITECTURE.md](../services/connectors/docs/ARCHITECTURE.md) | Distinguish writer/read errors and current responsibilities, clarify status normalization, and name the uploaded-media path. |
| [services/connectors/docs/CONTRIBUTING.md](../services/connectors/docs/CONTRIBUTING.md) | Prevent read-helper guidance/checklist from overriding the writer's uncertainty-preserving error contract. |
| [services/connectors/docs/DEPLOYMENT.md](../services/connectors/docs/DEPLOYMENT.md) | Correct restart/rollback external-state assumptions and Google error troubleshooting; link catalog reconciliation. |
| [services/documentation-system/.env.example](../services/documentation-system/.env.example) | Explain the staging meeting-refresh key requirements in comments only (DOCSYS-003). |
| [services/documentation-system/README.md](../services/documentation-system/README.md) | Align refresh mode/scopes, public auth exceptions, assertion scopes, fetch-enabled/auth-required behavior and destructive test opt-ins (DOCSYS-001/003/007; review follow-through). |
| [services/documentation-system/docs/API.md](../services/documentation-system/docs/API.md) | Correct direct person-context scope rules, source scope, dedup fetching/indexing and GitHub connector path (DOCSYS-001/002/006). |
| [services/documentation-system/docs/ARCHITECTURE.md](../services/documentation-system/docs/ARCHITECTURE.md) | Correct person-context gate, fetch semantics/full content and migration attribution (DOCSYS-001/005/007/009). |
| [services/documentation-system/docs/CONTRIBUTING.md](../services/documentation-system/docs/CONTRIBUTING.md) | Correct fetcher/migration recipes and destructive test opt-ins (DOCSYS-004/009; review follow-through); remove an existing stale test-count comment. |
| [services/documentation-system/docs/DEPLOYMENT.md](../services/documentation-system/docs/DEPLOYMENT.md) | Correct production boot settings, repair/backfill guarantees, GitHub empty case, refresh setup and rotation (DOCSYS-002/003/006/008/010). |
| [services/gateway/README.md](../services/gateway/README.md) | Remove the unpinned issuance shortcut in favor of the existing authoritative procedure. |
| [services/llm/README.md](../services/llm/README.md) | Correct provider-specific token minimum and auth assertions, including the matching quick-start occurrence. |
| [services/llm/docs/API.md](../services/llm/docs/API.md) | Correct auth-surface overview and remove duplicate validation prose. |
| [services/meeting/README.md](../services/meeting/README.md) | Correct disconnect/drain behavior, retention/current persistence summary, timestamp origin, control-frame limitation, boot-check limits, prompt ownership, and link minutes limits. |
| [services/meeting/docs/API.md](../services/meeting/docs/API.md) | Publish accurate wire timebase/control handling, drain/recovery conditions, summarization losses, and post-stop retention. |
| [services/meeting/docs/ARCHITECTURE.md](../services/meeting/docs/ARCHITECTURE.md) | Align repeated drain/recovery, malformed-input, asset-I/O/test-scope, and prompt-ownership claims with the same corrections. |
| [services/meeting/docs/CONTRIBUTING.md](../services/meeting/docs/CONTRIBUTING.md) | Correct asset-read/test scope, preserve defensive control validation expectations while naming the current gap, and qualify the drain fallback. |
| [services/meeting/docs/DEPLOYMENT.md](../services/meeting/docs/DEPLOYMENT.md) | Correct boot/recovery guarantees, outage degradation, temporary text retention, restart/rollback effects, and filesystem-test scope. |
| [services/team-tracking/.env.example](../services/team-tracking/.env.example) | Correct the boot-check/spoof-key environment matrix in explanatory comments only (TT-DOC-006). |
| [services/team-tracking/README.md](../services/team-tracking/README.md) | Correct current integration, scope, public auth exceptions, migration, route-inventory and membership-filter summaries (TT-DOC-001/003/005/006/008/011). |
| [services/team-tracking/docs/API.md](../services/team-tracking/docs/API.md) | Correct privilege fields/scopes, membership dates/filter/error contracts, provider-inactivity limit, retirement guarantee and introspection status (TT-DOC-002/003/004/005/007/009/010). |
| [services/team-tracking/docs/ARCHITECTURE.md](../services/team-tracking/docs/ARCHITECTURE.md) | Correct overlap, integration/retirement, people column and migration descriptions (TT-DOC-001/002/005/008/009). |
| [services/team-tracking/docs/CONTRIBUTING.md](../services/team-tracking/docs/CONTRIBUTING.md) | Make the migration-authoring recipe use inspected, unused revisions (TT-DOC-001). |
| [services/team-tracking/docs/DEPLOYMENT.md](../services/team-tracking/docs/DEPLOYMENT.md) | Add the missing elevated-person-write scope (TT-DOC-005). |
| [services/verification/README.md](../services/verification/README.md) | Correct fake-email setup, public auth exceptions, audit attribution, and provider-error guarantees. |
| [services/verification/docs/API.md](../services/verification/docs/API.md) | Document the current normalized/unhandled mail-error boundary once for linked references. |
| [services/verification/docs/ARCHITECTURE.md](../services/verification/docs/ARCHITECTURE.md) | Correct retained-data exposure, provider failures, fifth-attempt transition, and fake-backend rationale. |
| [services/verification/docs/CONTRIBUTING.md](../services/verification/docs/CONTRIBUTING.md) | Replace nonexistent code-log instructions with the retained test sender pattern and real-mail prerequisite; avoid gateway's database port in the remapping example. |
| [services/verification/docs/DEPLOYMENT.md](../services/verification/docs/DEPLOYMENT.md) | Correct retention, credential-presence limits, fake-backend rationale, and trimmed subject troubleshooting. |
| [docs/DOCUMENTATION-AUDIT.md](DOCUMENTATION-AUDIT.md) | Record scope, finding coverage, per-file justification and unchanged implementation limitations for this multi-zone documentation repair. |

## Finding coverage

Each row identifies an audited stale claim and the maintained documents corrected. Cross-area duplicates can point to the same correction. Scope and examples were checked against source; documenting a limitation does not fix that implementation.

| Audit ID | Audited discrepancy | Corrected reference(s) |
| --- | --- | --- |
| TT-DOC-001 | Migration walkthrough points at an already-used revision | [services/team-tracking/docs/CONTRIBUTING.md](../services/team-tracking/docs/CONTRIBUTING.md), [services/team-tracking/README.md](../services/team-tracking/README.md), [services/team-tracking/docs/ARCHITECTURE.md](../services/team-tracking/docs/ARCHITECTURE.md) |
| TT-DOC-002 | Architecture explicitly permits overlapping memberships | [services/team-tracking/docs/ARCHITECTURE.md](../services/team-tracking/docs/ARCHITECTURE.md), [services/team-tracking/docs/API.md](../services/team-tracking/docs/API.md) |
| TT-DOC-003 | `active_only` filter and current-roster recipes are inaccurate | [services/team-tracking/docs/API.md](../services/team-tracking/docs/API.md) |
| TT-DOC-004 | End date is described as inclusive | [services/team-tracking/docs/API.md](../services/team-tracking/docs/API.md) |
| TT-DOC-005 | API and operational references omit privileged person writes | [services/team-tracking/docs/API.md](../services/team-tracking/docs/API.md), [services/team-tracking/docs/DEPLOYMENT.md](../services/team-tracking/docs/DEPLOYMENT.md), [services/team-tracking/docs/ARCHITECTURE.md](../services/team-tracking/docs/ARCHITECTURE.md), [README.md](../README.md) |
| TT-DOC-006 | Staging safeguards are described inconsistently | [services/team-tracking/.env.example](../services/team-tracking/.env.example), [services/team-tracking/README.md](../services/team-tracking/README.md) |
| TT-DOC-007 | Inactive-provider rejection is promised but not implemented | [services/team-tracking/docs/API.md](../services/team-tracking/docs/API.md) |
| TT-DOC-008 | “Nothing runs inside” hides Google side effects | [services/team-tracking/README.md](../services/team-tracking/README.md), [services/team-tracking/docs/ARCHITECTURE.md](../services/team-tracking/docs/ARCHITECTURE.md) |
| TT-DOC-009 | Retiring a team does not empty all Google memberships | [services/team-tracking/docs/ARCHITECTURE.md](../services/team-tracking/docs/ARCHITECTURE.md), [services/team-tracking/docs/API.md](../services/team-tracking/docs/API.md) |
| TT-DOC-010 | Key introspection can return 403 | [services/team-tracking/docs/API.md](../services/team-tracking/docs/API.md) |
| TT-DOC-011 | README route/scope inventory has drifted | [services/team-tracking/README.md](../services/team-tracking/README.md) |
| DOCSYS-001 | Direct-document authorization tables overstate scoped-key permissions | [services/documentation-system/docs/API.md](../services/documentation-system/docs/API.md), [services/documentation-system/docs/ARCHITECTURE.md](../services/documentation-system/docs/ARCHITECTURE.md) |
| DOCSYS-002 | Idempotent ingestion is incorrectly promised to be fetch-free | [services/documentation-system/docs/API.md](../services/documentation-system/docs/API.md), [services/documentation-system/docs/DEPLOYMENT.md](../services/documentation-system/docs/DEPLOYMENT.md) |
| DOCSYS-003 | Primary refresh setup provisions an under-scoped key for the checked-in staging command | [services/documentation-system/docs/DEPLOYMENT.md](../services/documentation-system/docs/DEPLOYMENT.md), [services/documentation-system/README.md](../services/documentation-system/README.md), [services/documentation-system/.env.example](../services/documentation-system/.env.example) |
| DOCSYS-004 | Migration-authoring instructions reuse an existing revision and obsolete head | [services/documentation-system/docs/CONTRIBUTING.md](../services/documentation-system/docs/CONTRIBUTING.md) |
| DOCSYS-005 | Architecture attributes per-model chunk keys to migration 018 instead of 017 | [services/documentation-system/docs/ARCHITECTURE.md](../services/documentation-system/docs/ARCHITECTURE.md) |
| DOCSYS-006 | Current GitHub instructions retain the old local, title-only fetch behavior | [services/documentation-system/docs/API.md](../services/documentation-system/docs/API.md), [services/documentation-system/docs/DEPLOYMENT.md](../services/documentation-system/docs/DEPLOYMENT.md) |
| DOCSYS-007 | Architecture says auth-required sources skip fetching | [services/documentation-system/docs/ARCHITECTURE.md](../services/documentation-system/docs/ARCHITECTURE.md), [services/documentation-system/README.md](../services/documentation-system/README.md) |
| DOCSYS-008 | Production config table omits the boot-check switch and incorrectly recommends unsetting API_KEY | [services/documentation-system/docs/DEPLOYMENT.md](../services/documentation-system/docs/DEPLOYMENT.md) |
| DOCSYS-009 | Fetcher implementation recipe omits full content required for retrieval | [services/documentation-system/docs/CONTRIBUTING.md](../services/documentation-system/docs/CONTRIBUTING.md) |
| DOCSYS-010 | Backfill exit-zero guarantee ignores intentionally skipped and dry-run documents | [services/documentation-system/docs/DEPLOYMENT.md](../services/documentation-system/docs/DEPLOYMENT.md) |
| LLM-001 | README permits token budgets rejected by the default provider | [services/llm/README.md](../services/llm/README.md), [services/llm/docs/API.md](../services/llm/docs/API.md) |
| LLM-002 | Authentication wording overlooks public schema/documentation routes | [services/llm/README.md](../services/llm/README.md), [README.md](../README.md), [services/llm/docs/API.md](../services/llm/docs/API.md) |
| VF-001 | Local setup directs readers to nonexistent plaintext-code logs | [services/verification/docs/CONTRIBUTING.md](../services/verification/docs/CONTRIBUTING.md), [docs/DEVELOPMENT.md](../docs/DEVELOPMENT.md) |
| VF-002 | README incorrectly promises `X-Actor` audit attribution | [services/verification/README.md](../services/verification/README.md), [services/verification/docs/API.md](../services/verification/docs/API.md) |
| VF-003 | Universal clean-502 provider-failure guarantee exceeds the implementation | [services/verification/README.md](../services/verification/README.md), [services/verification/docs/ARCHITECTURE.md](../services/verification/docs/ARCHITECTURE.md), [services/verification/docs/API.md](../services/verification/docs/API.md) |
| VF-004 | Retention and compromise descriptions exclude unverified and old requests | [services/verification/docs/DEPLOYMENT.md](../services/verification/docs/DEPLOYMENT.md), [services/verification/docs/ARCHITECTURE.md](../services/verification/docs/ARCHITECTURE.md) |
| VF-005 | Architecture state table omits the fifth-wrong-attempt 429 transition | [services/verification/docs/ARCHITECTURE.md](../services/verification/docs/ARCHITECTURE.md) |
| VF-006 | Troubleshooting's mismatching-subject example actually matches | [services/verification/docs/DEPLOYMENT.md](../services/verification/docs/DEPLOYMENT.md) |
| VF-007 | Boot-guard rationale gives the wrong fake-backend confirm status | [services/verification/docs/DEPLOYMENT.md](../services/verification/docs/DEPLOYMENT.md), [services/verification/docs/ARCHITECTURE.md](../services/verification/docs/ARCHITECTURE.md) |
| MEETING-001 | README gives contradictory, obsolete disconnect behavior | [services/meeting/README.md](../services/meeting/README.md), [README.md](../README.md) |
| MEETING-002 | Privacy/retention passages still promise no data after stop | [services/meeting/README.md](../services/meeting/README.md) |
| MEETING-003 | Minutes documentation omits material lossy summarization behavior | [services/meeting/docs/API.md](../services/meeting/docs/API.md), [README.md](../README.md) |
| MEETING-004 | WS timestamp timebase is missing from the published wire contract | [services/meeting/docs/API.md](../services/meeting/docs/API.md), [README.md](../README.md) |
| MEETING-005 | Malformed text-frame tolerance is overstated | [services/meeting/docs/API.md](../services/meeting/docs/API.md), [README.md](../README.md), [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) |
| MEETING-006 | Boot documentation promises a stronger bootstrap-key check than exists | [services/meeting/README.md](../services/meeting/README.md) |
| MEETING-007 | README assigns prompt ownership to the wrong service | [services/meeting/README.md](../services/meeting/README.md) |
| MEETING-008 | Deploy-order guidance incorrectly says an LLM outage fails stop | [services/meeting/docs/DEPLOYMENT.md](../services/meeting/docs/DEPLOYMENT.md) |
| MEETING-009 | Contribution guide misstates when bundled assets are read and what the filesystem test covers | [services/meeting/docs/CONTRIBUTING.md](../services/meeting/docs/CONTRIBUTING.md) |
| CONN-001 | Contributor error-handling rule omits the writer's distinct contract | [services/connectors/docs/CONTRIBUTING.md](../services/connectors/docs/CONTRIBUTING.md), [services/connectors/docs/ARCHITECTURE.md](../services/connectors/docs/ARCHITECTURE.md) |
| CONN-002 | Restart and rollback guidance incorrectly rules out external state reconciliation | [services/connectors/docs/DEPLOYMENT.md](../services/connectors/docs/DEPLOYMENT.md), [services/connectors/README.md](../services/connectors/README.md) |
| CONN-003 | Sharing-error documentation promises revocation that is only best effort | [services/connectors/docs/API.md](../services/connectors/docs/API.md) |
| CONN-004 | Fetch response examples disagree with the serialized wire shape | [services/connectors/README.md](../services/connectors/README.md), [services/connectors/docs/API.md](../services/connectors/docs/API.md) |
| CONN-005 | Uploaded text is repeatedly described as a Drive export | [services/connectors/README.md](../services/connectors/README.md), [services/connectors/docs/API.md](../services/connectors/docs/API.md), [services/connectors/docs/ARCHITECTURE.md](../services/connectors/docs/ARCHITECTURE.md) |
| CONN-006 | Current service and endpoint inventories still describe a fetch-only surface | [services/connectors/README.md](../services/connectors/README.md), [services/connectors/docs/API.md](../services/connectors/docs/API.md) |
| CONN-007 | Authentication statements incorrectly include the schema and Swagger endpoints | [services/connectors/README.md](../services/connectors/README.md), [services/connectors/docs/API.md](../services/connectors/docs/API.md) |
| CONN-008 | Troubleshooting overstates what a Google 403 and the audit log prove | [services/connectors/docs/API.md](../services/connectors/docs/API.md), [services/connectors/docs/DEPLOYMENT.md](../services/connectors/docs/DEPLOYMENT.md) |
| GW-DOC-001 | Configuration table retains an unpinned internal-key command | [services/gateway/README.md](../services/gateway/README.md), [README.md](../README.md) |
| AUTH-01 | Key-store guide misclassifies verification and omits gateway | [packages/auth/README.md](../packages/auth/README.md) |
| AUTH-02 | Shared-auth rollout and test checklist excludes gateway | [packages/auth/docs/CONTRIBUTING.md](../packages/auth/docs/CONTRIBUTING.md) |
| AUTH-03 | Copyable key-store expression passes a wrapped secret | [packages/auth/README.md](../packages/auth/README.md) |
| AUTH-04 | Development-scope rejection is described as broader than implemented | [packages/auth/README.md](../packages/auth/README.md), [packages/auth/docs/CONTRIBUTING.md](../packages/auth/docs/CONTRIBUTING.md) |
| AUTH-05 | Scope-extension guide omits the special act-as-user contract | [packages/auth/docs/CONTRIBUTING.md](../packages/auth/docs/CONTRIBUTING.md) |
| DBOT-01 | Combined development mode is documented as using an isolated scratch directory | [discord-bot/README.md](../discord-bot/README.md) |
| DBOT-02 | The environment example's “full scope set” omits channel permissions | [discord-bot/.env.example](../discord-bot/.env.example) |
| DBOT-03 | Playground account-linking walkthrough omits verification | [discord-bot/README.md](../discord-bot/README.md) |
| DBOT-04 | Request-allowance cost explanation still assumes a single model call | [discord-bot/docs/helper-limits.md](../discord-bot/docs/helper-limits.md) |
| DBOT-05 | Node 20 prerequisite conflicts with the locked voice dependency | [discord-bot/README.md](../discord-bot/README.md), [discord-bot/docs/CONTRIBUTING.md](../discord-bot/docs/CONTRIBUTING.md), [docs/DEVELOPMENT.md](../docs/DEVELOPMENT.md) |
| DBOT-06 | Preview database-isolation setup counts only three databases | [docs/pr-previews.md](../docs/pr-previews.md) |
| DBOT-07 | Preview timeout guidance understates the current maximum | [docs/pr-previews.md](../docs/pr-previews.md), [.github/workflows/discord-preview.yml](../.github/workflows/discord-preview.yml) |
| DBOT-08 | Bot deployment-variable table omits mandatory LLM settings | [docs/RAILWAY-DEPLOYMENT.md](../docs/RAILWAY-DEPLOYMENT.md) |
| DBOT-09 | Deployment notes still claim the Discord PDF is the only retained meeting copy | [docs/RAILWAY-DEPLOYMENT.md](../docs/RAILWAY-DEPLOYMENT.md) |
| DBOT-10 | Manual registration example uses a nonexistent script path | [docs/RAILWAY-DEPLOYMENT.md](../docs/RAILWAY-DEPLOYMENT.md), [discord-bot/README.md](../discord-bot/README.md) |
| DBOT-11 | Channel append is described as client-side union plus PUT | [discord-bot/README.md](../discord-bot/README.md) |
| DBOT-12 | Recording infrastructure guidance names settings that do not exist | [discord-bot/README.md](../discord-bot/README.md) |
| DBOT-13 | Contributor authorization example contradicts the commands it recommends copying | [discord-bot/docs/CONTRIBUTING.md](../discord-bot/docs/CONTRIBUTING.md) |
| DBOT-14 | Root service summary advertises nonexistent `/doc show` | [README.md](../README.md) |
| DBOT-15 | Root admin guide says `/seed` can promote an existing person | [README.md](../README.md) |
| DBOT-16 | Deferred-work list still lists the implemented LLM adapter | [discord-bot/README.md](../discord-bot/README.md) |
| TOOL-001 | Preview setup isolates only three of four databases | [docs/pr-previews.md](../docs/pr-previews.md) |
| TOOL-002 | “Full” local test instructions silently skip CI's database tests | [README.md](../README.md), [AGENTS.md](../AGENTS.md), [docs/DEVELOPMENT.md](../docs/DEVELOPMENT.md), [Makefile](../Makefile) |
| TOOL-003 | Root command reference has obsolete names and authorization | [README.md](../README.md) |
| TOOL-004 | Retrieval is incorrectly marked unbuilt | [README.md](../README.md) |
| TOOL-005 | Local verification instructions request a code that is never logged | [docs/DEVELOPMENT.md](../docs/DEVELOPMENT.md) |
| TOOL-006 | Developer inventory omits gateway and recommends its occupied database port | [docs/DEVELOPMENT.md](../docs/DEVELOPMENT.md), [README.md](../README.md), [Makefile](../Makefile) |
| TOOL-007 | Cross-service setup omits implemented embedding and persistence dependencies | [README.md](../README.md), [docs/DEVELOPMENT.md](../docs/DEVELOPMENT.md) |
| TOOL-008 | Documented zone-enforcement edit rejects single-zone PRs too | [docs/CODE-OWNERSHIP.md](../docs/CODE-OWNERSHIP.md), [.github/workflows/pr-zone-check.yml](../.github/workflows/pr-zone-check.yml) |
| TOOL-009 | Preview timeout arithmetic no longer fits Actions | [docs/pr-previews.md](../docs/pr-previews.md), [.github/workflows/discord-preview.yml](../.github/workflows/discord-preview.yml) |
| TOOL-010 | `ready` is not a guarantee that an issue is unassigned | [docs/ONBOARDING.md](../docs/ONBOARDING.md), [.github/workflows/blocked-ready-automation.yml](../.github/workflows/blocked-ready-automation.yml) |
| TOOL-011 | Monitoring runbook promises failure details that logs do not contain | [docs/READINESS-MONITORING.md](../docs/READINESS-MONITORING.md) |
| TOOL-012 | `README.md:50,247` advertises “26 endpoints” and a resource list without channels. | [README.md](../README.md) |
| TOOL-013 | `docs/CODE-OWNERSHIP.md:90,198` says fourteen zones and twelve template options. | [docs/CODE-OWNERSHIP.md](../docs/CODE-OWNERSHIP.md), [.github/workflows/pr-zone-check.yml](../.github/workflows/pr-zone-check.yml) |
| TOOL-014 | `docs/CODE-OWNERSHIP.md:74-75` says label-consistency is the only mechanism that fails a run. | [docs/CODE-OWNERSHIP.md](../docs/CODE-OWNERSHIP.md) |
| TOOL-015 | `AGENTS.md:103` says ten CI jobs; `docs/DEVELOPMENT.md:442-448` says three other workflows fire on PRs and includes blocked-ready automation. | [AGENTS.md](../AGENTS.md), [docs/DEVELOPMENT.md](../docs/DEVELOPMENT.md), [.github/workflows/blocked-ready-automation.yml](../.github/workflows/blocked-ready-automation.yml) |
| TOOL-016 | `README.md:167` describes the issue forms as having “Blocked by + Zone fields.” | [README.md](../README.md) |
| CS-001 | Railway table disables the catalog's production boot protection by omission | [docs/RAILWAY-DEPLOYMENT.md](../docs/RAILWAY-DEPLOYMENT.md) |
| CS-002 | Railway notes promise that no meeting content survives | [docs/RAILWAY-DEPLOYMENT.md](../docs/RAILWAY-DEPLOYMENT.md) |
| CS-003 | Bot deployment variables omit a mandatory LLM connection and key | [docs/RAILWAY-DEPLOYMENT.md](../docs/RAILWAY-DEPLOYMENT.md) |
| CS-004 | The API verification command probes the operator's machine | [docs/RAILWAY-DEPLOYMENT.md](../docs/RAILWAY-DEPLOYMENT.md) |
| CS-005 | The platform guide still presents Bedrock as the chat backend/prerequisite | [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md), [docs/RAILWAY-DEPLOYMENT.md](../docs/RAILWAY-DEPLOYMENT.md) |
| CS-006 | Architecture says implemented retrieval is deferred and omits its dependencies/failure behavior | [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) |
| CS-007 | Repeat ingestion is documented as a tag-only operation | [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) |
| CS-008 | Current schema/deployment inventory is stale | [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md), [docs/RAILWAY-DEPLOYMENT.md](../docs/RAILWAY-DEPLOYMENT.md) |
| CS-009 | Later recording guidance says moved saved recordings keep going | [docs/MEETING-RECORDING.md](../docs/MEETING-RECORDING.md) |
| CS-010 | Failed-record troubleshooting uses an ignored query parameter | [docs/MEETING-RECORDING.md](../docs/MEETING-RECORDING.md) |
| CS-011 | The four-hour backstop is described like automatic finalization | [docs/MEETING-RECORDING.md](../docs/MEETING-RECORDING.md) |
| CS-012 | The cross-service wire contract omits the audio-complete control frame | [docs/MEETING-RECORDING.md](../docs/MEETING-RECORDING.md) |
| CS-013 | Readiness logs do not provide the promised fallback outage visibility | [docs/READINESS-MONITORING.md](../docs/READINESS-MONITORING.md) |
| CS-014 | General rollback guidance omits irreversible cleanup exceptions and downgrade preconditions | [docs/DEPLOYMENT-HISTORY.md](../docs/DEPLOYMENT-HISTORY.md) |
| CS-015 | The current CI checklist omits gateway coverage and still counts ten jobs | [docs/DEPLOYMENT-HISTORY.md](../docs/DEPLOYMENT-HISTORY.md), [docs/RAILWAY-DEPLOYMENT.md](../docs/RAILWAY-DEPLOYMENT.md) |
| CS-016 | Undated key-provisioning guidance grants too few directory scopes | [docs/DEPLOYMENT-HISTORY.md](../docs/DEPLOYMENT-HISTORY.md) |
| CS-017 | The scaling guidance excludes gateway's process-local limits | [docs/DEPLOYMENT-HISTORY.md](../docs/DEPLOYMENT-HISTORY.md) |
| CS-018 | Offboarding omits the catalog and gateway key registries | [docs/ACCESS.md](../docs/ACCESS.md) |
| CS-019 | The listed Gmail deployment alternative lacks its required settings | [docs/RAILWAY-DEPLOYMENT.md](../docs/RAILWAY-DEPLOYMENT.md) |
| CS-020 | The manual registration wrapper path is wrong after changing directories | [docs/RAILWAY-DEPLOYMENT.md](../docs/RAILWAY-DEPLOYMENT.md) |
| LLM-E01 | Duplicate validation-errors section | [LLM API](../services/llm/docs/API.md) |

## Implementation limits remain

- The locked Discord voice dependency requires Node >=22.12.0 while runtime/CI declarations still allow or select Node 20. The documentation identifies the mismatch; no runtime or dependency was changed.
- `make test-full` does not set opt-in database-test flags. The docs now require the service-specific disposable database setup; the wrapper is unchanged.
- Catalog `RUN_PG_TESTS=1` already enables some destructive migration tests; withholding `RUN_PG_MIGRATION_TESTS` does not make a retained database safe.
- Issue ready-label automation, readiness log visibility and preview timeout behavior are unchanged; their current limits are documented.
- Some email-provider exceptions escape the normalized 502 path, and malformed meeting control values can exit the WebSocket handler. These are documented implementation gaps.
- Meeting’s known-default bootstrap check does not reject an explicitly empty key. Real credential provisioning remains required.
- Starting meeting `/stop` after a disconnect does not cancel grace teardown. At expiry, a still-registered session is discarded, which can interrupt an unfinished transcription flush.
- Inactive directory providers can receive identifiers, Google group owners/managers survive ordinary member reconciliation, and unexpected-ACL revocation is best effort. The docs no longer promise stronger guarantees.
- No live deployment, permission cleanup, provider delivery, branch protection or remote resource state was established by this documentation change. Unconfirmed audit hypotheses were not promoted to facts.

## Verification

- Compared changed claims and examples with the owning source, manifests and existing regression-test source.
- Three Codex workers in Orca independently reviewed the root/shared, bot/auth and meeting/connectors/LLM changes after implementation; the coordinator reviewed the directory/catalog changes. Review findings were corrected in the docs, including migration-test opt-ins, embedding error wording, command/service inventories, public auth exceptions and disconnect recovery. Subsequent wording changes were checked against the cited source paths.
- Checked all local Markdown link targets and heading anchors, plus `git diff --check`.
- Ran `make labels` for repository zone/area consistency.
- Compared every non-Markdown change with the baseline after removing documentation comments: active configuration and executable lines are identical. The Makefile change is only its inline help comment.
- Attempted `UV_NO_SYNC=1 UV_OFFLINE=1 make check`; it stopped at the first lint command because this checkout has no installed `ruff`. No dependencies were installed and no application suite ran through that command.
- Full application, database, provider and Docker suites were not rerun for this documentation-only change. Earlier audit test runs are not claimed as new validation.
