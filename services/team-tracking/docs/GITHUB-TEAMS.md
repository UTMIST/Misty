# Managed GitHub teams

Team-tracking owns durable, audited mappings. Connectors owns GitHub reads and
creation with a separate administrative credential. Human administrators invoke
these operations through appropriately restricted service keys; they are not
registered as model tools. Ordinary `teams:write` cannot manage these mappings.

## HTTP contract

All bodies forbid unknown fields. Every operation requires `X-API-Key`.

| Service / method | Path | Scope | Result |
|---|---|---|---|
| Directory GET | `/teams/{uuid}/github-team` | `github-teams:read` | Mapping status (revalidates active IDs), 404 if absent |
| Directory POST | same | `github-teams:manage` | Reserve then adopt/provision, 409 for conflicts |
| Directory POST | same + `/reconcile` | `github-teams:manage` | Read provider state; refresh display names or recover pending creation |
| Directory DELETE | same | `github-teams:manage` | Soft-disable, increment version; never delete the GitHub team |
| Connectors GET | `/github/teams/{github_id}` | `github-teams:read` | Stable organization/team ID and current name/slug |
| Connectors POST | `/github/teams` | `github-teams:manage` | Provision/reconcile the exact Misty UUID |

Adopt: `{"mode":"adopt","github_team_id":123,"adoption_reviewed":true}`.
Provision: `{"mode":"provision"}`. The connector request is
`{"misty_team_id":"<uuid>","reconcile_only":false}` for the initial operation;
recovery always sets `reconcile_only:true`.

Stable IDs, not names, determine uniqueness. Both a Misty team and a GitHub team
can have only one mapping, including retained inactive mappings. Deactivation
is permanent for this foundation: a new identity cannot silently replace it.
Changes of identity require a separately reviewed migration, not direct SQL
against an uncertain creation. The snapshot includes `status`, `version`, and
actor/timestamps. An active read refreshes names and disables a known deleted team. `active` is
adoption state, not proof of current
repository permissions; downstream authorization must freshly reconcile and
check source grants before disclosure. Retirement disables the mapping in the
same directory write and reactivating a directory team does not reactivate it.
Reconciliation of a deleted GitHub team disables its known mapping. An unknown
404 after creation is uncertain, not proof that nothing was created.

## Explicit adoption policy

Before setting `adoption_reviewed:true`, an organization owner must review the
actual GitHub team's members, maintainers, nesting, repository grants and IdP
management. Existing members/maintainers are preserved untouched. There are no
membership-write or permission-write operations in this change. Nested teams
(parent or child) and observed IdP-group mappings are rejected; an IdP-managed
team must not be adopted. An unavailable Enterprise team-sync feature can return
404, so owner review is required even when the API cannot report IdP mappings.
Outside collaborators are not converted to organization members or automatically
added to teams. Adoption does not enroll repositories or change Discord access.
Any future membership writer must independently recheck these restrictions and
explicitly approve its reconciliation policy before it is enabled.

## Uncertain operations and recovery

The directory reserves a `pending` row before the first provider operation. A
second POST cannot create again. Provision uses a deterministic `misty-<uuid hex>`
name and description marker. A lost response is recovered by read-only lookup
of that exact identity and marker. Reconciliation never sends a second create.
A mismatched marker is a conflict, not permission to adopt the existing team.
Renames of known IDs refresh metadata without changing identity. If creation
may still be running and no team can be found, leave it pending and inspect
GitHub/API activity; do not delete the reservation to retry. A rejected adoption
retains its reserved ID and can be reconciled after the external problem is
resolved, or deactivated. Provider errors are static codes without vendor bodies.
Provider requests have timeouts and a 64 KiB response cap; there are no automatic
write retries. Keep the admin token outside logs and content-fetch tools.

Apply directory migration 012 before enabling the routes. Upgrade, downgrade one
revision, and upgrade again on a disposable database with existing directory rows.
Use Members write for provisioning and read for inspection in GitHub; a GitHub
App installation token must be rotated by the deployment's credential owner.
Disable by revoking the dedicated consumer/admin keys; existing GitHub teams
and membership are preserved. No production changes are made by these tests.
