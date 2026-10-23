# Saved meeting link delivery

After explicit or automatic stop, Misty posts the saved Google Doc link in the
exact text channel or thread where recording started. The message names the
meeting and start time, mentions the original requester, and identifies the
minutes and full transcript. Drive access stays personal: a Discord link grants
no permissions. Admin-only records say so. `save:false` keeps the existing PDF
flow. The [recording design](../../docs/MEETING-RECORDING.md) owns consent,
audience mapping and retention.

## Recovery

The bot consumes documentation-system's authenticated saved-reference endpoints;
it never creates a Doc, re-ingests content, or reruns transcription/minutes.
`DOC_API_KEY` needs `meetings:read` and `meetings:write` alongside its existing
scopes. Discord requires View Channel, Read Message History and Send Messages
(Send Messages in Threads in a thread) at the stored destination. A missing
thread, mismatched guild or denied fetch fails delivery without redirecting it.

On Discord readiness and every ten minutes, the bot polls saved records with
`delivery_status=pending` or `failed`. It reads each record again before posting
so removed, disabled and incomplete records are skipped. Google/catalog
`persistence_status` and Discord `delivery_status` remain independent. A delayed
or failed save never gets a saved-link announcement. Immediate failures produce
a channel notice where possible; durable delivery failure is visible through
`GET /meeting-records?delivery_status=failed&persistence_status=saved` and logs
carry identifiers/status only. Restore destination access or catalog key scopes,
then wait for the next poll or restart the bot to retry immediately. Keep the
PDF and meeting ID for admin recovery of a persistence failure.

Successful delivery PATCHes `posted` and `discord_message_id`. A retry with a
receipt edits that message without pinging the requester again. Only a confirmed
Discord message deletion allows replacing a known receipt. If send succeeded
but its acknowledgement was lost, the process remembers the receipt; after a
restart it searches the latest 100 channel messages for the bot's meeting ID
marker. A deterministic enforced nonce also deduplicates recent sends. If a
notification is older than that history window, has no saved receipt, and is
outside Discord's nonce window, a replacement can still occur. Do not run
multiple bot workers; this feature adds no distributed delivery queue or lock.

Link messages contain no summary, decisions, participant list or transcript;
previews are suppressed and only the requester can be mentioned. Stored
`text_channel_id` is the intended notice destination from the original hand-off,
including private threads; no retry promotes it to a parent channel. Team Drive
and catalog grants remain owned by #222. Removing already posted Discord links
or PDF attachments remains the admin-owned workflow in the recording design.

## Verification

Run `npm test`, `npm run lint` and `npm run format:check` in `discord-bot`.
The offline tests cover stop/auto-stop, separate save/delivery failures,
concurrent retry, acknowledgement failure, restart/history recovery, deleted
messages, unsafe references, exact thread placement and restricted mentions.

In the local playground choose **Meetings → Preview meeting delivery**. Pick
explicit stop, automatic stop, failed delivery then recovery, Discord failure,
pending save, failed save or admin-only. Scenarios use the real lifecycle and
delivery orchestration with in-memory fakes and show persistence and delivery
state separately. They make no audio, Discord, Google, directory or catalog
calls and are independent of the acting identity. Voice capture still requires
the real Discord adapter.

## Changed-file rationale for #223

| File | Why it is needed |
|---|---|
| `src/clients/meetingRecordClient.js` | Authenticated, bounded saved-reference reads and delivery receipts. |
| `src/meeting/meetingDelivery.js` | Independent delivery retry, concurrency control and startup/poll recovery. |
| `src/messages/meeting.js` | Shared pointer rendering and accurate persistence/delivery notices. |
| `src/adapters/discord/meetingPosts.js` | Exact-channel send/edit, safe mentions and duplicate recovery. |
| `src/meeting/meetingSurface.js` | Deliver after finalization independently of the PDF result. |
| `src/context.js`, `src/index.js` | Inject and start delivery only on the active Discord surface. |
| `src/web/meetingDeliveryPreview.js`, `src/web/server.js`, `src/web/public/app.js` | Offline scenario execution, validated endpoint and playground controls. |
| `test/meetingDelivery.test.js`, `test/meetingLinkPoster.test.js`, `test/meetingRecordClient.test.js` | Verify orchestration, Discord safety/recovery and authenticated transport. |
| `test/meetingDeliveryPreview.test.js`, `test/meetingSurface.test.js` | Verify playground scenarios and delivery/PDF failure independence. |
| `.env.example`, `README.md`, `docs/CONTRIBUTING.md` | Document required scopes, member behavior and local verification. |
| `docs/meeting-delivery.md` | Delivery operations, limitations, verification and file rationale. |
| Root `README.md`, `docs/MEETING-RECORDING.md`, `docs/RAILWAY-DEPLOYMENT.md` | Keep member instructions, design and deployment permissions aligned. |
| `services/documentation-system/docs/API.md`, `services/documentation-system/docs/DEPLOYMENT.md` | Link the implemented delivery workflow and clarify that automatic Discord removal remains outside #223. |
