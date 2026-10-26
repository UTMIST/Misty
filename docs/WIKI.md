# Maintaining the Misty user wiki

The member and administrator guides are authored in [`wiki/`](wiki/Home.md). Keep
that directory as the source of truth, review changes through `staging`,
and publish the reviewed pages to the
[GitHub wiki](https://github.com/UTMIST/Misty/wiki). This file is for
maintainers and is not part of the published wiki.

The guide includes live meeting Q&A and labels it **in progress**. It describes implemented behavior without claiming that
production activation or live acceptance has been completed.

## Source pages and assets

| File | Purpose |
|---|---|
| [`wiki/Home.md`](wiki/Home.md) | Member and admin entry points, navigation, and an overview of what happens outside Discord. |
| [`wiki/Getting-started.md`](wiki/Getting-started.md) | Account linking, additional emails, identity, and access levels. |
| [`wiki/Teams.md`](wiki/Teams.md) | Member guide to team structure, rosters, roles, Google Groups, and file access. |
| [`wiki/Talking-to-Misty.md`](wiki/Talking-to-Misty.md) | Mentions, DMs, document answers, command actions, confirmation, and allowances. |
| [`wiki/Documents.md`](wiki/Documents.md) | Search, cataloging, Google sharing, access, and removal routes. |
| [`wiki/Meetings.md`](wiki/Meetings.md) | Recording, outputs, audiences, recovery, removal, and in-progress live Q&A. |
| [`wiki/Administration.md`](wiki/Administration.md) | Admin task navigation, setup sequence, and responsibilities across Misty, Google, and Discord. |
| [`wiki/Managing-people-and-teams.md`](wiki/Managing-people-and-teams.md) | Onboarding, team lifecycle, Group sync, and the effects of membership changes outside Discord. |
| [`wiki/Managing-access-and-documents.md`](wiki/Managing-access-and-documents.md) | Google sharing, channel and saved-meeting audiences, document removal, and request allowances. |
| [`wiki/Teams-and-administration.md`](wiki/Teams-and-administration.md) | Compatibility page preserving earlier wiki links and section anchors while directing readers to the split guides. |
| [`wiki/Help-and-troubleshooting.md`](wiki/Help-and-troubleshooting.md) | Common failure recovery and reporting paths. |
| [`wiki/_Sidebar.md`](wiki/_Sidebar.md) | Navigation on every published wiki page. |
| [`wiki/assets/profile-example.svg`](wiki/assets/profile-example.svg), [`profile-example.png`](wiki/assets/profile-example.png) | Editable profile-reply illustration and its embedded PNG export. |
| [`wiki/assets/confirmation-example.svg`](wiki/assets/confirmation-example.svg), [`confirmation-example.png`](wiki/assets/confirmation-example.png) | Editable private-action preview and its embedded PNG export. |
| [`wiki/assets/qna-example.svg`](wiki/assets/qna-example.svg), [`qna-example.png`](wiki/assets/qna-example.png) | Editable in-progress Q&A reply and its embedded PNG export. |

## Keep the guide accurate

Write for someone completing a task across Discord and the connected tools.
The member path is account setup → teams → documents, conversations, and
meetings. The administrator path is people and teams → Google sharing →
channel and meeting setup. Keep those paths separate in Home and the sidebar.
Introduce the task and anything readers need first, give the steps in order,
and explain both the immediate reply and any later effect in Google or the
catalog. Put access and recovery notes beside the relevant step. Keep
deployment work in the existing operator docs.

Use the [command registry](../discord-bot/src/commands/index.js) and command
definitions to check names, options, and access policies. Use the
[bot command reference](../discord-bot/README.md#commands),
[helper behavior](../discord-bot/README.md#helper-document-tools),
[Google sharing guide](../services/documentation-system/docs/DISCOVERY.md#members-sharing-with-misty),
and [meeting delivery guide](../discord-bot/docs/meeting-delivery.md) for the
user-visible workflows. Keep provider configuration and recovery procedures
in their existing operator docs.

For the workflows outside Discord, also check the directory's
[managed Google Groups contract](../services/team-tracking/docs/ARCHITECTURE.md#managed-google-groups),
the catalog's [source access reconciliation](../services/documentation-system/docs/ARCHITECTURE.md#source-acl-reconciliation),
and [saved-meeting sharing](MEETING-RECORDING.md#durable-records).
The command registry and implementation govern what members and admins can
actually do; do not turn an operator-only API capability into a slash command.

Review command examples and links whenever those behaviors change. In
particular, preserve these distinctions:

- `/doc search` is private; the `/doc list` slash command posts publicly.
- Shared helper answers use channel team access; DMs use personal access.
- The PDF audience is the originating Discord channel. Google Doc access
  is separate and can fall back to admins only.
- `/qna` is included but in progress, requires the recording's exact text
  channel, and currently has no linked-account or voice-membership check.
- `/doc remove` retires a catalog entry. Full meeting-record removal is a
  separate operator action with separate Discord cleanup.
- Directory membership, Google Group sync, and document processing are
  separate outcomes. A successful roster change does not prove Google access.
- Managed Groups include primary and verified additional emails. Sync
  preserves Google owners and managers; offboarding must account for them.
- Text-channel document scope and voice-channel saved-Doc audience are
  configured separately. `/channel-access` in the voice channel's own chat
  configures that channel; changing it does not reshare past records.
- Misty admin access, Google ownership, and Discord permissions are separate.

Do not add unverified discovery latency, retention, or availability promises.
Use the [deployment runbook](RAILWAY-DEPLOYMENT.md) for configuration prerequisites.
Before announcing a release, verify helper activation, Google integrations, the
intended member and admin journeys, and the `/bug` support contact in the target
environment. Record acceptance results in that release's promotion PR.

## Maintain examples and graphics

The guide uses [Mermaid diagrams](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/creating-diagrams)
for flows and [expandable examples](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/organizing-information-with-collapsed-sections)
for extra detail. Keep the walkthrough usable without expanding anything.
Each diagram has an accessible title and description, and each reply image
has alt text and a nearby text equivalent. Keep those in sync when editing.

The reply graphics are labeled illustrations with fictional accounts and
meetings, not screenshots or evidence of a live test. Their fields and
buttons follow `messages/people.js`, `messages/helperActions.js`,
`messages/qna.js`, and `adapters/discord/helperActions.js` in the bot.
Do not put real emails, verification codes, documents, or meeting content
into examples. Use placeholder URLs only where clearly identified.

Edit the SVG sources under `wiki/assets/`, then export matching PNGs for
the wiki. PNG is one of GitHub's [supported wiki image formats](https://docs.github.com/en/communities/documenting-your-project-with-wikis/editing-wiki-content#adding-images).
On macOS, from the repository root:

```sh
for visual in docs/wiki/assets/*.svg; do
    sips -s format png "$visual" --out "${visual%.svg}.png"
done
```

On other platforms, export at the SVG's declared size with a vector image
renderer. Inspect the PNGs for clipping and readable text before publishing.
The images are static; their illustrated buttons are not interactive.
Display reply images with an HTML `img` width of 420 pixels and link each
image to its full-resolution PNG. This keeps examples compact while letting
readers open the original for a closer look. Preserve their alt text and
expandable text equivalents.

## Publish the reviewed source

GitHub wikis have a separate Git repository. An initial page must exist
before it can be cloned; updates become visible when pushed to that wiki's
default branch. See
[GitHub's wiki editing instructions](https://docs.github.com/en/communities/documenting-your-project-with-wikis/adding-or-editing-wiki-pages).

1. Review and merge these source docs through `staging`, then include them
   in the production promotion. Select the exact reviewed release revision
   when preparing the publication checkout.
2. Enable the repository wiki and create an initial page if it does not
   exist. Inspect any existing wiki pages before replacing names.
3. From a checkout of the reviewed source, clone the wiki into a separate
   temporary directory:

   ```sh
   MISTY_WIKI_DIR="$(mktemp -d)/Misty.wiki"
   git clone https://github.com/UTMIST/Misty.wiki.git "$MISTY_WIKI_DIR"
   ```

4. Copy the source pages and assets. Convert relative `.md` links to wiki
   page URLs and image links to the wiki's raw assets. This preserves working
   links in the source repository and published wiki. Run from the source
   repository root:

   ```sh
   python3 - "$MISTY_WIKI_DIR" <<'PY'
   from pathlib import Path
   import shutil
   import sys

   source = Path("docs/wiki")
   destination = Path(sys.argv[1])
   pages = sorted(source.glob("*.md"))
   assert pages and (destination / ".git").exists(), "Check both checkout paths"
   base = "https://github.com/UTMIST/Misty/wiki/"
   assets_base = "https://raw.githubusercontent.com/wiki/UTMIST/Misty/assets/"
   shutil.copytree(source / "assets", destination / "assets", dirs_exist_ok=True)
   for page in pages:
       content = page.read_text()
       for target in pages:
           for delimiter in (")", "#"):
               content = content.replace(
                   f"]({target.name}{delimiter}",
                   f"]({base}{target.stem}{delimiter}",
               )
       content = content.replace("](assets/", f"]({assets_base}")
       for attribute in ("src", "href"):
           content = content.replace(
               f'{attribute}="assets/', f'{attribute}="{assets_base}'
           )
       (destination / page.name).write_text(content)
   PY
   git -C "$MISTY_WIKI_DIR" status --short
   git -C "$MISTY_WIKI_DIR" diff
   ```

5. Inspect new files as well as the tracked diff. Reconcile existing wiki
   content deliberately; the copy does not delete unrelated pages. Commit
   the reviewed changes in the wiki checkout and push its default branch
   when publication is authorized. No publishing happens from the main
   repository's normal deploy workflow.
6. Open the published Home page, each sidebar link, the live Q&A anchor,
   and the issue-report link. Check diagrams, reply images, their expandable
   text equivalents, tables, and command examples. Then use the wiki URL in
   the release announcement and server's pinned help message.

`_Sidebar.md` is GitHub's recognized sidebar file; see
[GitHub's sidebar instructions](https://docs.github.com/en/communities/documenting-your-project-with-wikis/creating-a-footer-or-sidebar-for-your-wiki).
Keep edits in `docs/wiki/` and republish them so browser-only wiki edits do
not drift from the reviewed source. Keep the guide current; earlier published
versions remain recoverable from Git history and release tags.
