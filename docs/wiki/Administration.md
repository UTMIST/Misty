# Administration

Use this section to onboard people, organize teams, and make the right
documents available in Discord and Google Drive.

Misty admin commands require a linked account with `admin` or `superuser`
access. Check `/whoami` first. A team role, team-admin flag, or Discord
server role does not automatically grant this access.

## Choose an admin task

| You need to | Start here |
|---|---|
| Add a person and help them get started | [Add a new member](Managing-people-and-teams.md#add-a-new-member) |
| Create a team and its managed Google Group | [Create a team](Managing-people-and-teams.md#create-a-team) |
| Add or remove someone from a team | [Manage memberships](Managing-people-and-teams.md#add-a-member-to-a-team) |
| Check or recover Google Group membership | [Check the Google Group](Managing-people-and-teams.md#check-the-google-group) |
| Make a team folder available in Google and Misty | [Share team documents](Managing-access-and-documents.md#share-team-documents) |
| Choose which documents Misty can discuss in a channel | [Set up channel document answers](Managing-access-and-documents.md#set-up-document-answers-for-a-channel) |
| Set the audience for new saved meeting Docs | [Set up saved meeting access](Managing-access-and-documents.md#set-up-saved-meeting-access) |
| Remove a catalog entry or saved meeting | [Remove documents and meeting records](Managing-access-and-documents.md#remove-a-document-from-the-catalog) |
| Change the helper request allowance | [Adjust request allowances](Managing-access-and-documents.md#adjust-request-allowances) |

## Set up a team from start to finish

For a new team, work in this order:

1. Create the team and add its linked members in
   [Managing people and teams](Managing-people-and-teams.md).
2. Check that the managed Google Group is synced and copy its actual
   address from Misty's status reply.
3. Ask the folder owner to share the team's files with that Group and
   arrange [Misty's access to the files](Managing-access-and-documents.md#share-team-documents).
4. Configure the team's text channels for document answers and its voice
   channels for saved meeting access.
5. Ask a member to check `/my-teams`, open a team file in Google, and find
   it through `/doc search`. These checks confirm different parts of the setup.

Creating the Misty team sets up the directory entry and, when enabled,
starts Google Group provisioning. The team's Drive folders, Discord
channels, and Discord roles are set up separately by their owners.

## Know which admin can make the change

| Area | Who handles it |
|---|---|
| Misty people, teams, channel document settings, and request allowances | A linked Misty `admin` or `superuser`. |
| A file or folder's Google sharing | Its owner or someone allowed to manage its sharing. Misty admin status alone gives no Google ownership. |
| Google Group policy, authorization, or external-member failures | UTMIST's Google administrator or the managed Group's owner. |
| Discord roles and channel visibility | A Discord administrator with the relevant server permissions. |
| Integration activation, source-processing failures, or saved-record recovery | The Misty operator responsible for the deployment. |

Member-facing instructions live in [Teams](Teams.md), [Documents](Documents.md),
and [Meetings](Meetings.md). Send those pages to members who need to use a
feature; keep the setup and recovery work in this section.

For changes through conversation, follow
[Ask Misty to perform an action](Talking-to-Misty.md#ask-misty-to-perform-an-action)
and review the private preview before confirming. Read each result when
requesting several actions; some can succeed while another needs attention.
