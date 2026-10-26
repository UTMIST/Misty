# Help and troubleshooting

Start with `/help` if you are unsure which command to use. For details on a
specific command, enter its name in the `command` field, for example
`/help command:doc`. Misty shows help based on your account's permissions.

If something is not working, find the matching situation below. You can
also use `/bug` to find the reporting link and any configured Discord
support contact.

## I cannot link my account

If Misty cannot find your email, check that you entered the address on your
UTMIST member record. Ask an exec or Misty admin to add or correct that
record if needed, then run `/link` again.

If the code expired or you made too many incorrect attempts, request a new
one. Finish `/link` with `/verify-code`; finish `/add-email` with
`/verify-email`. If Misty asks you to wait before requesting another code,
wait before trying again.

Once linking succeeds, use `/whoami` to confirm your account. The complete
walkthrough is in [Getting started](Getting-started.md).

## Misty is not answering my message

For a new question in a server, put `@Misty` at the start of your message.
For a follow-up, reply directly to Misty inside a thread it created and
leave the reply ping enabled. A reply to another person or a message with
the ping turned off does not trigger an answer.

You can also open a DM with Misty and ask there. Make sure your account is
linked. If Misty says you have reached your allowance, wait for the reset
time in its reply.

If ordinary conversation works but document answers or requested actions
do not, ask an admin whether those features are enabled for the bot.

## I cannot find a document

1. Try `/doc search` with a different title word or topic.
2. If it is a Google file, check that you can open it using the email on
   your Misty record. [Verify an additional email](Getting-started.md#add-another-email)
   if the file is shared with another address you own.
3. If the file was just added or edited, allow time for Misty to process it.
4. Ask a Misty admin to check the file if it still does not appear. Give
   them its link privately if the document is sensitive.

Misty needs access to the source and must be able to associate its sharing
with the intended members. Changing the file to “anyone with the link” is
not a fix for a missing result.

If a document works in a DM but not in a shared conversation, ask an admin
to check that channel's team settings. A thread uses its parent channel's
document settings.

## My team or Google access is missing

1. Run `/my-teams`. If the team is missing, ask a Misty admin to check the
   directory membership.
2. If the team is present, check the Google account you are using. Its
   address should be your primary or a verified additional Misty email.
3. Ask an admin to check the team's Google Group sync and ask the file
   owner to confirm the file is shared with that Group or your address.
4. If the file opens in Google but is missing from `/doc search`, use the
   [missing-document checks](#i-cannot-find-a-document) above.

Joining a Discord channel does not create a team membership or Google
share. The [Teams guide](Teams.md) explains each step; admins can use
[Check the Google Group](Managing-people-and-teams.md#check-the-google-group)
for recovery.

## An action did not finish

If a **Review privately** or **Confirm** button expired, submit a new
request. If you already confirmed, check the current state before asking
Misty to repeat the change. For example, inspect the team roster after a
membership change or the document list after adding a link.

A request with several actions can finish some before another fails. Read
the individual results so you only repeat the work that is still needed.

## I am having trouble with a recording

Use `/record status` first. Misty allows one recording per server, so you
may need to finish an existing recording before starting a new one.

To stop, join the voice channel Misty is recording and use `/record stop`.
If that channel is already empty, you can stop it from outside.

After stopping, check the text channel or thread where the recording
started. If the PDF arrived but the Google Doc is delayed, keep the PDF and
follow Misty's notice. Contact an admin with the meeting ID if it reports a
failed save or needs recovery. If you cannot open the Doc, check which Google
account you are using and ask an admin about its sharing.

For the complete process, see [Meetings](Meetings.md).

## Live Q&A did not answer my question

Live Q&A is available and remains **in progress**.

Use `/qna` while recording is active, in the exact text channel or thread
where it started. If Misty gives you a channel link, follow it. A parent
channel and its thread are different destinations.

Q&A uses recent transcribed speech. If something was just said, wait for
transcription before asking again. Check the time range in the answer; an
older discussion may be outside its window. Each question stands alone,
so include the topic rather than relying on a previous question.

If Misty reports that Q&A is busy or the answer timed out, wait briefly and
try again. See [Live meeting Q&A](Meetings.md#live-meeting-qa) for its limits.

## Report a problem

If these steps do not resolve the issue:

1. Run `/bug` and open the reporting link, or use
   [GitHub's issue form](https://github.com/UTMIST/Misty/issues/new/choose).
2. Describe what you were trying to do and what happened instead.
3. Include the command, error text, approximate time, and steps that
   reproduce it. Say whether you were in a DM, channel, thread, or meeting.
4. Include a meeting ID if the problem concerns a recording.

<details>
<summary>Example: a useful bug report</summary>

> **Title:** `/doc search` returns an unavailable message
>
> **What I tried:** In a team text channel, I ran
> `/doc search query:robotics onboarding`.
>
> **Expected:** A private list of matching documents.
>
> **Actual:** Misty replied, “The documentation service is temporarily
> unavailable. Please try again shortly.”
>
> **When:** Include your date, approximate time, and time zone.
>
> **Other details:** `/whoami` worked. The same search failed again after
> waiting briefly. This happened on the official Misty bot.

Use your actual observations. The example is fictional; include a document
link privately only if an admin needs it to investigate.

</details>

For the form's Area, choose **bot** or **not sure** if you do not know which
part is involved. Choose **production** for the official Misty bot, or
**staging** if you were deliberately using the test bot.

Keep verification codes and private document or meeting content out of a
public issue. Use a private conversation with a Misty admin when you need
to share sensitive details. If you cannot use GitHub, use the Discord
contact shown by `/bug` or ask a Misty admin to file the report for you.
