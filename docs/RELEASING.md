# Misty releases and the user wiki

Misty has one platform release number in [`VERSION`](../VERSION). A release
covers the repository snapshot promoted through `staging → main`. Individual
services and scheduled runners can deploy separately; record their actual
commit SHAs and deployment IDs when verifying a rollout.

This guide is for maintainers shipping Misty. Developers and operators use the
documentation in this repository. Members and Misty administrators use the
[published user guide](https://github.com/UTMIST/Misty/wiki), which lives in the
separate `UTMIST/Misty.wiki` Git repository.

## Where documentation belongs

| Content | Location | When to update |
|---|---|---|
| API contracts, architecture, configuration, tests and recovery | This repository's root and service docs | In the implementation PR |
| Member/admin walkthroughs, command examples, screenshots and diagrams | The separate GitHub wiki repository | Prepare alongside the feature; publish with its release |
| What changed, setup needed and known limitations | [GitHub Releases](https://github.com/UTMIST/Misty/releases) | For each user-facing release |
| Test results, deployment observations and release decisions | Feature or promotion PR | While reviewing and verifying that change |

Keep repository docs current with the code on their branch. Keep the published
wiki aligned with the released behavior available to users, including any
activation requirements. A merge to `staging` does not publish wiki changes.
Link users to the wiki from the root README; keep user walkthroughs and their
assets in the wiki repository rather than duplicating them here.

## Version policy

Use [Semantic Versioning](https://semver.org/):

| Change | Example |
| --- | --- |
| Compatible bug fix, security fix, or maintenance change | `1.0.0 → 1.0.1` |
| Compatible feature or documented deprecation | `1.0.1 → 1.1.0` |
| Breaking supported API or Discord command behavior | `1.1.0 → 2.0.0` |

The compatibility contract is the documented HTTP/WebSocket interfaces and
Discord command behavior used by consumers. Internal refactors do not require
a major bump unless they break that contract. A major bump does not make a
breaking rollout safe: document the transition and support old/new consumers
during deployment where needed.

`VERSION` contains only `MAJOR.MINOR.PATCH`, without `v`, leading zeroes,
prerelease identifiers, or build metadata. Git tags use `vMAJOR.MINOR.PATCH`.
Ordinary feature PRs may leave `VERSION` unchanged; choose the next version
in a release-preparation PR into `staging` before promoting to `main`.
Treat staging builds as candidates identified by their exact SHA, even when
they carry the same version number as a release.

The gateway's `/v1` and each service's Alembic revisions remain independent.
The existing package manifests and OpenAPI metadata still use internal
`0.1.0` values; they are not the platform release identifier. Runtime version
diagnostics and automated release publication are separate follow-up work.

## Stable baseline and first patch

The retrospective **1.0.0** baseline is the completed October promotion:
[PR #319](https://github.com/UTMIST/Misty/pull/319), followed by the fixture
correction in [#322](https://github.com/UTMIST/Misty/pull/322). The rollout record
identifies `172aa5a9278639aeac060002985438ae2ba5bce3` as the final production
source revision. It predates `VERSION`. Backfill `v1.0.0` at that exact commit,
not today's branch tip. This mapping neither proves that a tag exists nor closes
the remaining acceptance work recorded in #319.

`VERSION` now prepares **`1.0.1`**, the first tracked patch after the baseline.
It collects the Groups, discovery and source-refresh warning fixes already
promoted in #327/#330, plus the hourly identity-cache fixes in #349. Earlier
untagged fix deployments retain their existing commit/PR identities; we do not
invent separate historical release numbers for them. Subsequent production
promotions each receive a fresh version.

## Prepare and verify a release

1. Compare the candidate with the previous release tag and choose the highest
   version bump required by its changes. Change `VERSION` through a PR into
   `staging`. A revert is a new release too; never lower `VERSION` to roll back.
2. Include release notes in the `staging` → `main` PR: features, fixes, breaking
   changes, migrations/recovery constraints, configuration and deployment
   order. Explicitly say when no migrations or new configuration are required.
3. Prepare the affected wiki pages in a separate checkout. Include their page
   list, base revision and a reviewable diff in the promotion PR. Draft notes
   around what users can do, required setup and known limitations; keep live
   meeting Q&A marked **in progress** until that status is deliberately changed.
4. Validate the exact staging candidate and require green promotion CI. If
   staging advances, validate the new candidate before merging. Follow the
   [deployment runbook](RAILWAY-DEPLOYMENT.md) for configuration, backups and
   stateful meeting/bot rollout precautions.
5. Merge `staging` → `main` through the reviewed flow. **Merging still triggers
   deployment.** Record the resulting main merge SHA; it differs from the staging
   SHA. Tagging or publishing a release is not a deploy step.
6. Verify production at that source revision: service readiness, migrations,
   Discord registration and the affected member/admin journeys. Record scheduled
   runners' actual revisions and deployment IDs too; a merge does not prove
   they updated. Offline tests do not establish live Google, AWS or Discord
   acceptance. Keep evidence sanitized and describe features as available only
   when their release configuration supports them.
7. Publish the reviewed wiki changes, tag and GitHub Release using the exact
   verified main SHA through the procedures below. If verification fails, retain
   the failure evidence and fix or revert via staging; do not publish the
   candidate as a verified release.

## Prepare wiki changes

Use a separate checkout so unreleased user guidance cannot become a second
documentation tree in the application repository:

```sh
# Run from the directory where you keep repositories.
git clone https://github.com/UTMIST/Misty.wiki.git Misty.wiki
cd Misty.wiki
git switch -c release-guide-updates
git rev-parse HEAD
```

For an existing checkout, inspect `git status` and preserve pending work before
fetching updates or switching branches. The wiki's default branch is independent
of the application's `main` and `staging`; inspect its remote HEAD rather than
assuming they use the same name. GitHub wiki edits have their own publication
workflow and are not deployed by the application's Railway configuration.

Edit the affected pages and assets in this checkout. Preserve the member path
(getting started → teams → documents, conversations and meetings) and the admin
path (people and teams → Google sharing → channel and meeting setup). Explain
what happens in Discord and in connected tools, including delayed sync or
processing. Use the bot's [command reference](../discord-bot/README.md#commands)
and service API docs to check names, options, permissions and failure behavior.

Keep page names and anchors stable where possible, and update `_Sidebar.md`
when navigation changes. Use full wiki page URLs for links between pages and
`https://raw.githubusercontent.com/wiki/UTMIST/Misty/assets/` for hosted assets.
Check that every link and asset resolves in the published wiki context.

Preserve accessible examples: Mermaid flows need text descriptions, images need
alt text, and illustrated replies need nearby text equivalents. Keep the existing
reply graphics at 420 pixels wide, linked to their full-resolution PNGs. Use
fictional accounts and content. Edit the SVG source and export its matching PNG;
on macOS, from the wiki checkout:

```sh
for visual in assets/*.svg; do
    sips -s format png "$visual" --out "${visual%.svg}.png"
done
```

On other platforms, use a vector image renderer. Inspect exports for clipping
and readability before publishing. Review new files as well as the tracked diff:

```sh
git status --short
git diff --check
git diff
```

Keep the draft local until release publication. Preserve any edits made directly
in the wiki since the checkout's base revision; fetch and reconcile those changes
before pushing. Never force-push over another maintainer's wiki work.

## Publish with the release

1. Merge the approved promotion and verify production using the deployment
   runbook. Check feature activation and dependencies before claiming availability.
2. Publish the reviewed wiki changes to its default branch. Record the resulting
   wiki commit in the release PR. A wiki push is public immediately, so publish
   the pages when their described behavior is available in the release.
3. Check Home, sidebar links, changed anchors, images, expanded examples and
   diagrams on desktop and mobile. Verify the support links and setup steps.
4. Follow [Tag and publish manually](#tag-and-publish-manually) to create the
   GitHub release and its tag for the exact verified `main` revision.
   Publish the prepared user-facing notes with links to the affected wiki pages,
   then use that release URL in the announcement. Record the code and wiki
   revisions together so the published documentation can be traced to the release.

The wiki remains a living guide to the current release. Keep historical changes
in release notes and Git history rather than adding dated audit pages or version
copies of every guide. If a rollback changes what users can do, restore the
matching guidance and amend the release notes as part of the rollback.

## Tag and publish manually

Run these commands only when performing the approved publication. Replace
`MAIN_MERGE_SHA` with the verified full main commit SHA; never use a moving
branch name as the tag target. Set `release_version` from that commit's
`VERSION`, rather than the current working tree:

```bash
git fetch origin main --tags
release_sha=MAIN_MERGE_SHA
git merge-base --is-ancestor "$release_sha" origin/main
git show "${release_sha}:VERSION"
```

Stop if the ancestry check fails. Confirm the displayed version and that its
tag is absent remotely before continuing. Prepare the reviewed notes in a
local file; include the promotion PR, full source SHA, rollout evidence and
remaining limitations.

```bash
release_version=$(git show "${release_sha}:VERSION")
git ls-remote --tags origin "refs/tags/v${release_version}"
git tag -a "v${release_version}" "$release_sha" -m "Misty ${release_version}"
git push origin "refs/tags/v${release_version}"
gh release create "v${release_version}" --repo UTMIST/Misty --verify-tag \
  --title "Misty ${release_version}" --notes-file /path/to/release-notes.md
```

For the one-time historical **1.0.0** publication, use
`release_sha=172aa5a9278639aeac060002985438ae2ba5bce3` and
`release_version=1.0.0` instead of reading its nonexistent `VERSION` file.
Use #319's completed-rollout evidence and preserve its outstanding acceptance
items in the notes. Add `--latest=false` to `gh release create` if a newer
release has already been published.

Never force-update a release tag. If a tag already exists, inspect its peeled
commit and resume only if it matches the intended SHA. A failed GitHub Release
creation after a successful tag push can be retried with `--verify-tag`;
recreating or moving the tag is unnecessary. The
[GitHub CLI reference](https://cli.github.com/manual/gh_release_create)
documents these publication options.

A tag identifies source code, not database state. Rollback follows the
[migration-specific recovery guidance](RAILWAY-DEPLOYMENT.md#redeploy-and-rollback).

## CI and local checks

[`release-version.yml`](../.github/workflows/release-version.yml) runs on every
PR/push to `staging` or `main`, and on staging `merge_group` candidates. It has
read-only permissions and never creates tags, releases or deployments.

- Every run validates the stable version format and rejects versions below
  the highest existing stable `v*` tag.
- PR/queue checks reject a version lower than their base's `VERSION`.
- A changed version must exceed existing release tags. Production promotion
  PRs must also exceed main's version, even if the last release is not yet
  tagged. Bases predating `VERSION` are supported for this initial rollout.
- Ordinary staging work can retain a released version until release prep, so
  publishing a tag does not block every subsequent feature PR.

Run from the repository root, with Node 22 and a full checkout:

```bash
git fetch origin --tags
make test-release
node scripts/check-release-version.mjs --base origin/main --promotion
```

`make test-release` runs offline regression checks using temporary Git
repositories, then validates the current working tree. `make check` and
`make test-full` include it. The promotion command is useful before opening
the release PR; it intentionally fails if main already has the same version.

After the workflow lands and passes, add the `release-version` job as a
required check on both branches through the normal repository settings
process. Workflow installation alone does not change branch protection. Keep
it free of path filters and retain the `merge_group` trigger; see the
[merge-queue rollout guide](DEVELOPMENT.md#merge-queue-on-staging).

CI checks the tags fetched at run time. Recheck them at publication, since
another operator could publish a tag after CI finishes.
