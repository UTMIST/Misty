---
name: Epic
about: A body of work too big for one issue — a container tracked through sub-issues
title: 'Epic: '
labels: epic
assignees: ''
---

Blocked by:

<!-- ^ List the issue numbers this depends on, on that ONE line above, like
     `#40, #42`. The label automation reads them from that line and keeps the
     `blocked` / `ready` labels in sync as those issues close and reopen.

     An epic's blockers are the work it *builds on*, not its own children —
     the decomposition below is not a blocker list. Delete the line entirely if
     nothing blocks this. -->

## Summary

<!-- Why this exists, in a paragraph. The most useful version names the gap:
     what is already on the board, what isn't covered by any of it, and what
     that missing piece is currently making look stuck. -->

## Model

<!-- The shape of the work — its stages or pieces, and how they relate. Name the
     constraint that governs it if there is one (an authorization boundary, an
     ordering requirement, a service that has to ship first). This section is
     what makes the decomposition below a consequence rather than a wish list. -->

## Decomposition (build order)

<!-- Ordered. Each line should be something one person can pick up and finish
     without holding the whole epic in their head. Promote each to its own
     sub-issue as it's picked up — the sub-issues are what the board tracks, and
     each of them should stay inside a single zone even though the epic doesn't. -->

- [ ] **** —
- [ ] **** —
- [ ] **** —

## What we can build on

<!-- The closed issues, existing patterns, and shipped services this reuses. Two
     jobs: it stops an epic from reading as more new work than it is, and it
     points whoever picks up the first sub-issue at the right prior art instead
     of a blank file. -->

## Not in this epic

<!-- The neighbouring work this gets confused with, and which issue owns it
     instead. This is the section that most reliably prevents the same work
     being built twice or counted twice. -->

## Zones touched

<!-- Epics routinely span several — that's much of what makes them epics. List
     them here so the scope is visible, then keep each sub-issue and each PR
     inside one, because pr-zone-check.yml is scoped to PRs, not to this. -->

`discord-bot` · `packages/auth` · `services/connectors` · `services/documentation-system` · `services/llm` · `services/meeting` · `services/team-tracking` · `services/verification` · `docs` · `scripts` · `.github`

## Open questions

<!-- Decisions genuinely not made yet. Filing with these open is fine and
     normal; starting the first sub-issue with the ones that affect it still
     open is not. -->
