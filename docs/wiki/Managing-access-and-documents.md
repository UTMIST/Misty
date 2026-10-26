# Managing documents and access

[Administration](Administration.md) · [People and teams](Managing-people-and-teams.md)

Use this guide after the team's roster and Google Group are set up. Misty
admin commands require `admin` or `superuser` access. Google and Discord
changes also require the appropriate permissions in those tools.

## Share team documents

A team folder needs two kinds of sharing: access for the people who use it,
and access for Misty to read and process it.

1. Run `/team google-status team:robotics` and confirm the Group is synced.
2. Copy the Group email from the reply.
3. Ask the file or folder owner to open **Share** in Google Drive and add
   that Group email. Choose the permission intended for the team, such as
   Viewer or Editor. See [Google's sharing instructions](https://support.google.com/drive/answer/2494822?hl=en).
4. For automatic discovery, ask the Misty operator for the reader account
   and confirm discovery is enabled. Follow
   [Share Google files for automatic discovery](Documents.md#share-google-files-for-automatic-discovery)
   to share the supported My Drive file or folder with that account.
5. Have a member open the file in Google using their primary or verified
   email. After processing, have them try `/doc search` as well.

The team's Group address and Misty's reader address serve different
purposes. The Group gives people access. The reader lets Misty fetch
supported files and inspect their sharing. The reader currently needs
Editor sharing for that inspection, but uses read-only access in its API
operations; it does not edit the source files or their permissions.

Discovery adds new files to the catalog. Processing makes their text
searchable, and periodic refresh updates existing content and sharing.
These steps can complete after the Drive share succeeds. If the file opens
in Google but remains absent from Misty, check the reader's access, the
member's verified email or synced Group, and then ask the operator to
investigate processing.

For a single link, a linked member can use
[`/doc add`](Documents.md#add-a-document-or-link). Cataloging a link does
not change its source permissions. A successful catalog entry can still
need content processing before questions about its contents work.

<details>
<summary>Example: make the Robotics onboarding folder usable end to end</summary>

The folder owner shares **Robotics onboarding** with the Group address
returned by Misty, using the permission intended for team members. The
owner also shares it with the operator-provided Misty reader account for
discovery.

Sam, a current robotics member, verifies the Google account used for work.
After Group sync, Sam checks the folder in Google Drive. After discovery
and processing, Sam checks `/doc search query:robotics onboarding`.

If Sam can open the folder but cannot find the guide through Misty, the
Google share worked. The next checks concern Misty's reader access and
document processing, rather than adding Sam to the roster again.

</details>

## Set up document answers for a channel

Choose teams whose information is appropriate for everyone who can read
the channel. Misty may include those teams' document content in shared
answers. Check the Discord channel's audience with a server admin first.

1. Open the text channel you want to configure.
2. Run `/channel-access show` to inspect its current teams.
3. To add a team while keeping the others, use
   `/channel-access append team:robotics`.
4. To replace the list, use `/channel-access set team:robotics`. Add
   `team2` through `team5` in the same command if needed.
5. Run `/channel-access show` again to check the result.

Both `set` and `append` accept up to five team options per command. To
remove all team document access from the channel, use `/channel-access clear`.
In a thread, these commands change the parent channel's settings.

These settings choose which team documents Misty can use in the shared
conversation. Discord still controls who can read the channel, and Google
still controls who can open the original files. Configure each where needed.
Document answers must also be enabled by the Misty operator.

<details>
<summary>Example: add a second team's documents to a channel</summary>

Suppose `#robotics-planning` already has `robotics` configured. To add
Infrastructure's documents as well, run:

```text
/channel-access append team:infrastructure
/channel-access show
```

The result should include both teams. Using
`/channel-access set team:infrastructure` would replace the list and leave
only Infrastructure. Everyone who can read this channel may see the
information in Misty's replies, so check that both teams' documents belong
in this conversation.

</details>

## Set up saved meeting access

A recording has two destinations to configure: the Discord destination
for the PDF and link, and the Google audience for the saved Doc.

1. Ask the Misty operator to confirm Google meeting saving is enabled.
2. Check that the intended teams' Google Groups are synced.
3. Open the **voice channel's own chat** and run `/channel-access show`.
4. Configure the intended teams there with `set` or `append`, then check
   again. Configuring a different text channel does not configure this
   voice channel.
5. When starting the meeting, choose a text channel or thread whose
   readers should receive the PDF. Follow the [recording guide](Meetings.md).
6. Read the start notice and, after stopping, check the PDF and saved Doc
   link. Have an intended member confirm that the Doc opens in Google.

Misty uses the voice channel's team setup for the new Doc and shares it
with those teams' managed Groups as **Commenters**. Members can read and
comment on the record. The operator manages its storage location and
ownership in Google Drive.

If the voice channel has no teams, or its Group access cannot be established,
Misty saves the Doc as **admin-only**. That refers to the configured Google
owners and administrators; a Misty `admin` access level alone does not
grant Drive access. The PDF still goes to the original Discord destination.

Changes to the channel's team setup apply to future saved recordings;
they do not automatically reshare old Docs. Group membership still matters
for existing Docs shared with that Group: new members may gain access to
past records, and departed members may lose that route to them. For a
meeting that needs a different audience, arrange the appropriate team and
channel setup before recording.

For a failed save or an admin-only record that needs attention, send the
meeting ID and intended audience to the operator. A missing PDF, a failed
Google save, and a missing Discord link are separate recovery tasks.

## Remove a document from the catalog

1. Run `/doc list` in an appropriate channel and find the entry. The list
   is public in that channel and shows document IDs to admins.
2. Copy the entry's ID.
3. Choose `/doc remove`, enter the ID, and send the command.
4. Check Misty's confirmation.

This retires the catalog entry. The original file and its Google sharing
remain in place. To revoke source access, ask the file owner to change the
Google share as well. Removing a catalog entry also does not erase earlier
Discord replies containing its information.

### Remove a saved meeting

Use the [meeting removal process](Meetings.md#request-removal). Collect the
meeting ID or Google Doc link and the Discord messages containing its PDF
and saved link. Ask the Misty operator to remove the durable meeting record
and associated stored content; arrange removal of the Discord copies with
someone who can manage those messages. `/doc remove` alone does not carry
out that full workflow.

## Adjust request allowances

1. Run `/helper-limits show` to check the current allowance.
2. Choose `/helper-limits set` and enter the intended `max_requests`,
   `window_seconds`, or both.
3. Send the command and check the reply.

For example, this sets an allowance of ten requests per one-hour window:

```text
/helper-limits set max_requests:10 window_seconds:3600
```

Use positive whole numbers. Changes apply immediately and retain each
person's existing usage. They return to the configured startup values
after a bot restart; ask the operator to make a lasting change.

For supported changes through chat, follow
[Ask Misty to perform an action](Talking-to-Misty.md#ask-misty-to-perform-an-action).
