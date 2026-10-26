# Teams

Your team membership connects your place in UTMIST with the documents you
use. Start here to find your teammates, understand your role, and check
which account to use for team files.

You need a [linked Misty account](Getting-started.md) for the commands on
this page. To create teams or change memberships, use the separate
[administrator guide](Administration.md).

## Understand your team membership

Misty keeps a directory of people and teams. You can belong to several
teams, with a different role in each. For example, Alex could be a lead in
Robotics and a member in Infrastructure.

| Term | What it means |
|---|---|
| Team name | The display name, such as **Robotics**. |
| Team slug | The short identifier used in commands, such as `robotics`. |
| Team role | Your responsibility in that team: `executive`, `director`, `lead`, or `member`. |
| Team admin | A flag recorded beside your membership. It does not by itself unlock Misty's admin commands. |
| Misty access level | Your overall `member`, `admin`, or `superuser` level, shown by `/whoami`. This controls access to admin commands. |

Discord roles and channel permissions are managed separately. Joining a
Discord channel does not add you to its Misty team. Where UTMIST uses parent
teams and subteams, membership in a subteam does not automatically add you
to the parent's Google Group.

## Find your teams

1. Run `/my-teams` in Discord.
2. Check the team names, slugs, and roles in the private reply.
3. If a team is missing or a role is wrong, ask a Misty admin to correct
   your membership. Include the team slug and your Discord account.

For example, a fictional member might see:

> **Misty — visible only to you**
>
> - **Robotics** (`robotics`) — lead — team admin
> - **Infrastructure** (`infrastructure`) — member

An empty list means Misty has no current team memberships for your account.
Linking your account establishes who you are; an admin adds your memberships
as part of [team onboarding](Managing-people-and-teams.md#add-a-member-to-a-team).

## Find your teammates

1. Run `/team list` to see active teams and their slugs.
2. Choose `/team roster` and select the team in the `team` field.
3. Send the command to see its members and their roles.

Team lists and rosters are **visible to the channel**. For example,
`/team roster team:robotics` might show:

> **Misty — sample shared reply**
>
> **Robotics** (`robotics`)
>
> - **Alex Chen** — lead — team admin
> - **Sam Patel** — member

Use `/team list active_only:false` to include inactive teams. To look up a
past roster, supply a date in `YYYY-MM-DD` format:

```text
/team roster team:robotics as_of:2026-09-01
```

## Use your team's Google files

When Google Groups is enabled, Misty maintains a Google Group for each
team. That Group is the bridge between the team's roster and Google files
shared with the team.

| Step | What happens | What you do |
|---|---|---|
| An admin adds you to the Misty team | Your directory membership is recorded. Google membership sync follows. | Confirm the team with `/my-teams`. |
| The Group sync succeeds | Your primary email and all your verified additional emails are included in the team's managed Group. | Sign in to Google with one of those accounts. |
| A file owner shares a file or folder with the Group | Group members receive the access the owner selected. | Open the shared link in Google Drive. |
| Misty reads and processes a supported shared file | Its contents can become available through Misty's document search, subject to access checks. | Use `/doc search`, or ask Misty in a DM. |

An admin's “added to team” confirmation establishes the directory change.
Google sync and document processing can finish later. If you cannot open a
team file, check the Google account you are signed in with, then ask an
admin to [check the Group](Managing-people-and-teams.md#check-the-google-group).

If you use another Google email, [add and verify it](Getting-started.md#add-another-email).
That also schedules updates to the managed Groups for your current teams.
A verified address identifies you to Misty; it does not create a Google
account or give access to files that have not been shared with you or your
Group.

<details>
<summary>Example: Sam joins Robotics and opens its onboarding folder</summary>

Sam links Misty with `sam@example.com` and verifies the Google address
`sam.drive@example.com`. An admin adds Sam to `robotics`.

The admin checks that the robotics Group has synced. The folder owner has
already shared the onboarding folder with that Group. Sam opens the folder
while signed in as `sam.drive@example.com`.

To find the same guide through Misty, the folder must also have been
[shared with Misty's reader](Documents.md#share-google-files-for-automatic-discovery)
and processed. Sam then tries `/doc search query:robotics onboarding`.
Opening a file in Drive and finding its contents through Misty are separate
checks.

</details>

## Understand team channels and meeting notes

Your own memberships determine your team relationships. A shared Discord
conversation has a separate setting: admins choose the teams whose
documents Misty may use in that channel. Everyone who can read the reply
can see the information it contains. Threads use their parent channel's
document settings.

For saved meetings, the **voice channel's** configured teams determine the
Google Groups used for the new Doc. The PDF goes to the **text channel or
thread where recording started**. These can have different audiences; see
[Meetings](Meetings.md#find-and-read-your-notes).

If you leave a team, an admin ends the membership and Misty updates its
managed Group when sync succeeds. Access through other teams or direct
Google shares can still remain. A departure also needs any separate
Discord access reviewed by the server's admins.

Next, [find your team's documents](Documents.md), or use
[Managing people and teams](Managing-people-and-teams.md) if you administer
the roster.
