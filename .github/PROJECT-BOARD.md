# Issue availability and project status

This guide describes the checked-in
[`blocked-ready-automation.yml`](workflows/blocked-ready-automation.yml).
It updates issue availability labels and optionally two project status values.
Repository files do not establish which native GitHub Projects automations are
enabled; inspect the project's settings before relying on them.

## Declare blockers and claim work

Put same-repository issue numbers in the issue form's **Blocked by** field,
for example `#40, #42`. The legacy line `Blocked by: #40, #42` also works.
Native dependency relationships alone are not read by this workflow; mirror
hard prerequisites in the body. An inaccessible blocker remains unresolved.

Before claiming a `ready` issue, check its blockers, assignees, and project
status. Assign yourself to claim it. Assignment removes `ready`; move the
project item to In progress or In review yourself. The workflow does not
interpret `on hold`, so a maintainer must preserve deliberate deferrals when
reconciling availability.

## Events and limits

| Event | Implemented behavior |
|---|---|
| Issue opened or edited | For an open, unassigned issue with body-declared blockers, sets `blocked` or `ready` from their states. With no blocker references, does nothing. |
| Issue assigned | Removes `ready`; leaves project status unchanged. |
| Last assignee removed | Restores `ready` and Ready status when no declared blockers remain open. |
| Blocker closed | Updates open dependent issues whose declared blockers are all closed to `ready` and Ready, including assigned dependents. |
| Blocker reopened | Updates open dependent issues to `blocked` and Blocked. |

Closing a blocker can therefore reset an assigned dependent's status. Inspect
affected work and restore its actual status and labels when necessary. A
`ready` label alone does not prove an issue is unassigned or ready to claim.
There is no scheduled reconciliation, PR/review event handler, native-dependency
reader, or project auto-add operation in this workflow.

## Project configuration and recovery

Label updates use the workflow's `issues: write` permission. Optional project
updates use the `PROJECTS_TOKEN` repository secret, with access to the intended
organization project. The workflow contains the project, Status field, Ready
option and Blocked option IDs; verify those IDs before changing the project.
Items must already belong to that project. Missing tokens skip project updates;
missing items and API failures are logged without failing the whole run.

Configure auto-add, initial status, and any review/merge transitions in the
project UI if desired. They are separate from this workflow. To verify setup,
use disposable issues to exercise blocker close/reopen and assignment, inspect
the Actions logs, and check labels and project status separately. Do not infer
successful project updates from a green workflow alone.

If an event is missed, inspect the issue's current blockers and ownership and
repair its labels/status manually. Editing the blocker field on an open,
unassigned issue with declared blockers triggers another evaluation. Re-running
an old event is not a general reconciliation operation: its event payload may
no longer describe current ownership or state.

## Closing links

Use `Closes #N` only when the PR completes the entire issue. Partial work uses
a plain reference and leaves acceptance open. Closing keywords take effect on
merge to the repository's default branch, `staging`; they do not establish that
production acceptance or a release has happened. Keep epic acceptance separate
from partial implementation PRs.
