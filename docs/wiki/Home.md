# Misty user guide

Misty connects UTMIST's team directory, Google document access, and meeting
notes through Discord. Use it to find your teammates, work with team
documents, and return to the notes after a voice meeting.

This guide explains how to use **Misty**. You do not need to install
anything or work with the code to get started.

## Using Misty for the first time

Start by [linking your Discord account](Getting-started.md). This lets Misty
recognize your UTMIST profile and the teams you belong to. You will need
access to the email address on your member record. If you have not been
added yet, an exec or Misty admin can help.

Once you are linked, run `/whoami` to check your profile and `/my-teams` to
see your memberships. Read [Teams](Teams.md) next: it explains how your
roster, roles, and Google access fit together.

## For members

| Your task | Guide |
|---|---|
| Set up your account or add another email | [Getting started](Getting-started.md) |
| Find your teammates and understand team access | [Teams](Teams.md) |
| Find a document or add one for your team | [Documents](Documents.md) |
| Ask Misty a question or have a private conversation | [Talking to Misty](Talking-to-Misty.md) |
| Record a meeting and get the notes | [Meetings](Meetings.md) |
| Catch up on an active meeting | [Live meeting Q&A — in progress](Meetings.md#live-meeting-qa) |
| Resolve a problem or get help | [Help and troubleshooting](Help-and-troubleshooting.md) |

## For administrators

Start with [Administration](Administration.md) for the setup sequence and
who handles each part. Then choose the work you need to do:

| Your task | Guide |
|---|---|
| Onboard people, create teams, manage memberships, and check Google Groups | [Managing people and teams](Managing-people-and-teams.md) |
| Share folders, configure channels and meeting access, remove documents, and adjust allowances | [Managing documents and access](Managing-access-and-documents.md) |

## Beyond Discord

Discord is where you ask Misty to act and read its replies. Some actions
also affect your accounts and files elsewhere:

| Where | What happens there |
|---|---|
| Your email inbox | You receive codes to link Discord or verify an additional address. |
| Misty's directory | Your person record, team memberships, and roles are stored. |
| Google Groups | When enabled, Misty keeps each team's managed Group in sync with its active members' primary and verified emails. |
| Google Drive and Misty's document catalog | File owners share source files. Misty reads supported files, records their access, and processes text so you can search and ask questions. |
| Saved meeting records | Misty prepares a PDF for Discord and, when saving is enabled and succeeds, a Google Doc with minutes and the transcript. The saved Doc is also catalogued for authorized retrieval. |

A team membership, a Google share, and permission to read a Discord channel
each control a different part of the experience. [Teams](Teams.md) explains
those connections; [Documents](Documents.md) and [Meetings](Meetings.md)
walk through what you should see in each tool.

## Two ways to use Misty

**Use a command** when you know what you want to do. Type `/` in Discord and
choose a Misty command, such as `/my-teams`. Discord shows the fields you
need to fill in. For a list of commands available to you, use `/help`.

**Start a conversation** when you want to ask a question in your own words.
Begin a message with `@Misty`, or send Misty a direct message. For example,
you can ask `@Misty show the robotics roster`. Document answers and actions
through chat must be enabled by your admins; the guides explain how to use
them and what to do if they are unavailable.

Examples throughout the guide use a fictional robotics team. Replace the
sample team names, emails, and people with your own.

## Pick a first task

| Try this | What to expect |
|---|---|
| `/my-teams` | A private list of your current team memberships. |
| `/doc search query:robotics onboarding` | Private results with document titles and links. |
| `@Misty show the robotics roster` | A shared roster when actions through chat are enabled. |
| `/record start name:Robotics weekly` | Misty joins your voice channel and starts recording after the start checks succeed. Follow the [meeting setup steps](Meetings.md#before-you-start) first. |

The illustrated replies in this guide use fictional data and simplified
Discord layouts. Expand **Read the example as text** below an illustration
for the same information in a readable text format.

## If you get stuck

Use `/help command:record` to look up a particular command, replacing
`record` with the command you need. For a problem you cannot resolve, use
`/bug` to find the reporting link and any configured Discord support contact.
