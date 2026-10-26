# Releases and the user wiki

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

## Prepare a release

1. Compare the proposed release with the previous release tag. Identify changes
   to commands, natural-language actions, permissions, sharing, meetings and
   required setup. Include user-visible fixes and limitations.
2. Open the normal `staging` → `main` promotion PR. Record the release scope,
   deployment/configuration prerequisites and verification results there. Use
   the [deployment runbook](RAILWAY-DEPLOYMENT.md) for the operational steps.
3. Prepare the affected wiki pages in a separate checkout, as described below.
   Include the page list and a reviewable diff or patch in the promotion PR.
   Record the wiki base revision so another maintainer can reproduce the diff.
4. Draft release notes around what users can do and any setup they need. Link
   to the relevant wiki pages and include known limitations. Keep live meeting
   Q&A marked **in progress** until that status is deliberately changed.
5. Verify the affected member and administrator journeys in the target setup.
   A passing offline suite does not establish live Google, AWS or Discord
   acceptance. Only describe features as available when their release
   configuration supports them.

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
4. Create the GitHub release and its tag for the exact released `main` revision.
   Publish the prepared user-facing notes with links to the affected wiki pages,
   then use that release URL in the announcement. Record the code and wiki
   revisions together so the published documentation can be traced to the release.

The wiki remains a living guide to the current release. Keep historical changes
in release notes and Git history rather than adding dated audit pages or version
copies of every guide. If a rollback changes what users can do, restore the
matching guidance and amend the release notes as part of the rollback.
