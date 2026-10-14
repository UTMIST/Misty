# PR #262 pending-membership review fix

An unfinished Google addition can complete after a later sync has seen an
empty group. Forgetting that addition lets the later sync report `synced`,
even though Google can subsequently restore a departed member's access.

The reconciler now persists each membership intent before sending the write.
It keeps unfinished or uncertain writes across retries and process restarts,
and waits to observe their effect before reversing the change.
Unrelated revocations continue. Pending writes keep the mapping `failed`, so
both synced-group lookups exclude it. A completed delayed addition is removed
on the next sync if the person no longer belongs.

Cloud Identity has no documented operation-polling method. Requests whose
effects never appear and cannot be safely retried require investigation before
their pending state can be cleared; see
[the recovery procedure](DEPLOYMENT.md#pending-membership-changes).
This fixes the false healthy state; Google access already granted by a delayed
write is removed when reconciliation runs, not at the instant Google finishes.

A follow-up check distinguishes connection-establishment timeouts and OAuth
refresh transport failures, which are safe to retry, from uncertain write
outcomes. It also rejects malformed Google read responses instead of
interpreting them as an empty membership list and prematurely clearing a
pending removal.

An independent Claude Code review identified that uncertain removals also need
retries when the person remains unwanted. These now reuse the original pending
record and exact membership resource; a rejected retry cannot erase an earlier
uncertain outcome. Additions remain guarded because replaying one can recreate
the delayed-addition race. The review also identified the session-lock deployment
requirement, now explicit in the deployment and architecture docs: API and cron
must use a direct database endpoint, not a transaction pooler. The deployed
connection configuration was not inspected during this review.

## Changed-file justification

Paths below are relative to `services/team-tracking/` unless prefixed by `/`.

| File | Why it changes |
|---|---|
| `contracts/groups.py` | Distinguishes an unconfirmed write from a definite rejection. |
| `contracts/types.py` | Defines the pending-change record and exposes it on group state. |
| `contracts/storage.py` | Extends the shared upsert contract to persist pending changes. |
| `src/storage/schema.py` | Defines the JSONB column and its empty-list default. |
| `migrations/versions/010_pending_group_changes.py` | Adds the column for existing deployments; prevents rollback from discarding unresolved writes. |
| `src/storage/in_memory.py` | Implements pending-state persistence with copied values in the fast adapter. |
| `src/storage/postgres.py` | Serializes pending records to JSONB and restores them through the shared model. |
| `src/providers/google_groups.py` | Preserves unfinished-operation identities and classifies uncertain write outcomes without exposing credentials. |
| `src/google_groups.py` | Saves intents before calls, retains unresolved writes, and reconciles completed effects against current membership. |
| `tests/test_google_groups.py` | Covers delayed adds/removals, revocations, retries with fresh providers, interrupted writes, and uncertain responses. |
| `tests/test_postgres_adapter.py` | Verifies stored state survives new adapters/providers and is cleared only after reconciliation. |
| `docs/API.md` | Documents the added response field and pending-state semantics. |
| `docs/ARCHITECTURE.md` | Explains persistence, recovery, and the updated schema/migration history. |
| `docs/DEPLOYMENT.md` | Records the coordinated API/cron rollout and operator recovery requirements. |
| `README.md` | Updates the migration inventory and current head. |
| `/AGENTS.md` | Updates the authoritative migration-head pointer. |
| `docs/GOOGLE-GROUPS-REVIEW.md` | Records this fix's scope, file justification, and validation. |

## Validation

- The full team-tracking suite passes against an isolated Postgres 16 instance.
- Imported `src` and `contracts` paths were verified against this checkout.
- Pinned Ruff lint and format checks pass.
- Repository-wide `make check` passes.
- Claude Code's final static review found no remaining correctness blockers in
  the corrections; the direct database endpoint remains a deployment prerequisite.
- Migration `010 -> 009 -> 010` succeeds with resolved state, preserves an
  existing row with the new default, and refuses rollback with unresolved writes.
- Google behavior is exercised only through injected fakes; no live Google
  membership changes were made.
