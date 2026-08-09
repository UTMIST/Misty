<!--
Branching rules (enforced by CI):
  - Branch off `staging`. Never commit directly to `main`.
  - PRs to `main` MUST come from `staging` — main-source-guard.yml rejects others.
  - Keep a PR inside one CODEOWNERS zone. pr-zone-check.yml warns (non-blocking)
    when a PR spans several. Multi-zone is sometimes right (a shared-library
    change, a protocol change touching both meeting and discord-bot) — just say
    so below rather than leaving it unexplained.
-->

## What this changes

<!-- One or two sentences. What is different after this merges? -->

## Why

<!-- The problem, or a link to the issue. "Closes #123" auto-closes it. -->

## Zone

<!-- Which area does this touch? Delete the rest. -->

`discord-bot` · `packages/auth` · `services/connectors` · `services/documentation-system` · `services/llm` · `services/meeting` · `services/team-tracking` · `services/verification` · `docs` · `scripts` · `.github`

<!-- If this spans more than one zone, explain why it can't be split: -->

## How to verify

<!--
The steps a reviewer runs to see it working. Be concrete — a curl with the
expected status, a command with the expected output, or the playground flow.
-->

```bash

```

## Checklist

- [ ] Branched off `staging` and targeting `staging` (or this is a deliberate `staging → main` promotion).
- [ ] Ran the service's test suite locally — the full one, including Postgres if the service has a database.
- [ ] `uv run ruff check .` and `uv run ruff format --check .` clean — CI gates both on every Python job.
- [ ] Read and followed the service's `docs/CONTRIBUTING.md` pre-push checklist.
- [ ] Docs updated in this PR where the change makes them wrong — `API.md` for a new endpoint, the README config table **and** `.env.example` for a new setting, `ARCHITECTURE.md` for a new trade-off, `DEPLOYMENT.md` for a new variable or scope.

## Deployment notes

<!--
Delete this section if none apply. Otherwise say exactly what an operator must
do, because merging to staging deploys immediately.
-->

- [ ] **Migration** — new Alembic revision. Head is now: `___`. `downgrade` verified.
- [ ] **New env var** — name(s), which services, and the value's source. Must be set *before* this merges or the deploy fails at boot.
- [ ] **New/rotated API key** — which service mints it, which consumer receives it, which `CONSUMER_KEYS` array it goes into.
- [ ] **Deploy order** — this must ship before/after another service. Which, and why.
- [ ] **Command registration** — a Discord command was added or changed; `npm run register:*` is a separate manual step.
- [ ] **Breaking change for a consumer** — what breaks, and what has to be updated alongside.

## Anything you're unsure about

<!--
Optional, and genuinely useful. Flag the part you'd most like a second opinion
on — it's a better use of review than a rubber stamp.
-->
