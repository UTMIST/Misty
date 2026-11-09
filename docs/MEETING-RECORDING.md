# Meeting Recording — Architecture

How the `/record` feature is split across a Discord **voice surface** (in the bot) and a stateful **`meeting` service**, and *why* the boundary sits where it does. Read this before changing either side — the two halves share a wire contract that must stay in lock-step.

Feature owner: Misty #92. Related: [platform ARCHITECTURE](ARCHITECTURE.md), [`services/meeting`](../services/meeting), [`discord-bot`](../discord-bot).

## What it does

A **linked** member (identity resolved via the directory — see "Authorization" below) runs `/record start` in a Discord voice channel, optionally supplying a name up to 100 characters. Before a recording is active, the first human to enter an empty voice channel receives a direct message prompting them to start one. The bot joins, and as people talk it streams their audio to the `meeting` service, which transcribes each speaker live into a rolling transcript. Recording ends on `/record stop` **or automatically when everyone leaves the voice channel** (the bot ends the meeting once no non-bot member remains). On stop the service turns the transcript into minutes (via the `llm` service), renders a PDF, and hands it back; the bot posts the **PDF** to the text channel, @-mentioning whoever started the recording. A supplied name becomes the PDF title and is sanitized into the attachment filename as `<name>_<YYYY-MM-DD_HHMM>.pdf` in America/Toronto; unnamed recordings use `meeting_<YYYY-MM-DD_HHMM>.pdf`, based on the meeting start time. For saved recordings, the finalized transcript and minutes are handed to documentation-system before cleanup, then persisted as one native Google Doc and cataloged. Live sessions and unaccepted hand-offs remain in memory. The bot posts the saved Doc link in the originating text channel or thread and recovers delayed deliveries automatically (#223); recipients still need their own Drive access. Audio is never persisted. See "Durable records" below.

## The boundary, and the one constraint that forces it

There is exactly one hard constraint, and it determines the whole design:

> **Voice capture is physically bound to the bot.** `@discordjs/voice` joins and receives voice through the bot's own Discord gateway connection (`guild.voiceAdapterCreator`). Nothing outside the bot process can receive that audio.

Everything *downstream* of capture — transcription, minutes, PDF — is ordinary stateful work with no Discord dependency. So the split is:

| | **`discord-bot`** — the *voice surface* | **`meeting` service** — stateful processing |
|---|---|---|
| **Owns** | Voice receive, the `/record` lifecycle, streaming audio up, posting results | Live transcription, the rolling transcript, minutes, PDF |
| **Must not** | transcribe, summarize, or render | touch Discord |
| **Deps** | `@discordjs/voice`, `libsodium-wrappers` (RTP decrypt), `ws` | `amazon-transcribe` (streaming), PyAV (Opus decode), `fpdf2`, `httpx`, `platform_auth` |

The bot deliberately carries **no processing dependencies** (no `@aws-sdk`, no `pdfkit`) — it forwards Opus and posts what the service returns.

## Why the `meeting` service is *stateful*

**Live transcription requires the same process to own a meeting's rolling transcript
and open streams across requests.** The service keeps an **in-memory registry of
active meeting sessions**, each holding one Transcribe stream per speaker and the
growing transcript (see "Live" below). The bot forwards audio; the meeting service
processes it. Audio chunks go straight to AWS and are dropped.

Other services have different state requirements: the directory, catalog,
verification service and gateway store durable records in their own databases;
llm and connectors are provider adapters without a local database. Gateway also
has process-local rate limits and [requires one replica/worker](../services/gateway/docs/DEPLOYMENT.md).

Live state is **ephemeral**: sessions and audio streams stay in memory. At stop, the optional record context enables a durable text-only hand-off to documentation-system before session cleanup. The meeting service never writes to disk. Abrupt disconnect recovery retains its existing grace period.

## Data flow

```text
Discord voice  ──Opus──▶  bot recorder ──sendFrame──▶  meetingClient (WS) ══▶  meeting service
                                                                                  │
                                                     per-speaker AWS Transcribe (persistent streams)
                                                                                  │
   /record stop ──POST /stop──────────────────────────────────────────────────▶  finalize:
                                                                    transcript → llm service → minutes
                                                                    → fpdf2 PDF
   channel.send(@requester + PDF) ◀──────── {pdf_b64, transcript, minutes} ◀──
```

**Live:** the bot's recorder taps each speaker's raw Opus packets and forwards them (untouched, no decode) over one WebSocket. The service decodes/resamples each speaker's audio and pushes it into that speaker's **persistent AWS Transcribe stream**, held open for the whole meeting. Audio is sent once and never replayed, so `GET /transcript` is a free read of what has finalized so far — it makes no AWS call and costs nothing to poll. `GET /meetings/{id}/transcript` exposes the transcript at any time — the same finalized-speech source used by the live `/qna` command.

**Stop:** `POST /meetings/{id}/stop` closes each speaker's Transcribe stream (concurrently, so latency is one flush and not N), assembles the transcript, calls the `llm` service for minutes, renders the PDF, hands off the optional durable record, returns the PDF base64-encoded, and discards the session.

**Timeline correctness:** each forwarded frame carries `ts_ms` = milliseconds since the meeting started. AWS Transcribe reports word times relative to *each speaker's own* stream, and a speaker's stream carries only the frames they actually spoke — silence is never sent. So the service records an **anchor** whenever a frame arrives later than the audio already streamed accounts for, and maps word times through the nearest preceding anchor. Anchoring on the first `ts_ms` alone is not enough: it fixes only the speaker's first word, leaving cross-speaker order wrong past the opening minute and collapsing each speaker into one segment (the gap rule never sees a gap). `ts_ms` must always be sent and honored.

## Durable records

Google Docs is the durable destination for completed meeting text. See the service
[API](../services/documentation-system/docs/API.md#meeting-records) and
[rollout runbook](RAILWAY-DEPLOYMENT.md#meeting-record-persistence).
Meeting removal uses an internal atomic catalog purge with restoration guards.
There is no public generic catalog-purge endpoint; use the meeting-record removal
API for saved meetings, and distinguish it from soft-retiring an ordinary document.

### Artifact

**One native Google Doc per completed meeting.** Sections, in order:

1. Header — meeting title, originating text channel, voice channel, start/end
   time (America/Toronto), the requester, and the teams the record is shared
   with.
2. Summary, Decisions, Action items — the existing `Minutes` fields.
3. Full transcript — the existing `assemble_transcript` lines
   (`[mm:ss] Speaker: text`), preserving speaker labels, timestamps, and order.
4. Footer — "Generated by Misty on <date>. Meeting ID <uuid>. To request
   removal, contact a Misty admin with this link."

Both explicit and automatic stop use the same path. Audio is never persisted or
written to disk. Reuse the `StopResponse` fields; no second LLM call and no PDF
parsing.

**PDF: stored only in Discord.** The PDF attachment is an immediate convenience
copy and the fallback if Google persistence
fails. Drive stores only the Google Doc. Anyone who needs a PDF later exports
the Doc from Google; Misty does not regenerate one.

### Identity

**`meeting_id` is the bot's session UUID.** The bot already generates it at
`/record start` (`meetingSurface`, `randomUUID`). It is the idempotency key for
every step and maps 1:1 to:

| Key | Where it lives |
|---|---|
| `meeting_id` (UUID) | `meeting_records.meeting_id` (PK) in documentation-system |
| Google document ID | `meeting_records.google_document_id`, and the Doc's Drive `appProperties.misty_meeting_id` |
| Canonical URL | `https://docs.google.com/document/d/{document_id}/edit` |
| Catalog entity | `meeting_records.doc_id` → `docs.id` |

Titles and folder names are presentation only. The Doc's real URL goes through
the normal catalog ingest; the UUID is never presented as a fetchable URL.

**One canonical Doc; late duplicates are reconciled.**
A Drive search followed by a create is not atomic — two overlapping attempts
can both find nothing, and a timed-out create can still be in flight when a
retry searches. A lease coordinates local workers; its expiry does not cancel
Google's request or prove that it finished. documentation-system owns the
canonical ID and cleanup of any extra files:

1. **One attempt at a time.** Before calling Google, documentation-system
   claims the record in one conditional update: `persistence_status` moves to
   `creating`, with a fresh `create_attempt_id` and `create_lease_until`
   (now + `MEETING_DOC_CREATE_LEASE_S`, default 10 min). The update
   succeeds only while the record has no canonical Doc, no removal marker,
   and no unexpired lease. `failed` records require explicit admin retry;
   a concurrent `PUT`, inline attempt, or `retry-due` run skips the record.
2. **Every create is tagged.** The Doc is created with
   `appProperties: {misty_meeting_id, misty_attempt_id}`.
3. **Accept only a current result.** Success stores the Google document ID and
   moves to `doc_created` in one conditional update: the attempt ID must still
   match, the lease must be unexpired, the status must still be `creating` or
   `create_uncertain`, `google_document_id` must be null, and removal must not
   have started. Updates reporting a create failure use the same guard, so a
   late failure cannot undo a newer success. A rejected completion leaves the
   Doc unshared for reconciliation; that worker must not adopt it,
   share it, catalog it, or clear the pending payload. A definite failure
   (Google 4xx) releases the current lease for a later retry.
4. **Uncertain outcome.** On a timeout, connection reset, or 5xx, the record
   moves to `create_uncertain` and **no new create is allowed** until it is
   reconciled.
5. **Reconcile before creating again.** After lease expiry, the next worker
   claims a new lease and searches with the writer identity for every file
   tagged `misty_meeting_id`, including trashed files and all result pages.
   Search by tag across the writer's accessible files, not just the original
   folder. If no canonical ID exists, adopt the oldest non-trashed match using
   the guarded update above; trashed-only matches require admin investigation.
   An empty search permits a new create under the current lease, but does
   **not** settle the earlier request's outcome.
6. **Never replace the canonical ID.** Once stored, `google_document_id` is
   immutable, including when an older or trashed duplicate appears later.
   Only the stored canonical Doc may be shared or cataloged; re-read the
   record and check that removal has not started before either operation.
   Losing workers leave cleanup to reconciliation, which reads the committed
   canonical ID before permanently deleting every other matching file.
   Concurrent reconciliation cannot elect a second canonical ID.

**Reconciliation continues after saving.** `retry-due` also scans records with
a due `next_reconcile_at`, including `saved`, `failed`, `inaccessible`,
`removing`, and `removed` records. Schedule the first scan when issuing a
create, and keep rescheduling it hourly after an empty search or successful
cleanup. A saved record keeps its canonical ID and deletes only duplicates;
a failed record with no canonical ID deletes late files without adopting
them. A removal marker requires deleting **all** matching files. Reconciliation
never creates a Doc or resets a terminal state on its own. A scan claims only
the attempt ID and lease, preserving the status, canonical ID, and removal
marker. Process due records oldest-first in bounded batches and persist their
next scan time so every record gets another turn. Cleanup failures are surfaced
to admins and retried independently of the creation attempt limit. Retain
content-free removal tombstones for these scans; neither lease expiry nor an
empty scan is grounds to stop checking for late files.

Docs are created **unshared**. An uncertain create can leave a temporary extra
copy visible only to the writer/Drive admins; it is never shared with members
or cataloged. Reconciliation deletes it once it becomes visible to the writer.
The guarantee is one stable shared/cataloged Doc and eventual duplicate
cleanup, not exactly-once Google file creation after an ambiguous response.

**No overwriting human edits.** The Doc is created *with* its content in one
Drive `files.create` call (HTML upload converted to a Google Doc), so there is
no "created but empty" state. Once a Doc exists for a `meeting_id`, retries
never write its body again.

### Hand-off and retry (closes the `/stop` gap)

With record context, `/stop` attempts to hand off finalized text before clearing
the live session. The transcript becomes durable only when documentation-system
accepts it; an unavailable catalog leaves an in-memory retry payload instead:

```text
bot ──POST /meetings/{id}/stop {title, record context}──▶ meeting
                                   finalize transcript + minutes + PDF
                                   PUT /meeting-records/{id} ──▶ documentation-system
                                                                 (Postgres insert, status=pending)
                                   ◀── 202 accepted
      ◀── {transcript, minutes, pdf_b64, record:{status, doc_url?}}
      discard session
documentation-system ── create Doc ──▶ connectors ──▶ Google Drive
                     ── ingest URL, set metadata ──▶ (own catalog)
```

1. **The bot sends record context with `/stop`.** `StopRequest` accepts optional
   `record` context: `guild_id`, `text_channel_id`, `voice_channel_id`,
   `requester_discord_id`, `started_at`, and `participants`
   (`[{discord_id, display_name}]` — every speaker the session saw). Callers
   without it receive the transcript, minutes and PDF without durable storage.
2. **`meeting` hands off before discarding.** After finalizing, it calls
   documentation-system `PUT /meeting-records/{meeting_id}` with the context,
   `ended_at`, the `Minutes` fields, and the assembled transcript. This is a
   durable acceptance before Google creation begins. After the hand-off attempt,
   `meeting` clears the live session. `StopResponse.record` reports the outcome
   as `{status, doc_url}`; only a catalog acknowledgement establishes durability.
3. **If documentation-system is unreachable,** `meeting` returns the normal
   minutes/PDF with `record.status = "handoff_pending"`, holds the finalized
   payload in memory for `RECORD_HANDOFF_HOLD_S` (default 30 min), and retries
   with backoff. The bot posts the PDF and says the Google Doc save is
   delayed. If the hold expires, `meeting` logs `meeting.record_handoff_failed`
   (IDs only) and the PDF in Discord is the surviving copy — never silent.
4. **documentation-system owns everything after acceptance.** A background
   attempt immediately tries the Google + catalog steps once (bounded time). Whatever is left is
   finished by `POST /meeting-records/retry-due`, called by the same hourly
   Railway cron that already drives `POST /docs/source-access/refresh-due`. No
   new queue or scheduler.

**State machine** (`meeting_records.persistence_status`):

`pending` → `creating` (leased) → `doc_created` → `shared` → `saved`
(cataloged). `creating` can instead go to `create_uncertain`, which is
reconciled as above before anything else happens. After
`MEETING_RECORD_MAX_ATTEMPTS` (default 8) the record becomes `failed`, which is
surfaced to admins via the audit log and `GET /meeting-records?persistence_status=failed`.
Every step after `doc_created` is idempotent against the stored document ID,
so it can be retried after a crash. Removal sets `removing` and `removed_at`
before external cleanup, then `removed` once known Google files and catalog
copies are purged. Neither state permits creation, adoption, sharing, or
cataloging. Reconciliation of late files continues in either state.

**Remaining crash-loss window (accepted):** a `meeting` process crash during a
live meeting (existing limitation — sessions are in memory), or between
finalization and the documentation-system insert. The latter is normally
seconds but can last the full `RECORD_HANDOFF_HOLD_S` during an outage. If the
process crashes while holding a `handoff_pending` payload, the channel's last
notice may still say "delayed"; there is no durable hand-off receipt from which
to send a later failure notice. The Discord PDF remains the fallback. This
notification gap is accepted alongside the in-memory loss window.

**Logs** carry `meeting_id`, status, and attempt counts — never transcript
content, participant names, or credentials.

### Services, endpoints, and scopes

| Service | New endpoint | Scope | Caller |
|---|---|---|---|
| documentation-system | `PUT /meeting-records/{meeting_id}` — create if absent; identical replay → 200 with current state; different payload for an existing id → 409 | `meetings:write` | `meeting` |
| documentation-system | `GET /meeting-records/{meeting_id}` — status, Doc URL, delivery state (no transcript) | `meetings:read` | bot |
| documentation-system | `GET /meeting-records?delivery_status=pending&persistence_status=saved` — records whose link still needs posting | `meetings:read` | bot (#223) |
| documentation-system | `PATCH /meeting-records/{meeting_id}/delivery` — `{status, discord_message_id}` | `meetings:write` | bot (#223) |
| documentation-system | `POST /meeting-records/retry-due?limit=N` — persistence retries, due reconciliation (including terminal records), and expiry | `meetings:write` | Railway cron |
| documentation-system | `POST /meeting-records/{meeting_id}/remove` — removal cascade (below) | `meetings:admin` | admin / cron expiry |
| connectors | `POST /google/docs` — `{meeting_id, attempt_id, voice_channel_id, title, html}` → `{document_id, url}`; creates the Doc **unshared** | `meeting-write` | documentation-system |
| connectors | `POST /google/docs/search` — `{meeting_id}` → every accessible Doc tagged with it, including trashed files and all pages (ID, attempt ID, created time, trashed state) | `meeting-write` | documentation-system |
| connectors | `PUT /google/docs/{document_id}/sharing` — `{commenters: [group emails]}`; makes the direct shares exactly this set and returns the resulting permissions | `meeting-write` | documentation-system |
| connectors | `POST /google/docs/{document_id}/fetch` — content **and complete ACL**, read with the writer identity; refuses files without `misty_meeting_id` | `meeting-fetch` | documentation-system |
| connectors | `GET /google/docs/{document_id}/status` — `exists` / `trashed` / `deleted`, as seen by the owner | `meeting-fetch` | documentation-system |
| connectors | `DELETE /google/docs/{document_id}` — removes every share, then permanently deletes | `meeting-write` | documentation-system |

`meeting` gets a documentation-system consumer key with `meetings:write`;
documentation-system gets a connectors key with `meeting-write` and
`meeting-fetch`. Vendor calls stay
behind connectors' injected provider (#60/#82 pattern); write-path credentials
(the infrastructure account's OAuth token, see "Google setup") are separate
from the read path's service account, so `/fetch` keeps read-only scopes.
**Google OAuth scope: `drive.file`** (create and manage only files Misty
created — enough for create, search by `appProperties`, reading the full ACL
as owner, sharing, and permanent deletion).
Confirm during #222 implementation; fall back to `drive` only if a required
call is refused.

**Link delivery (#223) survives restarts.** After `/stop`, the bot re-reads the
saved reference and posts its link immediately if persistence is `saved`.
It also polls saved records with `pending` or `failed` delivery on Discord
startup and every 10 minutes, and PATCHes delivery with the message ID.
It uses the stored exact `text_channel_id` and `guild_id`, including threads;
it never falls back to a broader parent channel. The message identifies the
title/start time, mentions the original requester and states that the Doc has
minutes and the full transcript. Recipients still need their own Drive access.
Only a pointer is posted, with link previews suppressed and no document text.
Retries edit a known message; recent-history recovery and a deterministic nonce
cover interrupted sends. Delivery failures leave persistence untouched. See
the bot's [delivery/recovery runbook](../discord-bot/docs/meeting-delivery.md)
for permissions, duplicate-recovery limits and offline playground scenarios.

### Metadata schema (documentation-system, migration `012`)

The `meeting_records` table exists in both storage adapters and is owned by
documentation-system (it already owns the catalog and has Postgres; `meeting`
stays database-free):

| Column | Type | Notes |
|---|---|---|
| `meeting_id` | UUID PK | Bot session UUID |
| `doc_id` | UUID FK → `docs.id`, null | Set when cataloged |
| `google_document_id` | text unique, null | Canonical ID; set once by guarded adoption, never replaced by a late result |
| `google_deleted_at` | timestamptz, null | Confirmed canonical-deletion receipt, preserved across cleanup retries (migration 014) |
| `removed_google_document_ids` | text[] | Permanent ingestion fences for canonical and discovered duplicate IDs (migration 015) |
| `index_pending` | boolean | Durable initial-index/retry work; set with catalog acceptance (migration 013) |
| `doc_url` | text, null | Canonical Doc URL |
| `guild_id`, `text_channel_id`, `voice_channel_id` | text | Discord snowflakes |
| `title` | text | Supplied name, else LLM title |
| `started_at`, `ended_at` | timestamptz | |
| `requester_discord_id` | text | |
| `requester_person_id` | UUID, null | Resolved via directory |
| `participants` | JSONB | `[{discord_id, display_name, person_id}]`; `person_id` null when lookup fails |
| `audience_team_ids` | UUID[] | Snapshot of the voice channel's teams at meeting time |
| `summary` | text | Generation-time snapshot |
| `decisions` | JSONB `string[]` | Generation-time snapshot, queryable |
| `action_items` | JSONB `string[]` | Generation-time snapshot, queryable |
| `generated_at` | timestamptz | Distinguishes snapshot from later Doc edits |
| `pending_payload` | JSONB, null | Transcript awaiting Doc creation; cleared only after canonical adoption commits |
| `persistence_status` | text | `pending` / `creating` / `create_uncertain` / `doc_created` / `shared` / `saved` / `failed` / `inaccessible` / `removing` / `removed` |
| `create_attempt_id`, `create_lease_until` | UUID, timestamptz, null | Serializes creation/reconciliation; stale results cannot change the record |
| `next_reconcile_at` | timestamptz, null | Set before the first create; remains scheduled after save, failure, and removal |
| `attempts`, `next_attempt_at`, `last_error` | int, timestamptz, text | `last_error` is a code, never content |
| `delivery_status` | text | `pending` / `posted` / `failed` / `disabled` (#223; removal disables delivery) |
| `discord_message_id` | text, null | Link message, for retry/update and removal |
| `removed_at`, `removed_by` | timestamptz, text, null | Tombstone; content columns cleared on removal |
| `created_at/by`, `updated_at/by` | | Attested actor convention |

documentation-system resolves participant and requester identities through the
directory API it already calls. The catalog `docs` row gets tag `meeting` and
`owning_person_id` = requester when resolved.

### Google setup

Meeting folders are organized by Discord voice-channel ID. Access comes from
the channel's configured teams and their managed Google Groups, with no per-user
overrides. Configure both Discord permissions and the team mapping deliberately.

**Writer identity: the UTMIST infrastructure Workspace account, via OAuth.**
The existing connectors service account belongs to a project outside UTMIST's
Google Workspace, so it cannot own files in UTMIST's Drive or share them with
Workspace groups. team-tracking already acts as the infrastructure Workspace
account through OAuth (`GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` / `_REFRESH_TOKEN`)
to manage Google Groups. connectors uses the same account for meeting Docs,
with its own refresh token granted `drive.file` and stored as `SecretStr`
settings for the write path only. The read path (`/fetch`, `/discover`) keeps
the existing service account and read-only scopes.

**Location.** A root folder `Misty Meeting Records/`
(`GOOGLE_MEETING_ROOT_FOLDER_ID`), owned by the infrastructure account — in a
UTMIST Shared Drive if one is available, otherwise in that account's My Drive.
If a Shared Drive is used, its membership is limited to infra/Drive admins,
because every Shared Drive member can see every file. Confirm the root location
and its admin-only audience before enabling the writer.

**Layout: one subfolder per voice channel**, named by the voice channel's
snowflake (stable across renames), created on first use and found again by
`appProperties.misty_voice_channel_id`. Folders are for organization only and
are shared with no members.

**Permissions: per-Doc shares, not folder inheritance.** After its ID is
recorded, each Doc gets direct *commenter* shares to the managed Google Group
(`team_google_groups.group_email`) of each audience team — members can read
and comment but not edit the record. Nothing else is shared. Direct per-Doc shares avoid inherited access
exposing a private meeting, and later changes to a channel's teams do not
silently broaden old records.

**The catalog reads meeting Docs as the owner.** Google only shows full
permission details (principal emails) to users who can manage sharing, and the
catalog's ACL resolution rejects user/group permissions without an email. So
meeting Docs are **not** shared with the read service account. For any catalog
doc linked to a `meeting_records` row, documentation-system fetches content and
ACL through connectors' `POST /google/docs/{id}/fetch` (writer identity) — at
first ingest, on refetch, and in `source-access/refresh-due`. All other
documents keep the existing `/fetch` path. Because the service account never
sees meeting Docs, discovery (#291) cannot catalog them a second time.

**Audience = the teams configured for the voice channel** where the meeting
happened, via `/channel-access` (team-tracking
`GET /channels/{guild}/{channel}/teams`, keyed by the voice channel's ID). An
admin configures a voice channel by running `/channel-access` in that voice
channel's chat. Keep Discord voice-channel permissions and this team mapping
aligned; Google access is not inferred from Discord role permissions.

**Discord and Google have separate audiences.** Voice-channel teams control the
saved Doc's Google sharing. PDF and saved-link delivery retain the original text
channel or thread; a private thread is never replaced by its broader parent.
Live Q&A follows the [bot command's channel rules](../discord-bot/README.md#commands).
Changing `/channel-access` does not change Discord channel permissions.

**Fail closed.** Before writing, documentation-system checks that every
audience team has a group with status `synced`. If the voice channel has no
teams or a group is not synced (e.g. while #236/#237 are unavailable), the Doc
is still created but shared with **no members** — admins only. The record is
`saved` with `audience_team_ids = []`, and the bot says the record is admin-only;
configuring `/channel-access` affects future recordings. Misty never uses
domain-wide or anyone-with-link sharing. After creation and sharing, connectors
reads back the full permissions. Allowed admins are the explicitly configured
infra/Drive admin principals, including inherited Shared Drive members; a
principal is not trusted merely because it has an elevated Drive role.

**Unexpected access is a security removal, not a persistence retry.** If any
other principal has access, connectors immediately attempts to revoke direct
shares and reports `unexpected_acl`. The current revocation is best effort and
skips owners/inherited permissions; the error alone does not prove access was
removed. External access can persist until Google deletion is confirmed. For an adopted canonical Doc, documentation-system
starts the `/remove` cascade: set the removal marker, disable link delivery,
permanently delete the Doc, purge any catalog copy, and retain the tombstone.
Deletion is required even when an unwanted inherited permission cannot be
revoked directly. Cleanup failures remain `removing` and retry with an admin
alert; they never resume persistence. An unadopted file is deleted without
clearing the pending payload. A canonical Doc removed for unsafe permissions
is deliberately not recreated: expose the removal status through
`GET /meeting-records/{id}` and emit an `unexpected_acl` audit event and admin
alert. The responding admin tells the requester that no Google record was
retained and that the Discord PDF is the fallback, using the same removal
notification responsibility as below. For this security-only removal, keep the
existing Discord PDF unless its removal is separately requested. Do not report
this as a delayed save or offer an automatic retry against the deleted canonical
ID. Google and catalog copies follow the same purge policy as an admin removal.

**Source grants follow Drive.** Source-ACL sync turns Drive group shares into
catalog grants at ingest and on refresh. Independently managed catalog grants
do not grant access to the Google Doc itself.

### Consent and visibility

The onboarding rule already requires that everyone knows they are being
recorded and is OK with it. Misty makes that explicit:

- At `/record start`, the bot posts in the text channel: recording has started;
  the transcript and minutes will be saved as a Google Doc visible to
  <teams | admins only>, plus a reminder that everyone present should consent; ask a Misty admin to remove a record.
- `/record start save:false` skips the Google Doc for that meeting
  (PDF only). Any participant may ask the requester to stop.
- Moving the bot ends a saved recording. Audio forwarding stops synchronously
  before receive-stream draining, so destination-channel speech cannot enter the
  original audience's record. Start a new recording for the destination audience.
  PDF-only (`save:false`) recordings continue to follow moves.
- Posting a link in Discord never grants Drive access (#223).

### Retention and removal

- **Lifetime:** defaults to 365 days after `ended_at`, configured by
  `MEETING_RECORD_RETENTION_DAYS`. `retry-due` also runs expiry.
- **Transcript copies held for retries:** `pending_payload` is cleared once the
  canonical Doc ID is committed; for `failed` records it is cleared after 7 days.
- **Deletion authority:** Misty admins (infra leads). Drive admins can also
  delete in Drive.
- **Member removal path:** a member DMs or pings a Misty admin with the Doc link
  or meeting ID. The admin calls `POST /meeting-records/{id}/remove`. Removal
  blocks new persistence and purges every discovered copy:
  1. **Stop new work:** atomically set `removing` and `removed_at/by`, invalidate
     the current attempt, and disable pending link delivery. Retries and late
     completions cannot clear this marker. Already-issued Google calls can
     still finish, so removal keeps the reconciliation schedule above.
  2. **Google:** connectors removes every share, then **permanently deletes**
     the canonical Doc and every tagged duplicate (`DELETE /google/docs/{id}`).
     Trash is not enough: in My Drive, people who already had access can still
     open a trashed file until it is permanently deleted. No recoverable Google
     copy is kept.
  3. **Catalog:** `/remove` invokes an internal atomic purge of the linked doc
     and every catalog alias of the canonical or discovered Google identities,
     even if `record.doc_id` was never set. It clears titles, descriptions,
     ownership metadata, snapshots, full text, chunks, tags and grants, then
     sets `active=false` and `purged_at`. Identity and audit fields remain so
     ingestion, refetch, refresh and discovery cannot restore a removed copy.
     This runs even when Google cleanup is unavailable. Ordinary `active=false`
     retirement only removes search chunks; it retains content and grants and
     is not a substitute for removal.
  4. **Meeting record:** content columns (summary, decisions, action items,
     participants, payload) are cleared, keeping only a tombstone
     (`meeting_id`, canonical Google document ID, `removed_at/by`, and cleanup
     status/lease/scheduling fields) so retries cannot recreate it. Mark
     `removed` after known copies are purged; repeated scans still delete
     late-created files. Clear every other field, including `title` and
     `requester_*`; keep only identifiers needed to finish any pending catalog
     or Discord cleanup until those steps succeed.
  5. **Discord:** the admin deletes both the link and PDF message manually,
     recording their locations before invoking `/remove`. The bot tracks the
     link's `discord_message_id`, but not the PDF, and does not automatically
     delete either message. Copies members already downloaded cannot be
     recalled; the admin tells the requester so. Expiry runs the Google/catalog
     cascade and also requires separate Discord cleanup.
- **Members who leave** lose access when they leave the managed Google Group;
  their past speech is not removed from records.

### Human edits, deletion, and access changes

- The **Doc is the source of truth for readable content.** Members can
  comment but not edit; Drive admins can correct it. Edits flow into
  the catalog's `content_snapshot` and search index through the existing
  refetch and `refresh-due` paths.
- **Structured metadata is a generation-time snapshot** (`generated_at`) and is
  never updated from Doc edits. Consumers that want current notes read the Doc.
- **A 404 is not proof of deletion.** Google returns `404 notFound` both when
  a file is gone and when the caller has lost access. A failed fetch therefore
  never triggers cleanup on its own: the record becomes `inaccessible`, is
  reported to admins, and the catalog retains its snapshot, `doc_content`, and
  chunks; existing grants still expire on their normal TTL and are not extended
  by a failed fetch. Generic `SourceGone` handling deletes `doc_content` and
  chunks on refetch or refresh. Meeting-linked docs instead use the
  non-destructive fetch-failure path for an unconfirmed owner-fetch 404 during
  ingestion, refetch and refresh. Destructive cleanup runs
  only after connectors'
  `GET /google/docs/{id}/status` — asked as the owning writer identity —
  confirms the Doc is `deleted` or `trashed`. The current Google adapter can
  positively identify `trashed`; a 404 stays `inaccessible` because it cannot
  independently prove permanent deletion. Then documentation-system runs
  the same cascade as `/remove` (permanently deleting a trashed Doc). If the
  writer identity itself has lost access, nothing is deleted and admins
  investigate.
- **Access changed in Drive:** the source-ACL sync revokes or adds catalog
  grants on the next refresh; `audience_team_ids` keeps the original snapshot
  for audit.

### Verification and prerequisites

**Recovery tests use offline fakes.** Delay a create past lease expiry and
past a replacement reaching `saved`; reject the stale completion and delete its
Doc on a later scan. Also cover an older duplicate appearing after canonical
adoption (preserve the canonical Doc and human edits), late creates after
failure/removal, repeated empty scans before a late file appears, and cleanup
failure followed by retry. None may reopen a terminal record or share/catalog
a duplicate. Also cover unexpected access after canonical adoption: removal is
terminal, cleanup retries cannot restart persistence, and status/audit output
reports the security deletion for admin follow-up. For owner-fetch 404s in
ingest, refetch, and refresh, verify that content and chunks survive until deletion or
trash is independently confirmed, that failed fetches do not extend grant TTLs,
and that confirmed removal still purges every retained copy.

**Prerequisites:**

1. Infra issues a connectors OAuth refresh token for the infrastructure
   Workspace account with `drive.file`, confirms the location (Shared Drive or
   that account's My Drive), creates `Misty Meeting Records/`, and sets
   `GOOGLE_MEETING_ROOT_FOLDER_ID`. In a Shared Drive, the account needs a role
   that can permanently delete files (Manager).
2. Consumer keys: `meeting` → documentation-system (`meetings:write`);
   documentation-system → connectors (`meeting-write`, `meeting-fetch`); bot → documentation-system
   (`meetings:read`, `meetings:write`).
3. The Railway cron also calls `POST /meeting-records/retry-due`.
4. Admins configure `/channel-access` on voice channels used for meetings.
5. Member (non-admin) access needs synced team groups (#237). Without them
   records are admin-only, which is safe and does not block shipping.

Live Q&A reads in-memory session state and does not wait on durable persistence.
It remains an in-progress feature; see [known limitations](#known-limitations-and-live-verification).

## The wire contract (keep both sides in lock-step)

The bot's `meetingClient.encodeFrame` and the service's `_parse_frame` are inverses. **If you change one, change the other.**

- **WebSocket:** `{wsUrl}/meetings/{sessionId}/stream?guild_id={guildId}` — the consumer key is sent as the **first WS text frame** `{"key": "…"}` (keeps it out of URLs/logs); the service also still accepts a `?key=` query param.
- **Binary audio frame:** `[2-byte big-endian speaker_id length][speaker_id UTF-8][8-byte big-endian ts_ms][raw Opus payload]`. `ts_ms` is elapsed monotonic milliseconds since recording start on one shared clock for every speaker, including silence; it is not an epoch or per-speaker clock.
- **Control frame (text/JSON):** `{"speaker_id": "...", "display_name": "..."}` — registers the display name shown for a speaker.
- **Audio-complete control:** after draining capture, send `{"end_of_audio":true}` before `POST /stop`. The service waits for this barrier before quiescing ingestion; older clients fall back to a bounded five-second wait that can leave a short transcript. See the [complete wire/stop contract](../services/meeting/docs/API.md).
- **Auth:** the `platform_auth` consumer key — on the WS as the first text frame `{"key": "…"}` (or a `?key=` query param; both server-supported), and on `GET /transcript` / `POST /stop` as the `X-API-Key` header. The `meetings` scope is required.
- **`POST /stop` request:** optional `title` (up to 100 characters) and `record` context; see the [durable stop contract](../services/meeting/docs/API.md#durable-record-context). A supplied title wins over the LLM-generated title. **Response:** `{ transcript, minutes, pdf_b64, record }` (PDF base64-encoded). The bot posts it @-mentioning whoever ran `/record start` — including on the auto-stop path, where there is no interaction to read the requester off.

## The "separate surface" in the bot

Voice recording does **not** go through the bot's neutral slash-command path (`defineCommand` → `router.js` → a surface-agnostic handler returning a reply payload). That abstraction is for stateless request/response commands; a long-lived, stateful recording that posts file attachments does not fit it. Forcing it in (as an earlier iteration did) required smuggling live Discord objects through the router and bolting an attachment side-channel onto the reply contract.

Instead, `/record` is a **dedicated path in the Discord adapter**: `adapters/discord/index.js` intercepts `record` interactions *before* neutral dispatch, and `adapters/discord/recording.js` drives `meetingSurface` directly with the live interaction. `router.js` stays completely clean. The command is still declared (so it registers as a slash command) but its handler is never reached.

**Surface isolation** is preserved: only `index.js`, `registerCommands.js`, and `adapters/*` (plus the sanctioned voice modules `recorder.js`/`meetingSurface.js`) import `discord.js`/`@discordjs/voice`. `meetingClient.js` is transport-only (no Discord). The attachment poster is Discord-specific, so it is built in `index.js` (the composition root) and **injected** into `meetingSurface` — keeping `context.js` free of any Discord import.

**Authorization.** Because `/record` bypasses `router.js`, it also bypasses the router's single Policy Enforcement Point. So the dedicated handler re-runs the same `resolvePrincipal → authorize(policy, …)` sequence itself — but it reads `policy` from the command metadata (per-subcommand `auth`, else command `auth`, fail-secure to `'linked'`) rather than hardcoding it, so the two can't drift. The policies are deliberately split:

- `start` → `'linked'`: starting a recording consumes resources (a voice connection + a live session), so an unlinked caller is turned away with the standard "link your account" message and never reaches `meetingSurface.start`. Fails **closed** if the directory is unavailable.
- `status` / `stop` → `'public'`: `status` is a local read (no directory call), and `stop` is *de-escalating* — gating it would mean a directory outage could strand a running recording (with no length cap, that's the memory escape hatch). So both work regardless of link state.

If `start` is ever silently downgraded to public, any guild member could drive live voice capture unauthenticated — keep it gated.

The dedicated adapter also checks voice-channel context before changing state.
A start attempt is refused while a recording is already active in the guild —
whether the caller is in the recorded channel (a duplicate start) or a
different one (Misty is busy elsewhere in the server; recordings are one at a
time **per guild**, sessions being keyed by `guildId`, not a single global
slot) — and a stop from outside the recorded channel is refused too, with the
active channel and auto-stop guidance. That stop guard has one exception:
`stop` is public precisely so anyone can end a runaway recording, so it does
not apply once the recorded channel is already empty of humans (the same
`humansIn` head-count auto-stop itself uses). Without that carve-out, a missed
`voiceStateUpdate`, or a recorded channel that's since gone private, full, or
been deleted, could strand the session with nobody able to finalize it. The
four-hour ingestion cutoff does not stop the session or deliver minutes. Status always names the recorded channel; when idle it tells
the caller that Misty will join the voice channel when recording starts, and
when the recorded channel is empty it notes the imminent auto-stop and the
`/record stop` option. Start attempts while the recorded channel is empty
similarly explain that Misty is wrapping up and can be ended immediately with
`/record stop`.

If a moderator moves Misty during a **saved** recording (`save:true`, the
default), capture stops and the meeting finalizes to preserve its original
audience. Only PDF-only recordings (`save:false`) continue in the destination:
status, stop permissions and empty-channel checks then follow that channel.
Delivery still targets the text channel where recording started.

**Auto-stop.** `wireDiscordClient` listens for `voiceStateUpdate` (via `createAutoStop`); when the channel being recorded goes empty of non-bot members it **debounces** — schedules a stop after a grace period (`AUTO_STOP_GRACE_MS`) and cancels it if a human is back either on a later event or at fire time (re-check). This avoids a transient client blip or a voice-region failover irreversibly finalizing a live meeting on a single stray "last member left" event. When it does fire it calls `meetingSurface.stop(guildId)` — the same path as `/record stop`. The recorded channel is read from the live session via `meetingSurface.activeSession(guildId)` (which returns the `{ sessionId, voiceChannel }` snapshot opaquely, so `meetingSurface` keeps no `discord.js` dependency), which keeps the head-count honest and makes the listener a no-op once a session is torn down. Each pending timer is **bound to its `sessionId`**, so a timer scheduled for one recording can never terminate a *later* recording that reuses the same guild (a real bug caught in review): a new recording always schedules its own full-grace timer, and a stale one no-ops at fire time. The head-count is computed from **`guild.voiceStates.cache`** (maintained by the `GuildVoiceStates` intent — the same one that powers voice receive), *not* from `channel.members`. `channel.members` resolves each occupant to a `GuildMember` via `guild.members.cache`, which only the privileged `GuildMembers` intent keeps populated; without it that resolution is unreliable and miscounts occupants — including the recorder bot itself, whose member often isn't cached — which is what made auto-stop silently never fire in an early version. Counting voice states avoids that dependency entirely, and the recorder bot is excluded by its own user id (`client.user.id`), so `GuildMembers` is deliberately **not** required.

## Deployment

`meeting` is a private Railway service (`meeting.railway.internal:<PORT>`), like the other services: `platform_auth` consumer keys, `/health`, Dockerfile build (no ffmpeg binary — PyAV bundles its own), GitHub-connected auto-deploy on push to `staging`. It needs AWS credentials (Transcribe, via the standard chain), an `llm` consumer key (for minutes), and its own `CONSUMER_KEYS` set. The bot gets a `meeting` consumer key (`MEETING_API_KEY`) and `MEETING_BASE_URL=http://meeting.railway.internal:<PORT>`; the WebSocket rides Railway's private network. If `MEETING_BASE_URL` is unset the bot boots fine and `/record` reports "not configured" — the feature degrades gracefully.

## Known limitations and live verification

Audio handling changes need the service's [live verification checklist](../services/meeting/README.md#live-verification), covering speaker timing, AWS stream restarts, and concurrent-stream quotas. Repository tests use fakes and do not establish live provider acceptance.

- **Meeting length cutoff:** `MAX_MEETING_MS` defaults to four hours. Once exceeded, the service drops new audio frames; it does not close the socket, finalize the session or deliver minutes. `/record stop` or auto-stop-on-empty is still required. Configure a millisecond limit through the service setting; do not rely on the cap to recover a stranded recording.
- **Dropped WebSocket recovery has a deadline.** The service holds a disconnected session for `DISCONNECT_GRACE_S` (default 60s), and the bot responds to an unexpected `onClose`/`onError` by attempting normal finalization (`POST /stop`). Starting stop does not cancel the grace timer: if the session remains registered at expiry, teardown can interrupt an unfinished transcript flush. Recovery is therefore best effort; see the [disconnect contract](../services/meeting/docs/API.md#disconnect). A failed finalize reports "could not be recovered". Both the server-side hold and prompt bot-side salvage are required, and the close code is logged on both sides.
- **Live Q&A:** `/qna question:<text>` is in progress and registered globally in every server the bot is in. It runs only in the recording’s original text channel, replies privately, and makes no directory lookup. The meeting service snapshots the last 15 minutes, shortens unusually dense windows with explicit disclosure, and returns validated speaker/timestamp references. No Q&A history is retained. See the [API contract](../services/meeting/docs/API.md#post-meetingssession_idqna) and [pipeline architecture](../services/meeting/docs/ARCHITECTURE.md#live-meeting-qa).
