# Managing people and teams

[Administration](Administration.md) · [Documents and access](Managing-access-and-documents.md)

Use this guide to take a person from their first member record to a team
with working Google access. These commands require Misty `admin` or
`superuser` access. Members can use the separate [Teams guide](Teams.md)
to check their own memberships.

## Add a new member

1. Get the person's preferred name and the email they will use to link
   their account.
2. Choose `/seed` and fill in `email` and `name`.
3. Choose the appropriate `level`, usually `member`, and send the command.
4. After Misty confirms the record was added, send the person the
   [Getting started guide](Getting-started.md).
5. Ask them to complete `/link` and `/verify-code`, then check `/whoami`.

```text
/seed email:alex@example.com name:Alex level:member
```

This creates a Misty directory record. The person receives the verification
email when they request `/link`. They still use their existing Discord and
Google accounts.

You can assign `member`, `admin`, or `superuser`, up to your own access
level. If the person already exists, `/seed` rejects the request; it does
not change an existing person's access level. Ask the Misty operator to
correct an existing record or change its access level through the directory.

## Create a team

1. Choose `/team create`.
2. Enter a short identifier in `slug`, such as `robotics`.
3. Enter the name members should see in `label`, such as `Robotics`.
4. Optionally add a `description`, then send the command.
5. Check `/team list` to confirm the team appears.

```text
/team create slug:robotics label:Robotics
```

When Google Groups is enabled, team creation also starts provisioning a
managed Group. Read the Google status in the creation reply: the team can
exist even while its Group is pending or failed. If Groups is unconfigured,
the team exists only in Misty's directory.

Use [Check the Google Group](#check-the-google-group) to confirm setup and
get the real Group email. A file owner then uses that email to
[share the team's files](Managing-access-and-documents.md#share-team-documents).
Arrange the team's Drive folder and Discord channels separately.

### Rename a team

```text
/team rename slug:robotics new_label:Robotics team
```

This changes the display name. Keep using the same slug in commands. An
already provisioned Group keeps its email address, so existing shares can
continue using it.

The directory can also represent parent teams and subteams. Ask the Misty
operator to configure that structure; the Discord create command does not
have a parent-team field. Parent Groups do not automatically include
subteam members. Add people to each team they actually need to belong to.

## Add a member to a team

The person must link their account before you use `/team add` for them.

1. Choose `/team add`.
2. Select the person's actual Discord account in `user` and the team's
   slug in `team`.
3. Choose their `role`: `executive`, `director`, `lead`, or `member`. The
   default is `member`.
4. Set `team_admin` if they should be recorded as a team administrator,
   then send the command. This flag does not grant Misty's overall admin
   access or Google Group ownership.
5. Check `/team roster`, and ask the person to check `/my-teams` privately.

```text
/team add user:@Alex team:robotics role:member
```

The confirmation means the membership was added to Misty's directory.
When Groups is enabled, synchronization then adds the active member's
primary email and **all verified additional emails** to the team's Group.
If they later verify another email, Misty schedules another update.

Google changes happen separately from the Discord reply. Check Group
status before treating onboarding as complete. If the person is already
on the team, `/team add` leaves their existing membership in place; ask the
operator to correct an existing role or team-admin flag.

<details>
<summary>Example: onboard Sam to the robotics team</summary>

1. An admin runs `/seed email:sam@example.com name:Sam Patel level:member`.
2. Sam completes `/link` and `/verify-code` using that email.
3. Sam verifies `sam.drive@example.com` with `/add-email` and `/verify-email`
   if that is the Google account used for team work.
4. The admin runs `/team add user:@Sam team:robotics role:member`, selecting
   Sam's actual Discord account.
5. Sam checks `/my-teams`. The admin checks
   `/team google-status team:robotics` and resolves any sync problem.
6. The folder owner confirms that the robotics Group has access to the
   onboarding folder. Sam opens it using the verified Google account.
7. After Misty has processed the shared folder, Sam tries
   `/doc search query:robotics onboarding`.

If Sam already has a member record, start with linking or checking the
existing link; do not create a second record.

</details>

## Check the Google Group

1. Run `/team google-status team:robotics`.
2. Read the Group email and status. Use the returned address when sharing
   files; do not guess the domain or create another Group with a similar name.
3. If a sync needs retrying, run `/team google-sync team:robotics`.
4. Check status again and follow any recovery instructions in the reply.

| Result | What it means and what to do |
|---|---|
| Synced | The last sync completed. Check the intended Google account and file sharing if access is still missing. |
| Provisioning or no mapping yet | Setup may still be running. Check again, then retry sync if necessary. |
| Not configured | Ask the Misty operator to enable the integration. The directory roster still works. |
| Failed | Read the error, resolve its cause, and retry. The directory change may already have succeeded. |
| External-member recovery needed | Ask UTMIST's Google administrator to check the authorization and organization policy described in the reply, then retry sync. |

Misty attempts the required external-member settings automatically. When
the reply includes a **Group settings** link, the Google administrator or
Group owner can use it for recovery. Creating a second Group or repeatedly
adding the same person to the roster will not fix an authorization problem.

Maintain ordinary Group membership through Misty's roster. A manual Google
addition that is absent from the directory can be removed by the next sync.
Google owners and managers are preserved; manage those roles separately
with the Google administrator.

## Remove a member from a team

1. Run `/team remove user:@Alex team:robotics`, selecting the person's
   actual Discord account.
2. Check `/team roster team:robotics` to confirm the current roster.
3. Check `/team google-status team:robotics` and resolve any failed sync.
4. If this is an access removal, ask the file owners and Discord admins
   to review the person's other access routes too.

This ends the membership while keeping the person's record and membership
history. Their other Misty team memberships remain in place.

When sync succeeds, Misty removes the departed member's ordinary managed
Group memberships for that team. Direct file shares, other Groups, Google
owner or manager roles, and Discord permissions can still grant access.
Review those separately when someone leaves UTMIST or should lose access
to the team's materials. Previously downloaded files also remain with
their recipients.

## Retire a team or correct a directory record

Ask the Misty operator to retire a team, deactivate a person, change an
existing role, or correct a primary email. These tasks do not have dedicated
Discord commands in this release.

A retired team remains in the directory's history. Its managed Google
Group and existing Drive shares also remain; synchronization removes
ordinary managed members while preserving Google owners and managers.
Plan any file ownership, sharing, and Discord cleanup with their owners.

Once the roster is correct, continue with
[Managing documents and access](Managing-access-and-documents.md).
