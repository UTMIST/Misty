# meeting — architecture

Why this service is shaped the way it is. For *what it does* and how to run it, see the [README](../README.md); for *how to change it*, see [CONTRIBUTING.md](CONTRIBUTING.md). For how `/record` splits across the bot and this service, see [`docs/MEETING-RECORDING.md`](../../../docs/MEETING-RECORDING.md).

## The service that breaks the rules

Every other service on the platform is stateless or state-in-Postgres, and every bot command is a "neutral command" that runs identically on Discord and in the web playground. `meeting` breaks both, deliberately:

- **It holds state in process memory.** One `MeetingSession` per active meeting, for the meeting's duration.
- **It has no playground equivalent.** Voice capture needs the real Discord surface.

Both fall out of one fact: a live meeting *is* a long-lived stateful thing. There's no way to model "an open audio stream to AWS, per speaker, for 45 minutes" as a stateless request/response. The honest choice was to accept the statefulness and bound it — nothing persists, nothing survives a restart, and the constraints are documented loudly rather than papered over.

## Layering

```
src/contracts.py         Pydantic wire types (Minutes, Segment, TranscriptView, StopResponse)
       ▲
src/api/routers/         FastAPI — HTTP routes + the WebSocket handler
src/api/wiring.py        THE only place fakes become real
       │  injects deps into
       ▼
src/sessions.py          SessionRegistry / MeetingSession — all lifecycle logic,
       │                 zero AWS/network imports
       ├─► src/audio/decoder.py     Opus → PCM (PyAV, per-speaker, stateful)
       ├─► src/stt/transcribe.py    Amazon Transcribe streaming wrapper
       └─► src/pipeline/            transcript assembly → minutes (via llm) → PDF
```

The unusual part is `src/sessions.py`. It's the heart of the service and it imports **nothing from AWS or HTTP** — no `boto3`, no `httpx`, no FastAPI. Every collaborator that touches the outside world — the transcriber factory, the audio decoder, the report builder, even the clock — arrives through an injected `deps` dict:

```python
deps = {
    "make_transcription_stream": lambda: create_transcription_stream(region),
    "audio": AudioAdapter(),
    "report_builder": _build_report_builder(),
    "now": lambda: datetime.now(timezone.utc),
    "max_meeting_ms": settings.max_meeting_ms,
}
```

`src/api/wiring.py` is the only module that builds that dict with real implementations. Everything else — the entire test suite included — passes fakes. That's why the suite needs no AWS credentials, no network, and no audio files.

The one direct import that isn't injected is `from src.pipeline.transcript import assemble_transcript` (`sessions.py:31`), used when finalizing. It's pure text assembly with no I/O, so it doesn't compromise the offline property — but it does mean "sessions.py imports nothing from the pipeline" is not literally true. The property to preserve is **no I/O and no vendor SDK**, not zero pipeline imports.

## The cost bug that shaped the design

The docstring at the top of `sessions.py` describes a design that was **replaced**, and understanding it explains most of the current code.

The original design buffered each speaker's PCM and re-transcribed the whole accumulated buffer on every `/transcript` poll, and again at `/stop`. That meant:

- **AWS cost grew with the square of meeting length.** k polls re-sent roughly k/2 copies of the audio.
- **Each poll got progressively slower**, since each one transcribed more.
- **Every speaker's PCM had to be retained in memory** for the whole meeting.

The current design opens **one persistent Transcribe stream per speaker** on their first frame and feeds it as audio arrives. Consequences, all of them load-bearing:

- **Audio is sent exactly once and never replayed.** Each decoded PCM chunk goes straight to that speaker's stream and is dropped.
- **`/transcript` is a free read.** It returns what the streams have already finalized — no AWS call, no cost, no latency. Poll it as often as you like.
- **Nothing retains the audio**, because nothing needs to replay it.

If you're ever tempted to buffer audio "just in case," this is the reason not to.

## Speaker-timeline anchoring

The hardest correctness problem in the service.

Each speaker's Transcribe stream carries **only the frames that speaker spoke** — silence is never sent. So Transcribe's word timings are relative to *that speaker's own audio*, not to meeting time. Naively concatenating them would put every speaker's first word at 0 ms.

The fix: `_SpeakerBuffer` records an **anchor** at each detected silence gap, mapping "this many ms into your stream" → "this many ms into the meeting." `_to_meeting_ms` then maps stream-relative word times back onto the meeting timeline.

Two tuning constants govern how words become segments:

| Constant | Value | Role |
|---|---|---|
| `_SEGMENT_GAP_MS` | 1500 | A pause longer than this ends an utterance. Conversational pauses between sentences run a few hundred ms, so 1.5 s is past "drawing breath" without splitting mid-thought. |
| `_MAX_SEGMENT_MS` | 5000 | Hard cap on one segment with no pause at all. Segments sort as whole blocks, so this bounds how far an overlapping speaker can be displaced — an interjection can never appear more than ~5 s late. |

`_MAX_SEGMENT_MS` was measured, not guessed: at 10 s, an interjection inside the first 10 s of someone's turn still sorted after their whole block, which is precisely the bug it exists to fix.

> The 200 ms gap tolerance used for anchoring is reasoned from Discord's ~20 ms frame cadence but has **not** been measured against a live call. See [Live-verify caveats](#live-verify-caveats).

## The end-of-audio barrier

`POST /stop` cannot simply finalize immediately. The bot may still have frames in flight, and cutting the streams early truncates the tail of the transcript — the last thing anyone said, which is usually the action items.

The protocol solves it with an explicit signal. After the bot stops recording and has forwarded every captured frame, it sends `{"end_of_audio": true}`. Because the socket delivers **in order**, receiving that frame proves every audio frame before it has already been fed. `stop()` waits for that barrier before finalizing.

The fallback matters as much as the signal: if it never arrives — an older bot, a crash, a dropped socket — `/stop` proceeds anyway after `AUDIO_DRAIN_TIMEOUT_S` (5 s) and logs a warning. A missing signal degrades the transcript tail; it must never hang `/stop`.

## stop() vs discard()

Two teardown paths, and picking the wrong one is expensive:

| | `stop()` | `discard()` |
|---|---|---|
| Trigger | `POST /meetings/{id}/stop` | `DISCONNECT_GRACE_S` elapsing after a disconnect with no stop |
| Waits for end-of-audio | Yes (5 s cap) | No |
| Flushes Transcribe | Yes | Aborts the streams |
| Generates minutes | Yes — **blocking HTTP call to `llm`** | No |
| Renders PDF | Yes | No |
| Returns | `StopResponse` | Nothing |

`discard()` deliberately skips the finalize pipeline: paying for an LLM call plus a PDF render that nobody will read is pure waste.

**A disconnect no longer routes straight to `discard()`.** It used to, and that was a data-loss bug rather than a saving: the transcript is already assembled in memory when the socket dies, so destroying it immediately meant a client whose connection dropped had nothing left to claim — its follow-up `POST /stop` 404'd. Two fully-transcribed production meetings were lost that way in a single morning. A disconnect now **holds** the session for `DISCONNECT_GRACE_S` (default 60 s) and marks end-of-audio; `discard()` runs only if that window expires unclaimed. The cost saving is preserved — nothing is finalized unless someone actually asks — while a dropped socket becomes recoverable instead of fatal.

The window is short on purpose. A held session keeps its Transcribe streams parked, so this trades a bounded amount of idle AWS state for the ability to recover a meeting; it is not a general-purpose resume mechanism. Reconnecting to a held session is deliberately *not* supported — a second WS connect for a live `session_id` still closes with 1008.

## Statefulness: the operational consequences

`SessionRegistry` holds one `MeetingSession` per active meeting in process memory. Two hard constraints follow:

**Not horizontally scalable as-is.** A session's WebSocket, its `/transcript` polls, and its `/stop` call must all land on the *same* process. Multiple replicas behind a load balancer without sticky routing on `session_id` will send `/transcript` and `/stop` to a process that never saw that session — which returns a clean 404, so the failure is visible rather than silently wrong, but it's still broken.

**Ephemeral by design.** A restart or crash mid-meeting loses everything in flight: the rolling transcript, the open streams, all of it. There is no database, no durable queue, and **nothing is ever written to disk** (there's a test asserting exactly that: `test_session_lifecycle_never_touches_the_filesystem`). If a meeting's minutes matter, the consumer must call `/stop` and persist the response itself — this service retains nothing after replying.

## Concurrency in the WebSocket handler

Three things in `stream_meeting` exist because of specific failures:

- **`session.feed` runs on a thread** (`asyncio.to_thread`). Opus decode is blocking CPU work; running it on the event loop would stall every *other* meeting's connection.
- **A raising `feed` drops one frame, not the meeting.** The broad `except` around it is deliberate — but it is pointedly *not* `except WebSocketDisconnect`, which must still propagate and break the loop.
- **A second connect for an active `session_id` closes cleanly** with 1008 rather than crashing the handler post-accept, and leaves the existing session untouched.

Malformed input is uniformly non-fatal — a meeting should survive a buggy client — but note the asymmetry in *observability*: only the binary path counts drops (`empty_ws_frame`, `malformed_ws_frame`, `feed_raised`). Unparseable JSON and text frames matching neither control shape are silently `continue`d with no counter and no log. A client sending malformed control frames looks identical to one sending none, which is worth knowing when a display name never appears.

## Why Opus decode is per-speaker and in-process

Discord voice delivers **bare Opus packets** with no container or demuxer framing, and Opus decode carries state across packets. Two consequences:

- Decode cannot be a stateless `decode(frame)` shared across speakers. Each speaker gets its **own** `OpusStreamDecoder`, created once and fed that speaker's packets in order for the life of the session.
- Shelling out to ffmpeg does not work. ffmpeg has no demuxer for standalone Opus packets — this failed with exit 234 against real Discord audio. That was the live-integration bug that produced the current design.

Decode runs in-process via PyAV, which bundles its own ffmpeg libraries. **No `ffmpeg` binary is needed anywhere**, and nothing shells out per frame.

## `llm` as the summarization choke point

`meeting` never calls Bedrock. It POSTs the assembled transcript to the `llm` service's `/chat` and parses the completion into `Minutes`. Swapping models, providers, or prompt wording happens in `llm`'s consumer code path here — but the credential and the model catalog stay in one place.

This is a genuine hard dependency: `verify_production_secrets()` requires `LLM_BASE_URL` and `LLM_API_KEY` outside `local`, so `meeting` refuses to boot without them. Deploy `llm` first.

## Live-verify caveats

Several integration assumptions are unit-tested against fakes but **not yet confirmed against real Discord audio or a real AWS account**:

- **Speaker-timeline anchoring** — the 200 ms gap tolerance is reasoned from Discord's frame cadence, not measured on a live call.
- **AWS session restarts** — Transcribe ends a streaming session on its own (idle timeout, 4 h cap). The wrapper reopens on the next audio and offsets the new session's word times by the audio already delivered. Tested against a fake, never observed against a real timeout.
- **Concurrent stream limits** — one open Transcribe stream per active speaker, for the whole meeting. Check the account's concurrent-stream quota before a large meeting.
- **Transcribe streaming generally** — the suite exercises `src/stt/transcribe.py` against a fake client only. Realistic audio pacing is needed to validate end-to-end.
