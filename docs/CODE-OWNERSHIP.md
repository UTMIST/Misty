# Code ownership: zones

A **zone** is one top-level ownership bucket: a directory the repo treats as a
single unit for two purposes — who gets auto-requested for review, and how wide
a single PR is allowed to be. Zones are not a build concept, a deploy concept,
or a runtime concept. They exist only for review scope.

> **"Zone" is the word.** Older comments, commit messages, and PR history say
> "area" for this same thing — they mean zones. The word "area" is being kept
> free for the `area/*` issue-label namespace, which groups by *kind of work*
> (`area/service`, `area/tooling`, …) and is a different axis from these
> directory buckets. One `area/service` issue can span six zones.

---

## The canonical list

Fourteen buckets. Every tracked file lands in exactly one.

| Zone | What it covers | Owner | `pr-zone-check` pattern | CODEOWNERS line |
|---|---|---|---|---|
| `discord-bot` | Discord frontend for the platform — slash commands over the team-tracking API. Holds no database and no business logic. [README](../discord-bot/README.md) | @qiuethan | `discord-bot/*` | `/discord-bot/` |
| `packages/auth` | `platform-auth`, the shared API-key auth library — keys, scopes, audit middleware — that every service wires in via `build_auth(...)`. [README](../packages/auth/README.md) | @qiuethan | `packages/auth/*` | `/packages/auth/` |
| `packages/other` | Any package with no zone of its own yet. Transitional — see below. | @qiuethan | `packages/*` | `/packages/` |
| `services/connectors` | Stateless `POST /fetch` — returns document text from external sources (Google Drive/Docs) so consumers never hold source credentials. [README](../services/connectors/README.md) | @qiuethan | `services/connectors/*` | `/services/connectors/` |
| `services/documentation-system` | Catalog API for the org's links — docs, sheets, repos, videos — with owners, tags, and content snapshots. [README](../services/documentation-system/README.md) | @qiuethan | `services/documentation-system/*` | `/services/documentation-system/` |
| `services/llm` | Stateless `POST /chat` fronting Claude on Bedrock. The single choke point for credentials, model catalog, and provider quirks. [README](../services/llm/README.md) | @qiuethan | `services/llm/*` | `/services/llm/` |
| `services/meeting` | Stateful HTTP + WebSocket service: live Discord voice audio to Amazon Transcribe, then a rolling transcript, minutes, and a PDF. [README](../services/meeting/README.md) | @qiuethan | `services/meeting/*` | `/services/meeting/` |
| `services/team-tracking` | Source of truth for the directory — people, teams, roles, memberships, external identity mapping. [README](../services/team-tracking/README.md) | @qiuethan | `services/team-tracking/*` | `/services/team-tracking/` |
| `services/verification` | Proves someone controls an email address for a given subject, via short-lived one-time codes. [README](../services/verification/README.md) | @qiuethan | `services/verification/*` | `/services/verification/` |
| `services/other` | Any service with no zone of its own yet. Transitional — see below. | @qiuethan | `services/*` | `/services/` |
| `docs` | The top-level `docs/` only: cross-cutting docs that belong to no one service. A service's own `docs/` is part of that service's zone. | @qiuethan | `docs/*` | `/docs/` |
| `scripts` | Repo-level tooling run by hand or by CI — [`check-labels.mjs`](../scripts/check-labels.mjs), the provisioning and registration shell scripts. | @qiuethan | `scripts/*` | `/scripts/` |
| `.github` | CI and repo automation: the [workflows](../.github/workflows), [`CODEOWNERS`](../.github/CODEOWNERS), [`labeler.yml`](../.github/labeler.yml), and the issue and PR templates. | @qiuethan | `.github/*` | `/.github/` |
| `root` | Every top-level file: `README.md`, [`AGENTS.md`](../AGENTS.md), `Makefile`, `pyproject.toml`, `uv.lock`, `.claude/`, the dotfiles. | @qiuethan | everything else | *(the `*` fallback)* |

Every owner is a `@qiuethan` placeholder for now. Per-zone assignment happens as
people are hired — see [`CODEOWNERS`](../.github/CODEOWNERS) for how to add one
without accidentally removing yourself.

The two `*/other` buckets and `root` are **real destinations, not leftovers.**
A PR that edits `AGENTS.md`, `README.md`, `Makefile`, `pyproject.toml`, or
`.claude/` lands in `root`. A newly added service lands in `services/other`
until it gets its own line in both files.

---

## What consumes the list

| Mechanism | Effect | Blocking? |
|---|---|---|
| [`.github/CODEOWNERS`](../.github/CODEOWNERS) | GitHub auto-requests the zone's owner on a matching PR | No — `require_code_owner_reviews` is off, so it requests but does not gate |
| [`codeowners-valid.yml`](../.github/workflows/codeowners-valid.yml) | Fails when CODEOWNERS names an owner GitHub cannot resolve or who lacks write access — a failure that is otherwise completely silent | **Yes** — the job exits non-zero |
| [`pr-zone-check.yml`](../.github/workflows/pr-zone-check.yml) | Warns when one PR touches more than one zone; writes the list to the job summary | No — ends in `exit 0` by design |
| [`zone-label.yml`](../.github/workflows/zone-label.yml) + [`labeler.yml`](../.github/labeler.yml) | Applies `zone: <name>` labels from the paths a PR touches, so zones are visible on the PR list | No |
| [`label-consistency.yml`](../.github/workflows/label-consistency.yml) + [`scripts/check-labels.mjs`](../scripts/check-labels.mjs) | Fails when the copies of the list disagree, when `labeler.yml`'s globs resolve a path to a different zone than `zone_for()` does, or when a `services/*` / `packages/*` directory has no zone | **Yes, when made a required check** — the job itself exits non-zero |
| [`PULL_REQUEST_TEMPLATE.md`](../.github/PULL_REQUEST_TEMPLATE.md) | Prompts the author to name the zone | No |

`label-consistency` is the only one that fails a run, and only about the list's
*internal* consistency — nothing here gates a merge on which zones a PR touches.
`pr-zone-check` becomes enforcing by changing its trailing `exit 0` to `exit 1`
and adding the job as a required status check in branch protection.

## Zones are for PRs. Areas are for issues.

These are two different label namespaces on two different axes, and the split is
deliberate:

| | `zone: *` | `area/*` |
|---|---|---|
| Applies to | Pull requests | Issues |
| Answers | Which directory bucket, and so who reviews it | Which parts of the system are involved, and so what you need to know to pick it up |
| Cardinality | **One.** A file has exactly one zone, and a PR is expected to stay inside one | **Several.** Most issues here carry two or three |
| Set by | [`zone-label.yml`](../.github/workflows/zone-label.yml), from the changed paths | [`area-label-issues.yml`](../.github/workflows/area-label-issues.yml), from the form's Area dropdown |
| The list | The fourteen above | `bot`, `deployment`, `docs-system`, `integration`, `observability`, `service`, `tooling` |

The cardinality is the reason they cannot be one namespace. A zone is
single-valued by construction — that is the whole basis of `pr-zone-check`
nagging about multi-zone PRs. An area is multi-valued by nature: one
`area/service` issue routinely spans six zones. Collapsing them would either
make zones meaningless or make areas unusable.

So an issue is never given a zone, and a PR is never given an area. If you want
to know which zones an epic will touch, that falls out of its PRs.

---

## The rule

**Keep a PR inside one zone.** Not because CI stops you, but because a
single-zone PR has one obvious reviewer and one obvious blast radius.

Spanning zones is sometimes correct. The sanctioned cases:

- **A `packages/auth` change that ripples into every service's shim.** This is
  expected to span every zone — see
  [`packages/auth/docs/CONTRIBUTING.md`](../packages/auth/docs/CONTRIBUTING.md).
- **A protocol change touching both sides**, e.g. `meeting` and `discord-bot`.
- **A `staging → main` promotion**, which carries whatever accumulated.

In all three, say so in the PR's Zone section rather than leaving the warning
unexplained. What the rule is actually against is *incidental* spread — a
drive-by fix in a second service bundled into an unrelated PR.

For epics, list every zone in the epic body, then keep each sub-issue and each
PR inside one. `pr-zone-check` reads PRs, not issues.

---

## Four things that are not obvious

**`docs` means the top-level `docs/` only.** A service's own docs
(`services/llm/docs/API.md`) belong to `services/llm`, not to `docs`. This is
deliberate and convenient: [`AGENTS.md`](../AGENTS.md) requires you to update a
service's docs in the same PR as the code change, and because both sit in the
same zone, doing the right thing does not trip the multi-zone warning.

**The two mechanisms resolve in opposite directions, and both files now contain
a catch-all whose position depends on that.** `zone_for()` is a bash `case` —
**first** match wins, so the specific service arms sit **above** `services/*`.
CODEOWNERS is the reverse: **last** matching pattern wins, so the `/services/`
catch-all sits **below** nothing and **above** every `/services/<name>/` line.
Same intent, mirrored layout; get either backwards and the catch-all swallows
every service instead of only the unregistered ones. `check-labels.mjs` checks
the ordering in both files.

CODEOWNERS is also not cumulative — only the last line a file matches applies,
so an owner added to a specific zone line *replaces* the `*` fallback rather
than adding to it.

**Zones are a review boundary, not an architectural one.** They happen to line
up with the service directories because the services are independent, but the
list is maintained by hand and can drift from the layout — it has before. In
August 2026, five directories had no zone entry and fell through to the fallback
(commit `deee002`); at the same time the `services/*` catch-all was collapsing
every unlisted service into one bucket, so a PR spanning `llm` and `meeting`
counted as a single zone and never warned.

**A file that matches nothing still has a zone.** There is no unowned state.
`root` and the `*` fallback exist precisely so that a new top-level file is
attributed rather than silently ownerless.

---

## Adding or renaming a zone

Five files carry the list. Change them in the same PR — `label-consistency`
fails the build otherwise, and names the file you missed:

1. [`.github/workflows/pr-zone-check.yml`](../.github/workflows/pr-zone-check.yml)
   — a `zone_for()` case **above** the `services/*` / `packages/*` catch-alls.
   This one is canonical; the checker reads the others against it.
2. [`.github/CODEOWNERS`](../.github/CODEOWNERS) — the zone line and its owner.
3. [`.github/labeler.yml`](../.github/labeler.yml) — a `zone: <name>` key **and**
   a matching `!` negation in every catch-all bucket the new zone carves paths
   out of, because `labeler` applies *every* rule that matches rather than
   stopping at the first. Forget the negation and the zone gets two labels; the
   checker probes a path per zone and fails on exactly that.
4. [`.github/PULL_REQUEST_TEMPLATE.md`](../.github/PULL_REQUEST_TEMPLATE.md) —
   the Zone list.
5. This file's table.

Then create the label: `gh label create "zone: <name>" --color BFD4F2`. Nothing
checks that the labels exist — a missing one is created on first use, in a
random colour.

Run `make labels` before pushing to get the same answer CI will give you.

**Adding an area** is a separate, smaller job: the `areas` array in
[`area-label-issues.yml`](../.github/workflows/area-label-issues.yml) (canonical)
and the Area dropdown in each of the three issue forms. Same check, same
failure mode.

If the new zone is a service, its CI job in
[`ci.yml`](../.github/workflows/ci.yml) belongs in that PR too — a service has
shipped to staging with no CI coverage before, and `label-consistency` does not
catch that. It checks that the service *has a zone*, not that it has tests.

---

## Deliberate gaps, and one that isn't

**The templates offer 12 of the 14 zones, on purpose.** They list `root` but not
`services/other` or `packages/other`. Those two are *transitional* buckets — a
PR landing in one means something was added that nobody registered, and the
right response is to give it a real zone
([above](#adding-or-renaming-a-zone)), not to pick the catch-all off a menu.
`root` is different: a permanent destination that never resolves into anything
else, and the one the most ordinary PRs in this repo belong to.

**The gap that used to be here is closed.** Five files hold the zone list, and
[`label-consistency`](../.github/workflows/label-consistency.yml) now diffs them
against `zone_for()` on every PR. Both drift incidents so far traced to nobody
checking; correcting the copies by hand reset the clock without changing the
odds, which is why this is a CI step rather than a convention.

It also catches the incident directly: any directory under `services/` or
`packages/` without its own zone fails the build, rather than silently joining
the catch-all bucket where multi-zone PRs stop warning. And it probes one path
per zone through `labeler.yml`'s globs, so a bucket that double-labels — or a
mistyped glob that labels nothing — fails too, rather than being a set of keys
that merely *looks* right.

What it still cannot see: whether a zone's paths are the *right* paths. It
checks that the copies agree, not that the mapping matches how the code is
actually organised. A zone pointed at a directory that no longer exists passes
cleanly.

**The latent one is now closed for two of the three.** `services/other` and
`packages/other` used to have no `CODEOWNERS` line and resolved through the `*`
fallback — invisible while every owner was `@qiuethan`, but the moment owners
diverge an unregistered service starts auto-requesting the fallback owner
instead of anyone who knows it. They now have bare `/services/` and `/packages/`
lines, placed **above** the specific ones so last-match-wins still gives
`services/llm` to its own owner.

`root` keeps resolving through `*`, and always will: it is not a directory, so
there is no pattern to write. "Every top-level file" is exactly what `*` means.

**A CODEOWNERS entry can be wrong in a way that is completely silent.** Name
someone who is not a collaborator, or who has read-only access, and GitHub never
requests them and never says why — the zone looks owned and reviews go nowhere.
[`codeowners-valid.yml`](../.github/workflows/codeowners-valid.yml) asks
GitHub's own parser on every change to the file and fails on exactly that.
`check-labels.mjs` cannot catch it: it sees a line per zone, not whether the
handle on that line can review.
