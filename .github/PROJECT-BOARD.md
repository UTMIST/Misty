# Project board automation

[Misty project 13](https://github.com/orgs/UTMIST/projects/13) tracks issues and
PRs separately. Issue completion means its declared scope is complete; a merged
implementation PR does not prove unrelated live acceptance. Keep unfinished
acceptance in an open issue and use a plain reference for partial delivery.

## Linking work and closing it

Put `Closes #123` (or `Fixes #123` / `Resolves #123`) in the **PR description**
only when merging should finish the whole issue. GitHub's Development sidebar
also supports explicit closing links. A mention in a title, branch name,
comment, or `Part of #123` is not a closing link.

The repository's default branch is **staging**. GitHub automatically closes
explicitly linked issues when their PR merges there; production promotion to
`main` is separate. Closing an unmerged PR does not close its issue. Reopening
an issue after a merge keeps it open. The reconciliation script never issues
an issue-close request or infers delivery from a plain issue number.

See GitHub's [closing-link rules][closing] before changing the default branch.

## Native project settings

These settings live in the project's **Workflows** UI, outside repository YAML.
Configure them as follows:

| Workflow | Setting |
| --- | --- |
| Auto-add to project | Repository `UTMIST/Misty`, filter `is:issue,pr is:open -label:"automation/report"` |
| Auto-add sub-issues | Enabled |
| Item added | Issues and PRs → Backlog |
| PR linked to issue | Issue → In progress |
| Code changes requested | PR → In progress |
| Code review approved | PR → In review |
| Item closed / PR merged | Done |
| Item reopened | Issues and PRs → Backlog |
| Auto-close issue | Moving an issue to Done closes it |
| Auto-archive | Closed issues not updated for two weeks |

The API exposes workflow names and enabled states, but not their filter/status
configuration. Inspect the UI when auditing these settings. Native auto-add
handles future matching updates; enabling it does not backfill all old items.

## Repository reconciliation

[`project-board.yml`](workflows/project-board.yml) runs on issue, PR and review
events, hourly, and on manual dispatch. It reads current repository
and project state, so an older queued event cannot replay an old issue body or
PR status. The scheduled pass repairs missed events and native relationship
edits. Fork review events lack the project secret and use that scheduled pass.

`project-board.yml` replaces `blocked-ready-automation.yml`; do not re-enable
the retired workflow. **The replacement must reach `staging` before it runs.**
A review event before installation reports a skip when both the workflow and
reconciler are absent from the default-branch checkout. An installed workflow
with a missing script still fails, as do missing scripts on non-review events.
Native project settings operate independently.

| Current evidence | Reconciled status |
| --- | --- |
| New issue without explicit readiness | Backlog |
| Open draft PR or requested changes | PR and closing-linked issue → In progress |
| Open non-draft PR awaiting review or approved | PR and closing-linked issue → In review |
| Multiple linked PRs, any still draft/requiring changes | Issue → In progress |
| Explicit `on hold` on issue, or all its open PRs held | Backlog |
| Issue closed | Done; remove availability labels |
| PR merged or closed unmerged | PR → Done; issue closure still belongs to GitHub |
| Issue reopened or its only review PR abandoned | Leave Done/review; retain an assignee's In progress, otherwise Backlog |

Done on an **unmerged PR** records its disposition, not shipped functionality.
An approved PR stays In review until merge. A held PR cannot finish its issue.
An issue with other non-held PRs continues to follow those active PRs.

The workflow combines native **Blocked by** dependencies with issue-form
references (`### Blocked by` followed by `#40, #42`) and legacy
`Blocked by: #40, #42`. Put explanatory references outside that field. An
inaccessible dependency counts as unresolved. Blocked work uses the `blocked`
label; there is deliberately no Blocked status column. Removing the last
blocker makes unassigned Backlog work Ready, except explicitly held work.
Assigned or active work is never reset to Ready by a blocker closing.

Only Status, `ready` and `blocked` are reconciled. Priorities, sizes, dates,
assignees, descriptions and acceptance checkboxes are not rewritten. Historical
archived closed cards stay archived; reopened cards are restored. Use the
`on hold` label for deliberate deferrals, rather than relying on a manually
placed Backlog card to override a linked PR. Native transitions can temporarily
precede reconciliation; the scheduled pass restores this policy after missed
or competing events.

## Additional automation

These workflows provide review evidence without rewriting acceptance criteria.
They use the normal Actions token for repository metadata; only project reads
and reconciliation need `PROJECTS_TOKEN`. No Railway credential is required.

| Automation | Trigger and result |
| --- | --- |
| [Health check](workflows/automation-health.yml) | Daily at 12:47 UTC and after board, review, PR-link or deployment workflows finish; one reusable `automation/health` issue for failures, closed on recovery and reopened on recurrence |
| [PR issue-link advice](workflows/pr-issue-check.yml) | PR description, label and lifecycle updates; warnings in annotations and the job summary for missing issue links, broad epic closure or unchecked acceptance |
| [Deployment evidence](workflows/deployment-evidence.yml) | Actual GitHub `deployment_status` events from Railway; one updatable comment per deployment on the exact merged PR and its explicit same-repository closing issues |
| [Weekly exceptions](workflows/board-review.yml) | Mondays at 13:43 UTC; one reusable `automation/report` issue containing review queues, unassigned high-priority work and holds to revisit |
| Epic completion review | Included in the weekly report when all native direct sub-issues are closed; cancelled children and unchecked parent criteria remain explicit review items |

The health check is separate from the board writer. It checks that the board,
review, PR-link, deployment, health and CI workflows are enabled, checks the
latest completed board/review scheduled runs and PR-link/deployment runs, and
probes project access and Status options. A board heartbeat expires after four
hours; a weekly heartbeat after nine days. Newly created workflows have the
same startup grace. A manual dry run cannot renew a scheduled heartbeat. CI
failures are left to CI notifications; disabled CI is a health finding. The
incident updates only when findings change. The retired blocker workflow and
the deliberately deferred readiness monitor are excluded.

This check cannot alert when its own workflow is disabled or GitHub Actions is
unavailable. Its daily schedule and `workflow_run` trigger are complementary,
not an external uptime monitor. Workflow metadata also cannot verify native
project filter settings; audit those in the project UI.

PR advice verifies same-repository issue references in the description and
reads native closing links. It ignores fenced examples and historical details.
Documentation-only and Dependabot PRs are exempt from the missing-link warning;
maintainers can apply `no-issue-needed` to other justified exceptions. Exemptions
do not suppress epic/acceptance warnings. Advice does not fail the job or post
comments; API/inspection failures do fail visibly. Do not make this advisory a
required merge check. Native link edits may need a manual rerun; the workflow
also accepts a `pr_number` for inspection.

The weekly report flags non-draft review PRs without activity for three days,
approved PRs without activity for two days, unassigned P0/P1 project issues,
and `on hold` work without activity for fourteen days. Ages use GitHub's updated
timestamp, not the exact review-request or hold date. Epic review uses native
direct sub-issues, not arbitrary mentions or a recursive proof of acceptance.
It never closes epics, assigns work or resumes a hold. `automation/report` is
excluded from native auto-add and repository reconciliation; do not manually
add the report as deliverable work or make it a sub-issue. Health incidents
remain actionable board items. Keep bot markers and automation labels on the
managed issues so subsequent runs can find them; duplicate bot reports fail
instead of silently choosing one. The next weekly run reopens a closed report.

Deployment evidence is restricted to the existing Misty staging/production
Railway environment IDs in the script. It rereads the latest deployment status,
uses only Railway-authored records and URLs for this project, and bounds service
observations to the same commit, environment and deployment time window. A CI
success is never deployment proof. Pending/failure states remain visible as
such; unobserved services are not claimed as deployed. The PR must have a merge
commit equal to the deployed SHA. Production evidence attaches to the promotion
PR, not every prior feature PR. PR previews and unrelated deployments are ignored.
GitHub does not emit this event for `inactive` statuses; comments are dated
observations rather than a current-inventory guarantee. Use a manual dispatch
with `deployment_id` to replay a missed event. Production event handling also
requires this workflow to reach the deployed `main` revision through promotion.

## Credentials, rollout and recovery

`PROJECTS_TOKEN` must have permission to read/write this organization's project
and read repository issue/PR metadata (classic PAT: `project`, plus `repo` for
private repositories). The normal Actions token changes issue labels. No token
value belongs in evidence, logs or checked-in configuration. Missing credentials,
API failures, missing status options and truncated relationship reads fail the
job visibly; they must not produce a green labels-only run.

The privileged job checks out the repository's default branch with persisted
credentials disabled. It never checks out or executes PR-head code. Tests run
with offline API fakes in ordinary CI, including merge groups.

After merging into `staging`:

1. Dispatch **Sync project board** with `dry_run=true`. Review its job summary
   and proposed status/label changes; verify the configured token works.
2. Dispatch with `dry_run=false`, inspect representative issue/PR cards, then
   repeat the dry run to confirm convergence.
3. Check a draft → ready-for-review → changes-requested → approval → merge
   journey and an unmerged close/reopen journey on legitimate development work.
   Verify both the PR and its explicitly linked issue; do not merge a fake
   implementation just to exercise automation.
4. Dispatch **Review board exceptions** and **Check automation health** with
   `dry_run=true`; inspect their summaries before dispatching with `false`.
   Confirm the native report exclusion above. The `automation/report`,
   `automation/health` and `no-issue-needed` labels must exist before activation.
5. Dispatch **Check PR issue links** for an existing PR and **Record deployment
   evidence** with an existing GitHub deployment ID and `dry_run=true`. After
   review, replay that deployment with `false` or observe a real subsequent
   deployment. Verify one comment per target/deployment after a repeat run.
6. Verify scheduled board and weekly runs after their first scheduled execution.
   A local authenticated API preview does not test the Actions secret, hosted
   permissions, event delivery or the scheduler.

To pause repository reconciliation, disable **Sync project board** in Actions.
Do not revive the retired workflow. Native automation remains separately
configurable. After a token/schema correction, use the dry-run/manual-dispatch
sequence; a scheduled full snapshot repairs missed changes. To stop all status
automation, also disable the relevant native workflows in project settings.
Each additional workflow can be disabled independently in Actions. Keep the
health monitor enabled to expose accidental disables; an intentional pause of
watched workflows will remain a finding until restored or deliberately removed
from its watched list. No pause should delete acceptance work or old evidence.

## Verification

Run `node --test .github/scripts/*.test.cjs` with Node 22,
`node scripts/check-labels.mjs`, and Actionlint on the changed workflow YAML.
Tests cover transitions, multiple PRs, holds, dependency removal/reopening,
pagination, idempotence, archived history, dry-run writes and fail-closed reads.
The additional suite covers report lifecycle and deduplication, advisory
exceptions, schedule freshness, credential failure, epic cancellation, trusted
deployment evidence and exact PR association. All tests use offline fakes.
The activation suite executes the workflow's actual script with fake runner
boundaries, covering pre-installation reviews, active runs and visible failures
for missing scripts or credentials. Hosted permissions and event delivery still
need the rollout checks above; record that evidence in the implementation PR
or issue.

[closing]: https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue
