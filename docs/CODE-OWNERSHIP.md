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

| Zone | `pr-zone-check` pattern | CODEOWNERS line | Owner |
|---|---|---|---|
| `discord-bot` | `discord-bot/*` | `/discord-bot/` | @qiuethan |
| `packages/auth` | `packages/auth/*` | `/packages/auth/` | @qiuethan |
| `packages/other` | `packages/*` | *(none — falls to `*`)* | @qiuethan |
| `services/connectors` | `services/connectors/*` | `/services/connectors/` | @qiuethan |
| `services/documentation-system` | `services/documentation-system/*` | `/services/documentation-system/` | @qiuethan |
| `services/llm` | `services/llm/*` | `/services/llm/` | @qiuethan |
| `services/meeting` | `services/meeting/*` | `/services/meeting/` | @qiuethan |
| `services/team-tracking` | `services/team-tracking/*` | `/services/team-tracking/` | @qiuethan |
| `services/verification` | `services/verification/*` | `/services/verification/` | @qiuethan |
| `services/other` | `services/*` | *(none — falls to `*`)* | @qiuethan |
| `docs` | `docs/*` | `/docs/` | @qiuethan |
| `scripts` | `scripts/*` | `/scripts/` | @qiuethan |
| `.github` | `.github/*` | `/.github/` | @qiuethan |
| `root` | everything else | *(none — falls to `*`)* | @qiuethan |

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
| [`pr-zone-check.yml`](../.github/workflows/pr-zone-check.yml) | Warns when one PR touches more than one zone; writes the list to the job summary | No — ends in `exit 0` by design |
| [`PULL_REQUEST_TEMPLATE.md`](../.github/PULL_REQUEST_TEMPLATE.md) | Prompts the author to name the zone | No |
| [`ISSUE_TEMPLATE/`](../.github/ISSUE_TEMPLATE) | `feature_request` prompts for one zone; `epic` prompts for the several it spans | No |

Nothing here can block a merge today. `pr-zone-check` becomes enforcing by
changing its trailing `exit 0` to `exit 1` and adding the job as a required
status check in branch protection.

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

**The two mechanisms resolve in opposite directions.** `zone_for()` is a bash
`case` — **first** match wins, which is why the specific service paths must sit
above the `services/*` and `packages/*` catch-alls. CODEOWNERS is the reverse:
**last** matching pattern wins, and it is not cumulative. Only the last line a
file matches applies, so an owner added to a specific zone line replaces the
`*` fallback rather than adding to it.

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

Five files carry the list. Change them in the same PR or the next reader gets a
contradiction:

1. [`.github/CODEOWNERS`](../.github/CODEOWNERS) — the zone line and its owner.
2. [`.github/workflows/pr-zone-check.yml`](../.github/workflows/pr-zone-check.yml)
   — a `zone_for()` case **above** the `services/*` / `packages/*` catch-alls.
3. [`.github/PULL_REQUEST_TEMPLATE.md`](../.github/PULL_REQUEST_TEMPLATE.md) —
   the Zone list.
4. [`.github/ISSUE_TEMPLATE/feature_request.md`](../.github/ISSUE_TEMPLATE/feature_request.md)
   and [`epic.md`](../.github/ISSUE_TEMPLATE/epic.md) — the same list, twice.
5. This file's table.

If the new zone is a service, its CI job in
[`ci.yml`](../.github/workflows/ci.yml) belongs in that PR too — a service has
shipped to staging with no CI coverage before, and the zone list is not what
catches that.

---

## Deliberate gaps, and one that isn't

**The templates offer 12 of the 14 zones, on purpose.** They list `root` but not
`services/other` or `packages/other`. Those two are *transitional* buckets — a
PR landing in one means something was added that nobody registered, and the
right response is to give it a real zone
([above](#adding-or-renaming-a-zone)), not to pick the catch-all off a menu.
`root` is different: a permanent destination that never resolves into anything
else, and the one the most ordinary PRs in this repo belong to.

**The one real gap: five files hold the list and nothing checks they agree.**
`CODEOWNERS`, `pr-zone-check.yml`, the three templates, and this page. Both
drift incidents so far trace to that, and correcting the copies resets the clock
without changing the odds. A CI step that extracts the zones from `zone_for()`
and from `CODEOWNERS` and diffs the two would catch it mechanically.

**Latent, not yet biting:** `root`, `services/other`, and `packages/other` have
no `CODEOWNERS` line and resolve through the `*` fallback. Invisible while every
owner is `@qiuethan`. Once owners diverge, an unregistered service starts
auto-requesting the fallback owner instead of anyone who knows it.
