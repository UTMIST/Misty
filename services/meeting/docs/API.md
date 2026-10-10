# meeting — API reference

Base URL (local): `http://localhost:8004` · Swagger UI: `/docs` · Schema: `/openapi.json`

> **The WebSocket route is not in OpenAPI.** `/openapi.json` covers the HTTP routes only — WebSockets aren't representable in the spec. The wire format below is the contract; the Discord bot's voice surface mirrors it.

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/health` | none | Liveness probe |
| WS | `/meetings/{id}/stream` | `?key=` or first frame | Audio ingest + optional finalized subtitle events |
| GET | `/meetings/{id}/transcript` | `X-API-Key` | Poll the rolling transcript |
| POST | `/meetings/{id}/stop` | `X-API-Key` | Finalize: transcript + minutes + PDF; accepts an optional title |

## Authentication

```
X-API-Key: meeting_<prefix>_<secret>
```

A key is either the bootstrap env key (`API_KEY`, carries `admin`) or a per-consumer key from `CONSUMER_KEYS`. A **single scope, `meetings`**, covers every protected endpoint — HTTP and WS alike. This service has one internal consumer class (the Discord bot), so separate read/write/stream scopes were a deliberate simplification. `admin` satisfies it. `/health` is the unauthenticated exception.

## `session_id`

Must match `^[A-Za-z0-9_-]{1,64}$`. The service writes nothing to disk, so this is input hygiene rather than path safety.

A `session_id` unknown to the in-memory registry returns **404** on `/transcript` and `/stop`. "Unknown" covers three cases that are indistinguishable from outside: never started, already stopped, or **the process restarted**. Sessions live only in memory.

---

## `WS /meetings/{session_id}/stream`

```
ws://.../meetings/{session_id}/stream?key=<consumer-key>&guild_id=<guild-id>
```

### Handshake

**Connection URL query parameters:**

| Param | Required | Notes |
|---|---|---|
| `key` | optional; required in first frame if omitted here | Supported query parameter. Validated **before** the socket is accepted; a bad key is rejected at the handshake. The bot uses first-frame auth to keep keys out of URL logs. |
| `guild_id` | optional | Passed to session creation. Omitted → `session_id` is used as the guild_id |

If `key` is omitted from the query string, the server accepts the socket and requires the **first** message to be a text frame `{"key": "..."}`. Missing, malformed, or invalid → closed with **1008** (policy violation) before any audio is processed.

Close code **1008** is also used for: an invalid `session_id` (before any session is created), and a second connect for an already-active `session_id` (the existing session is left untouched).

**First JSON message: authentication and subtitle opt-in together (client → service):**

To subscribe to subtitles, connect without a `key` query parameter and send
this JSON text frame **first**, before speaker controls or audio:

```text
ws://localhost:8004/meetings/meeting-123/stream?guild_id=<discord-server-id>
```

```json
{"key": "<consumer-key>", "subtitle_events": true}
```

This is the same first message that carries the API key, with the optional
subtitle flag added. There is no second authentication handshake or separate
subscription message. The WebSocket's HTTP upgrade opens the transport; the
service then validates this message's key and scope before sending
`session.ready` or any subtitle events.

| JSON field | Type | Required | Meaning |
|---|---|---|---|
| `key` | string | yes, when authenticating through the first frame | Existing consumer API key, requiring `meetings` or `admin`. |
| `subtitle_events` | boolean | no; default `false` | Set to `true` to request finalized subtitle events on this connection. Only the JSON boolean `true` enables it. |

`subtitle_events` belongs to this first authentication message. It is
not a URL query parameter or HTTP upgrade header, and sending it later as a
control message does not enable subtitles. The service acknowledges an
authenticated subscription with `session.ready`, shown below. `/record start`
defaults subtitles to on, so the bot sends this flag unless the member chooses
`subtitles:false`; the protocol itself defaults the flag to off.

Omitting the flag, setting it to false, or using query-parameter auth produces
no service-to-client events. Audio/control messages remain unchanged.

Both key transports remain supported. Subtitle opt-in is independent of
authentication: the service creates no session or outgoing sender and sends
no ready/subtitle events until authentication and the `meetings`/`admin` scope
check succeed. First-frame auth accepts the WebSocket transport so it can read
the key, but that acceptance alone does not authenticate the client.

### Messages the server accepts

**1. Control — WebSocket text frames, UTF-8 JSON.**

```json
{"speaker_id": "<id>", "display_name": "<name>"}
```

Registers or updates the display name for a speaker. Send whenever a speaker's identity becomes known. Until one arrives, the `speaker_id` itself is used as the display name.

```json
{"end_of_audio": true}
```

Send **once**, after the recording stops and every captured frame has been forwarded. Because the socket delivers in order, this proves all audio has arrived, and `POST /stop` waits for it before finalizing.

Without it — an older client, a crash, a dropped socket — `/stop` proceeds after a 5 s drain timeout and logs a warning. The transcript tail may then be short, which is exactly the failure this signal prevents. **Send it.**

Unknown or malformed text frames are ignored, not fatal.

**2. Audio — WebSocket binary frames**, one raw Opus packet each:

```text
[2 bytes,  big-endian uint16]  speaker_id_len
[speaker_id_len bytes, UTF-8]  speaker_id
[8 bytes,  big-endian uint64]  ts_ms
[remaining bytes]              raw Opus packet payload
```

A length-prefixed speaker id, an 8-byte millisecond timestamp, then the raw Opus payload with no further framing. Truncated or undersized frames are dropped and counted, not fatal.

### Optional messages the server sends

Every event is a UTF-8 JSON text frame on the **same authenticated connection**.
The bot authenticates with its existing `meetings` key; no reverse webhook,
listener, or additional credential is needed. Each meeting has its own socket,
even when the bot records in multiple Discord servers (API term: guilds).

```json
{"type": "session.ready", "session_id": "meeting-123", "subtitle_events": true}
```

Sent only after successful authentication and session creation. Confirms that
this service supports the subscription. An older service sends no ready event;
the bot disables subtitles after five seconds while recording continues.

**Live subtitle message (`subtitles.chunk`, service → client):**

The live transcription result arrives as one JSON text message per finalized
chunk. It is pushed over the socket, rather than returned as an HTTP response
or a cumulative transcript array:

```json
{
  "type": "subtitles.chunk",
  "session_id": "meeting-123",
  "sequence": 1,
  "speaker_id": "discord-user-id",
  "display_name": "Alice",
  "start_ms": 12000,
  "text": "We should launch on Friday"
}
```

Every field below is present in each chunk message:

| Field | JSON type | Meaning |
|---|---|---|
| `type` | string | Always `"subtitles.chunk"`; distinguishes subtitle text from ready, completion, and error events. |
| `session_id` | string | Meeting session bound to this authenticated socket. |
| `sequence` | integer | Positive, 1-based chunk number within the session, allocated in delivery order. |
| `speaker_id` | string | Speaker identifier supplied with the audio; the bot uses the Discord user ID. |
| `display_name` | string | Speaker name at chunk creation, falling back to `speaker_id` when no name is known. |
| `start_ms` | integer | Chunk start in milliseconds since the meeting began, mapped to the meeting timeline. |
| `text` | string | Finalized text for this chunk only. Subsequent chunks do not replace or extend this message. |

Only **finalized** AWS results produce chunks. Provisional results never leave
the service. Each result is split using the existing word-gap (1.5 s) and
segment-span (5 s) rules, and its boundary closes the chunks: published text is
immutable. These thresholds format already-finalized words; they do **not**
bound AWS finalization latency. Names and meeting-relative timestamps are
captured when the chunk is produced. A later name update affects future chunks.

`sequence` starts at 1 for each meeting and describes **delivery order**, not
speech order. All speakers share one bounded FIFO and one sender for their
meeting. Sequence assignment and insertion run together on the event loop
without an await; the sender awaits each send before taking the next event.
WebSocket preserves that send order on the live connection. Independent
speakers can finalize in a different order from when they spoke: use `start_ms`
to sort speech, and `sequence` to detect delivery gaps/duplicates. Neither the
socket nor this queue is durable; there are no acknowledgements, reconnects,
or replay. The bot routes by its own session/thread binding, never by a
Discord destination supplied in an event.

The bot formats the example chunk as `[00:12] **Alice:** We should launch on
Friday` (the speaker label renders in bold). Multiple chunks received within
the same two-second batch can share a Discord message. IDs and sequence numbers
are used internally and are not displayed. See the
[Discord message example](../../../docs/MEETING-RECORDING.md#subtitle-message-example)
for a complete batch and its end marker.

```json
{"type": "subtitles.complete", "session_id": "meeting-123", "last_sequence": 1, "status": "complete"}
```

Enqueued **after** all speakers finish flushing, behind their tail chunks,
before minutes/PDF generation. `last_sequence` is the last allocated chunk
number (0 for no speech). `status` is `complete` or `incomplete`; a failed or
timed-out speaker flush is incomplete. This describes the finalized subtitle
stream, not successful PDF delivery or a guarantee that AWS recognized every
word. Discarding a still-connected session also attempts incomplete completion.

```json
{"type": "subtitles.error", "session_id": "meeting-123", "code": "queue_overflow"}
```

Fixed codes are `queue_overflow` and `send_failed`. The sender holds at most
256 queued events or 256 KiB of serialized JSON and bounds each send at 5 s.
Overflow/failure disables subtitles and attempts one content-free error event;
it never blocks audio ingestion or report generation. A failed socket may be
unable to deliver the error. The bot marks received history incomplete and
uses the existing HTTP stop/salvage path for the PDF.

See [architecture](ARCHITECTURE.md#finalized-subtitle-delivery) for the queue
trade-offs and [the recording guide](../../../docs/MEETING-RECORDING.md#live-subtitles)
for Discord batching, permissions, and retained history.

### Disconnect

Disconnecting (or erroring) **without** a preceding `POST /stop` **holds** the session for `DISCONNECT_GRACE_S` (default 60 s) rather than destroying it. The transcript is already assembled server-side, so within that window `POST /stop` still finalizes it into minutes and a PDF exactly as a normal stop would — which is how a client that lost its socket recovers a meeting. The disconnect also marks end-of-audio, since a closed socket can deliver no more frames.

Only when the grace period expires with no `/stop` is the session torn down via `discard()` — abort each speaker's Transcribe stream, deregister, done. That path deliberately skips the finalize pipeline, so **no minutes and no PDF are produced**: it involves a blocking `llm` call not worth paying for when nobody is waiting for the result. Set `DISCONNECT_GRACE_S=0` to discard immediately instead.

The WebSocket close code is logged on every abrupt disconnect (`session <id>: WS closed without POST /stop (code=…)`).

To get minutes, call `POST /stop` before closing the socket.

---

## `GET /meetings/{session_id}/transcript`

Poll the rolling transcript of an active session.

**Response** (`TranscriptView`) — `200`:

```json
{"segments": [
  {"speaker": "Alex Chen", "start_ms": 12400, "text": "Let's start with sponsorship."},
  {"speaker": "Sam Patel", "start_ms": 19100, "text": "I've got the deck ready."}
]}
```

**Polling makes no additional AWS or LLM call.** It reads finalized words, but
each request maps, groups, sorts, and transfers the accumulated transcript.
Repeated polling therefore consumes service CPU and bandwidth even in silence.

It is a **cumulative view, not a diff**. Each response is the whole transcript so far; there is no cursor or append stream. `start_ms` is meeting-relative, mapped back from each speaker's stream-relative timings.

| Condition | Status |
|---|---|
| `session_id` fails the regex | 400 `invalid session_id` |
| Session not in the registry | 404 `unknown session` |
| Missing key / no `meetings` scope | 401 / 403 |

---

## `POST /meetings/{session_id}/stop`

End the session and get everything back. This is the only call that produces minutes.

The optional request body can provide a caller-selected PDF title. It is limited
to 100 characters:

```json
{"title": "Sponsorship Sync"}
```

When present, `title` takes precedence over the LLM-generated title. Omitting
the body preserves the LLM title behavior, with `Meeting Minutes` as the final
fallback.

What it does, in order: wait for the end-of-audio barrier (max 5 s) → close the Transcribe streams → assemble the final transcript → call `llm` for minutes → render the PDF → tear the session down.

**Response** (`StopResponse`) — `200`:

```json
{
  "transcript": "[00:12] Alex Chen: Let's start with sponsorship.\n[00:19] Sam Patel: I've got the deck ready.",
  "minutes": {
    "title": "Sponsorship Sync",
    "summary": "The team reviewed ...",
    "decisions": ["Target three tiers for 2026"],
    "action_items": ["Sam to send the deck by Friday"]
  },
  "pdf_b64": "JVBERi0xLjQK..."
}
```

`transcript` is one line per segment, formatted `[MM:SS] speaker: text` and sorted by `start_ms` (`pipeline/transcript.py`). Segments at or past one hour use `[HH:MM:SS]` instead — reachable, since `MAX_MEETING_MS` defaults to 4 h. `pipeline/pdf.py` splits on `"] "` so it handles both; don't change the format without changing both.

`pdf_b64` is the base64-encoded minutes PDF. `minutes.title` may be an empty string, in which case the PDF falls back to a generic title.

**If `llm` is unreachable, `/stop` still returns 200** — `minutes` comes back as `{"summary": "(minutes unavailable: LLM service error)", "decisions": [], "action_items": []}` with a PDF built around it. There is no error status for a failed summarization; check the `summary` string.

**No meeting audio is returned, ever.** Audio exists only as transcription input — never mixed, never persisted, never written to disk.

**The session is gone after this call.** It is deregistered as part of stopping, so a subsequent `/transcript` or `/stop` returns 404. Persist the response — this service retains nothing.

This call is **slow relative to the others**: it blocks on the drain barrier, the Transcribe flush, and an LLM round trip. Budget accordingly; `REQUEST_TIMEOUT_S` (default 60) bounds the `llm` leg.

| Condition | Status |
|---|---|
| `session_id` fails the regex | 400 `invalid session_id` |
| Session not in the registry | 404 `unknown session` |
| Missing key / no `meetings` scope | 401 / 403 |

---

## `GET /health`

Unauthenticated liveness probe. Railway's healthcheck path.

```json
{"status": "ok"}
```

Answers `200` without AWS credentials or a reachable `llm`. A green healthcheck does **not** imply transcription or minutes generation work.

---

## Lifecycle at a glance

```
bot                                  meeting
 │  WS connect ?key=…&guild_id=…       │
 ├────────────────────────────────────►│  session created
 │  {"speaker_id","display_name"}      │
 ├────────────────────────────────────►│  name registered
 │  <binary audio frames> ×N           │  decode → per-speaker Transcribe stream
 ├════════════════════════════════════►│  (audio dropped after send; never stored)
 │                                     │
 │  GET /transcript      (free, any time, cumulative)
 │◄───────────────────────────────────►│
 │                                     │
 │  {"end_of_audio": true}             │
 ├────────────────────────────────────►│  barrier lifted
 │  POST /stop                         │
 ├────────────────────────────────────►│  flush → transcript → llm → PDF
 │◄──── {transcript, minutes, pdf_b64} ┤  session deregistered
```

## Constraints worth designing around

- **One process owns a session end-to-end.** No sticky routing means misrouted `/transcript` and `/stop`. Do not scale to multiple replicas without it.
- **A restart loses every in-flight meeting.** Nothing is persisted.
- **`MAX_MEETING_MS`** (default 14,400,000 = 4 h) is a backstop: a session stops accepting audio past it. 4 h is also Amazon Transcribe's per-stream cap.
- **One open Transcribe stream per active speaker**, for the whole meeting. Large meetings can hit the account's concurrent-stream quota.
