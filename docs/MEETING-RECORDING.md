# Meeting Recording — Architecture

How the `/record` feature is split across a Discord **voice surface** (in the bot) and a stateful **`meeting` service**, and *why* the boundary sits where it does. Read this before changing either side — the two halves share a wire contract that must stay in lock-step.

Feature owner: Misty #92. Related: [platform ARCHITECTURE](ARCHITECTURE.md), [`services/meeting`](../services/meeting), [`discord-bot`](../discord-bot).

## What it does

A **linked** member (identity resolved via the directory — see "Authorization" below) runs `/record start` in a Discord voice channel. The bot joins, and as people talk it streams their audio to the `meeting` service, which transcribes each speaker live into a rolling transcript. Recording ends on `/record stop` **or automatically when everyone leaves the voice channel** (the bot ends the meeting once no non-bot member remains). On stop the service turns the transcript into minutes (via the `llm` service), renders a PDF, and hands it back; the bot posts the **PDF** to the text channel, @-mentioning whoever started the recording. Nothing is persisted — the transcript lives in memory for the meeting and is discarded after the report is returned.

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

Every other backend service (`team-tracking`, `documentation-system`, `llm`, `verification`) is a stateless source-of-truth: a request goes in, a response comes out, nothing is held between calls. `meeting` is the deliberate exception. **Live transcription requires *something* to hold the rolling transcript across the life of a meeting**, and the bot is the wrong place (we're keeping it lean and processing-free). So the service keeps an **in-memory registry of active meeting sessions**, each holding one live Transcribe stream per speaker plus the growing rolling transcript those streams produce (see "Live" below). This is a considered trade, not an accident — it's the price of "ask Misty during the meeting" being possible at all. The audio itself is *not* held: each chunk goes straight to AWS and is dropped.

State is still **ephemeral**: a session exists only while its meeting is live, and `POST /stop` (or an abrupt WebSocket disconnect) tears it down and closes its Transcribe streams. Nothing is written to disk, and nothing reaches a database or object store.

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

**Live:** the bot's recorder taps each speaker's raw Opus packets and forwards them (untouched, no decode) over one WebSocket. The service decodes/resamples each speaker's audio and pushes it into that speaker's **persistent AWS Transcribe stream**, held open for the whole meeting. Audio is sent once and never replayed, so `GET /transcript` is a free read of what has finalized so far — it makes no AWS call and costs nothing to poll. `GET /meetings/{id}/transcript` exposes the transcript at any time — the hook that makes in-meeting "ask Misty" a fast-follow (the Q&A feature itself is not built yet).

**Stop:** `POST /meetings/{id}/stop` closes each speaker's Transcribe stream (concurrently, so latency is one flush and not N), assembles the transcript, calls the `llm` service for minutes, renders the PDF, returns it base64-encoded, and discards the session.

**Timeline correctness:** each forwarded frame carries `ts_ms` = milliseconds since the meeting started. AWS Transcribe reports word times relative to *each speaker's own* stream, and a speaker's stream carries only the frames they actually spoke — silence is never sent. So the service records an **anchor** whenever a frame arrives later than the audio already streamed accounts for, and maps word times through the nearest preceding anchor. Anchoring on the first `ts_ms` alone is not enough: it fixes only the speaker's first word, leaving cross-speaker order wrong past the opening minute and collapsing each speaker into one segment (the gap rule never sees a gap). `ts_ms` must always be sent and honored.

## The wire contract (keep both sides in lock-step)

The bot's `meetingClient.encodeFrame` and the service's `_parse_frame` are inverses. **If you change one, change the other.**

- **WebSocket:** `{wsUrl}/meetings/{sessionId}/stream?guild_id={guildId}` — the consumer key is sent as the **first WS text frame** `{"key": "…"}` (keeps it out of URLs/logs); the service also still accepts a `?key=` query param.
- **Binary audio frame:** `[2-byte big-endian speaker_id length][speaker_id UTF-8][8-byte big-endian ts_ms][raw Opus payload]`
- **Control frame (text/JSON):** `{"speaker_id": "...", "display_name": "..."}` — registers the display name shown for a speaker.
- **Auth:** the `platform_auth` consumer key — on the WS as the first text frame `{"key": "…"}` (or a `?key=` query param; both server-supported), and on `GET /transcript` / `POST /stop` as the `X-API-Key` header. The `meetings` scope is required.
- **`POST /stop` response:** `{ transcript, minutes, pdf_b64 }` (PDF base64-encoded). The bot posts it @-mentioning whoever ran `/record start` — including on the auto-stop path, where there is no interaction to read the requester off.

## The "separate surface" in the bot

Voice recording does **not** go through the bot's neutral slash-command path (`defineCommand` → `router.js` → a surface-agnostic handler returning a reply payload). That abstraction is for stateless request/response commands; a long-lived, stateful recording that posts file attachments does not fit it. Forcing it in (as an earlier iteration did) required smuggling live Discord objects through the router and bolting an attachment side-channel onto the reply contract.

Instead, `/record` is a **dedicated path in the Discord adapter**: `adapters/discord.js` intercepts `record` interactions *before* neutral dispatch and drives `meetingSurface` directly with the live interaction. `router.js` stays completely clean. The command is still declared (so it registers as a slash command) but its handler is never reached.

**Surface isolation** is preserved: only `index.js`, `registerCommands.js`, and `adapters/*` (plus the sanctioned voice modules `recorder.js`/`meetingSurface.js`) import `discord.js`/`@discordjs/voice`. `meetingClient.js` is transport-only (no Discord). The attachment poster is Discord-specific, so it is built in `index.js` (the composition root) and **injected** into `meetingSurface` — keeping `context.js` free of any Discord import.

**Authorization.** Because `/record` bypasses `router.js`, it also bypasses the router's single Policy Enforcement Point. So the dedicated handler re-runs the same `resolvePrincipal → authorize(policy, …)` sequence itself — but it reads `policy` from the command metadata (per-subcommand `auth`, else command `auth`, fail-secure to `'linked'`) rather than hardcoding it, so the two can't drift. The policies are deliberately split:

- `start` → `'linked'`: starting a recording consumes resources (a voice connection + a live session), so an unlinked caller is turned away with the standard "link your account" message and never reaches `meetingSurface.start`. Fails **closed** if the directory is unavailable.
- `status` / `stop` → `'public'`: `status` is a local read (no directory call), and `stop` is *de-escalating* — gating it would mean a directory outage could strand a running recording (with no length cap, that's the memory escape hatch). So both work regardless of link state.

If `start` is ever silently downgraded to public, any guild member could drive live voice capture unauthenticated — keep it gated.

**Auto-stop.** `wireDiscordClient` listens for `voiceStateUpdate` (via `createAutoStop`); when the channel being recorded goes empty of non-bot members it **debounces** — schedules a stop after a grace period (`AUTO_STOP_GRACE_MS`) and cancels it if a human is back either on a later event or at fire time (re-check). This avoids a transient client blip or a voice-region failover irreversibly finalizing a live meeting on a single stray "last member left" event. When it does fire it calls `meetingSurface.stop(guildId)` — the same path as `/record stop`. The recorded channel is read from the live session via `meetingSurface.activeSession(guildId)` (which returns the `{ sessionId, voiceChannel }` snapshot opaquely, so `meetingSurface` keeps no `discord.js` dependency), which keeps the head-count honest and makes the listener a no-op once a session is torn down. Each pending timer is **bound to its `sessionId`**, so a timer scheduled for one recording can never terminate a *later* recording that reuses the same guild (a real bug caught in review): a new recording always schedules its own full-grace timer, and a stale one no-ops at fire time. The head-count is computed from **`guild.voiceStates.cache`** (maintained by the `GuildVoiceStates` intent — the same one that powers voice receive), *not* from `channel.members`. `channel.members` resolves each occupant to a `GuildMember` via `guild.members.cache`, which only the privileged `GuildMembers` intent keeps populated; without it that resolution is unreliable and miscounts occupants — including the recorder bot itself, whose member often isn't cached — which is what made auto-stop silently never fire in an early version. Counting voice states avoids that dependency entirely, and the recorder bot is excluded by its own user id (`client.user.id`), so `GuildMembers` is deliberately **not** required.

## Deployment

`meeting` is a private Railway service (`meeting.railway.internal:<PORT>`), like the other services: `platform_auth` consumer keys, `/health`, Dockerfile build (no ffmpeg binary — PyAV bundles its own), GitHub-connected auto-deploy on push to `staging`. It needs AWS credentials (Transcribe, via the standard chain), an `llm` consumer key (for minutes), and its own `CONSUMER_KEYS` set. The bot gets a `meeting` consumer key (`MEETING_API_KEY`) and `MEETING_BASE_URL=http://meeting.railway.internal:<PORT>`; the WebSocket rides Railway's private network. If `MEETING_BASE_URL` is unset the bot boots fine and `/record` reports "not configured" — the feature degrades gracefully.

## Known limitations & live-verify items

- **Concurrent stream limits:** one open Transcribe stream per active speaker, held for the whole meeting — a 10-person call is 10 concurrent streams. Confirm the account's concurrent-stream quota before a large meeting; exceeding it surfaces as a `meeting.audit` warning and that speaker retries, then drops out.
- **AWS session restarts:** Transcribe ends a session on its own (idle timeout, 4h cap). The wrapper reopens on the next audio and offsets the new session's word times by the audio already delivered — unit-tested against a fake, not yet observed against a real timeout.
- **Speaker-timeline anchoring:** a speaker's stream carries only the frames they spoke, so word times are mapped onto meeting time via anchors recorded at each detected silence. The 200 ms tolerance is reasoned from Discord's ~20 ms cadence, not measured live.
- **Meeting length backstop:** the normal end is `/record stop` or auto-stop-on-empty, with a **4h `max_meeting_ms` backstop** so a forgotten meeting can't run indefinitely. Set `MAX_MEETING_MS` to another value, or `None`, to change or disable it.
- **A dropped WebSocket is recoverable, not fatal.** The service holds a disconnected session for `DISCONNECT_GRACE_S` (default 60s) instead of discarding it, and the bot responds to an unexpected `onClose`/`onError` by running the normal finalize (`POST /stop`) rather than announcing a lost meeting — so the minutes still post. The channel only sees "could not be recovered" if that finalize itself fails. Both halves are required: without the server-side hold the bot's `/stop` would 404, and without the bot-side salvage the held session would just expire. The close code is logged on both sides, which is what makes the *next* dropped socket diagnosable.
- **Not built yet:** the in-meeting `/ask` Q&A feature (only the `GET /transcript` hook it will use).

