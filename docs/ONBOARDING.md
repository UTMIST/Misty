# Joining the team

Welcome! This page covers the people side of contributing: what you're
responsible for, how work gets assigned, what a pull request should look
like, and what we expect from you week to week.

It deliberately skips anything technical. That lives in two other docs:

- [`DEVELOPMENT.md`](DEVELOPMENT.md) walks you from a fresh clone to running
  things locally to your first merged change.
- [`ACCESS.md`](ACCESS.md) covers credentials: what you need (almost nothing)
  and who to ask for the rest.

So if your question is "how do I run this?" or "where do I get this key?",
those pages have the answer, not this one.

---

## Your first week

Work through these in order. Only steps 1 and 4 need anyone's permission.

1. **Get repo write access.** Ask @qiuethan.
2. **Read the [README](../README.md)** top to bottom, then skim
   [`ARCHITECTURE.md`](ARCHITECTURE.md). Don't worry about retaining all of
   it. The goal is knowing what exists so you can find it again later.
3. **Work through [`DEVELOPMENT.md`](DEVELOPMENT.md)** until `make install`
   and `make check` pass on your machine. This proves your environment works
   before any real task depends on it.
4. **Tell @qiuethan which zones interest you.** The
   [canonical zone list](CODE-OWNERSHIP.md#the-canonical-list) is the menu.
   Your interests genuinely matter here; the final call just also has to
   balance what the org needs covered.
5. **Get your zone assignment** and read your zone's own docs, meaning the
   service README and its `docs/CONTRIBUTING.md`. As the zone's reviewer,
   those are the documents you'll be holding other people to.
6. **Claim a `ready` issue** whose work lands in your zone, by assigning
   yourself to it (see [Getting work](#getting-work) for what the labels
   mean).
7. **Get your first PR merged to `staging`.** Small is ideal. The point of a
   first PR is to walk the whole loop once (branch, review, merge,
   auto-deploy to staging), not to be impressive.

---

## Zones: what you're responsible for

Every dev owns one or more **zones**, the directory buckets defined in
[`CODE-OWNERSHIP.md`](CODE-OWNERSHIP.md). @qiuethan assigns them (see step 4
above), and the assignment is recorded in
[`.github/CODEOWNERS`](../.github/CODEOWNERS).

Owning a zone means three things:

1. **You're its reviewer.** GitHub automatically requests your review on
   every PR that touches it, and the
   [turnaround expectation](#pull-requests) below applies to you.
2. **You triage the issues headed its way.** Issues aren't labeled by zone
   (they carry `area/*` labels instead), but most issues are clearly about
   some part of the code, and if that part is yours, the issue should hear
   from you. A label, a question, or a "won't do" all count. Silence
   doesn't.
3. **You keep its docs honest.** The service README, `docs/CONTRIBUTING.md`,
   `API.md`, and `.env.example` are part of the zone. If a PR would make them
   wrong, that's worth catching in review, and if you notice drift yourself,
   it's yours to fix.

Think of ownership as responsibility, not territory. Anyone can open a PR
against your zone. Owning it just means you're the one making sure what
merges is right.

---

## Getting work

**The board is self-serve.** Find a `ready` issue, assign yourself, get to
work. You don't need to ask first.

Two conventions on top of that:

- **Default to work that lands in your own zones.** Issues aren't labeled by
  zone, so this is a judgment call about where the change will live, not a
  label to filter on. Picking up work in someone else's code is fine now and
  then. Just coordinate with that zone's owner on the issue before you
  start, so two people don't build the same thing.
- **High-priority issues get assigned directly.** If @qiuethan assigns you an
  issue, it jumps ahead of whatever you picked yourself.

### How to read an issue

Issues carry labels on three independent axes:

| Axis | Labels | Meaning |
|---|---|---|
| Kind | `type/bug`, `type/feature`, `epic` | What sort of work it is. An `epic` is a container tracked through sub-issues, so claim the sub-issues, not the epic itself. |
| Knowledge | `area/*` (often two or three) | Which parts of the system are involved, and so what you'd need to know to pick it up. |
| State | `blocked` / `ready` | Whether it can be started right now. |

**`ready` means up for grabs:** no open blockers and nobody assigned. The
state labels are automated, not applied by hand. Each issue form has a
**"Blocked by"** field (`#40, #42`), and a workflow keeps the labels in sync:
closing the last blocker flips an issue from `blocked` to `ready`, reopening
a blocker re-blocks it, and assigning yourself removes `ready` because the
issue just left the queue. That's why claiming means *assigning yourself*,
not just leaving a comment.

### Filing an issue

Use the [issue forms](https://github.com/UTMIST/Misty/issues/new/choose).
Their **Area** dropdown applies the `area/*` labels for you, and the
**Blocked by** field feeds the automation above. Blank issues are allowed
too, on purpose: "just write it down" beats template compliance. They simply
arrive with no labels until someone adds them by hand.

### Zone vs. area (they're different axes)

These two are easy to conflate, and they're kept separate on purpose:

- A **zone** applies to a **PR**. It says which directory bucket the change
  lives in, and therefore *who reviews it*. Every file has exactly one zone,
  and a PR should stay inside one.
- An **area** applies to an **issue**. It says which parts of the system are
  involved, and therefore *what you need to know* to pick it up. Most issues
  carry two or three. One `area/service` issue can span six zones.

The full rationale, and both canonical lists, live in
[`CODE-OWNERSHIP.md`](CODE-OWNERSHIP.md#zones-are-for-prs-areas-are-for-issues).

---

## Pull requests

The mechanics (branch naming, CI gates, the template) are enforced by the
repo itself. These are the human norms on top.

**Before requesting review**, a PR should have:

- `make check` passing locally, plus the service's full test suite
  (`make test-full` territory if the service has a database).
- The [PR template](../.github/PULL_REQUEST_TEMPLATE.md) actually filled in,
  especially "How to verify". Your reviewer should be able to see the change
  working without reverse-engineering your intent.
- One zone. Multi-zone PRs are sometimes the right call, but say why in the
  template rather than leaving it unexplained.

**Who reviews:** the zone's owner, via CODEOWNERS. Cross-zone PRs and
catch-all-zone PRs go to @qiuethan.

**Turnaround is 48 hours, both directions.** Reviewers respond to a
review-ready PR within 48 hours, and authors respond to review feedback
within 48 hours. "Respond" can just be "I need until Friday". The point is
that nobody is left wondering. If your reviewer blows past the window, ping
them on the PR, and if they're still silent, ask @qiuethan to reassign.

**Everything lands on `staging` first.** Branch off `staging` and PR into
`staging`. A merge auto-deploys to the staging environment. Production only
ever changes through a `staging → main` promotion PR, and CI rejects
anything else. Never commit to `main` directly.

---

## AI-assisted development

Using AI tools (Claude Code, Copilot, and friends) here is normal and
encouraged. The repo is deliberately structured to work well with them.

Two rules:

1. **Your agent follows [`AGENTS.md`](../AGENTS.md).** It's the compressed
   contract of this codebase's invariants, written specifically for coding
   agents. Point your tool at it.
2. **You own every line you submit.** "The AI wrote it" is not an answer in
   review. If you can't explain what a change does and why it's safe against
   the invariants in `AGENTS.md`, it isn't ready to submit.

**Missing a tool you want?** If there's an AI tool that would help you work
and you don't have access to it (a Claude subscription, an API key, an IDE
integration), tell @qiuethan. He'll see what he can do about getting you one.

---

## Time commitment

- **About 5 hours a week** is the baseline for staying an active
  contributor. This is a student org and nobody is counting hours, but zone
  ownership only works if owners are actually around.
- **If you go quiet for 2 weeks without a heads-up**, your zones get
  reassigned so the org isn't blocked. No hard feelings, and it's fully
  reversible: come back and you can take a zone again.
- **A heads-up beats disappearing.** Exam season, internships, life in
  general: all completely expected. A one-line "I'm out until March" to
  @qiuethan keeps your zones yours, held for you instead of reassigned.
