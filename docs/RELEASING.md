# Misty releases

Misty has one platform release number in [`VERSION`](../VERSION). A release
covers the repository snapshot promoted through `staging → main`. Individual
services and scheduled runners can deploy separately; record their actual
commit SHAs and deployment IDs when verifying a rollout.

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

The [1.0.0 baseline](DEPLOYMENT-HISTORY.md#2026-10-23--100-stable-baseline-pr-319-completed-by-322)
is the completed October promotion. That historical commit predates `VERSION`.
Backfill `v1.0.0` at its recorded final production SHA (also given in the
publication instructions below), not today's branch tip. The history records
the intended mapping and acceptance limitations, not proof that a tag exists.

`VERSION` now prepares **`1.0.1`**, the first tracked patch after the baseline.
It collects the Groups, discovery and source-refresh warning fixes already
promoted in #327/#330, plus the hourly identity-cache fixes in #349. Earlier
untagged fix deployments retain their existing commit/PR identities; we do not
invent separate historical release numbers for them. Subsequent production
promotions each receive a fresh version.

## Prepare and verify a release

1. Choose the version and change `VERSION` through a PR into `staging`. Use
   the highest bump required by the changes since the last release. A revert
   is a new release too; never lower `VERSION` to roll back code.
2. Include release notes in the `staging → main` PR: features, fixes, breaking
   changes, migrations/recovery constraints, configuration and deployment
   order. Explicitly say when no migrations or new configuration are required.
3. Validate the exact staging candidate and require green promotion CI. If
   staging advances, validate the new candidate before merging. Follow the
   [deployment runbook](RAILWAY-DEPLOYMENT.md) for configuration, backups and
   stateful meeting/bot rollout precautions.
4. Merge `staging → main` through the existing reviewed flow. **Merging still
   triggers deployment.** Record the resulting main merge SHA; it differs
   from the staging SHA. Tagging or publishing a release is not a deploy step.
5. Verify production at that source revision, including service readiness,
   migrations, Discord registration and relevant smoke checks. Record the
   actual revision/deployment ID of scheduled runners as well: a code merge
   alone does not prove they updated. Keep evidence sanitized.
6. Publish the tag and GitHub Release using the exact verified main SHA below.
   If verification fails, retain the failure evidence and fix or revert via
   staging; do not publish the candidate as a verified release.

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
[migration-specific recovery guidance](DEPLOYMENT-HISTORY.md#redeploy-and-rollback).

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
