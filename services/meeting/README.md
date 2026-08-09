# meeting

Stateful HTTP + WebSocket service that ingests live per-speaker Discord voice audio, transcribes it via Amazon Transcribe streaming, and produces a rolling transcript plus end-of-meeting minutes and a PDF for UTMIST's internal consumers (the Discord bot).

## What this service does

A Discord bot opens one WebSocket per meeting and streams each speaker's raw Opus audio frame-by-frame as it arrives from voice channels. `meeting` decodes each frame to PCM in-process (one stateful PyAV Opus decoder per speaker) and streams it straight into that speaker's **persistent Amazon Transcribe session**, held open for the whole meeting. Audio is sent once and never replayed, so the rolling transcript is a free read of what those sessions have finalized so far. When the meeting ends (`POST /stop` or a client disconnect), it assembles the final transcript, calls the `llm` service to summarize decisions/action items into structured minutes, and renders a PDF — returning transcript, minutes, and PDF. Meeting audio is never mixed, persisted, or returned.

Two design choices shape everything:

- **Stateful, but ephemeral.** Unlike `llm`, this service *does* hold state — one `MeetingSession` per active meeting, alive only in process memory for the meeting's duration (see the STATEFULNESS note below). There is no database and nothing survives a restart.
- **`llm` as the summarization choke point.** `meeting` never calls Bedrock directly. It POSTs the assembled transcript to the `llm` service's `/chat` endpoint (via `LLM_BASE_URL`/`LLM_API_KEY`) and parses the completion into `Minutes`. Swapping models, providers, or prompts happens in `llm`, not here.

## Quick start

Prerequisites: Python 3.11+ and [uv](https://github.com/astral-sh/uv). No `ffmpeg` binary is needed — Opus decode runs in-process via PyAV, which bundles its own ffmpeg libraries, and nothing shells out. No Docker or database needed for local dev.

```bash
# 0. From the repo root, enter the service directory (all commands below run here)
cd services/meeting

# 1. Copy environment config
cp .env.example .env

# 2. Install dependencies (including dev tools)
uv sync --extra dev

# 3. Start the API server
uv run uvicorn src.api.app:app --reload --port 8004
```

> The repo is a single [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/) (root `pyproject.toml` with `[tool.uv.workspace] members = ["services/*", "packages/*"]`, one root `uv.lock`). meeting depends on the shared `platform-auth` package (`[tool.uv.sources] platform-auth = { workspace = true }`) but the commands above are unchanged — `uv sync` and `uv run pytest` work exactly as shown when run from this directory.

The API is now at `http://localhost:8004`. Interactive Swagger UI is at `http://localhost:8004/docs`; the machine-readable schema is at `http://localhost:8004/openapi.json` (HTTP routes only — the WebSocket route isn't represented in OpenAPI).

The default dev bootstrap key is `dev-api-key-change-me` (set in `.env`, carries the `admin` wildcard scope). Pass it as `X-API-Key` on the HTTP endpoints, or `?key=` on the WebSocket.

**Ports:** this service runs on **8004** (team-tracking 8000, documentation-system 8001, llm 8002, verification 8003) — chosen to avoid colliding when running the services locally. It has no database, so it claims no Postgres host port.

### Environment tiers (`MEETING_ENV`)

`MEETING_ENV` declares which environment this instance represents: `local` (default), `staging`, or `production`. Outside `local`, `create_app()` calls `verify_production_secrets()` and **refuses to start** unless all four of `API_KEY` (overridden from the dev default), `AWS_REGION`, `LLM_BASE_URL`, and `LLM_API_KEY` are set to real values — the error names every one that isn't. A misconfigured deploy dies at boot, not on first request.

### Configuration

All settings load from the environment (`src/config.py`, `.env` in dev):

| Var | Default | Purpose |
|-----|---------|---------|
| `MEETING_ENV` | `local` | Environment tier (`local` / `staging` / `production`). |
| `API_KEY` | `dev-api-key-change-me` | Env-bootstrap key; carries `admin` scope. Must be overridden outside `local`. |
| `CONSUMER_KEYS` | `""` | JSON array of per-consumer keys (see auth model). |
| `AWS_REGION` | `""` | Region for Amazon Transcribe streaming. Standard AWS credential chain also reads `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. Required outside `local`. |
| `LLM_BASE_URL` | `""` | Base URL of the `llm` service (e.g. `http://llm.railway.internal:8002` in prod, `http://localhost:8002` in dev). Required outside `local`. |
| `LLM_API_KEY` | `""` | Consumer key this service presents to `llm`'s `X-API-Key`. |
| `REQUEST_TIMEOUT_S` | `60` | Per-request timeout when calling `llm` (seconds). |
| `MAX_MEETING_MS` | `14400000` (4h) | Safety backstop: a session stops accepting audio past this. The normal end is `/record stop` or auto-stop-on-empty; this only bounds a forgotten meeting. 4h is also AWS Transcribe's per-stream cap. |
| `DISCONNECT_GRACE_S` | `60` | How long a session survives an abrupt WebSocket disconnect. The transcript is already assembled server-side, so within this window `POST /stop` still turns a dropped recording into minutes instead of losing it. `0` discards immediately. |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. Applied at boot by `configure_logging`. INFO is the default because the volume is a startup note plus per-meeting summaries, not per-request chatter — the alternative is losing them entirely. |

## Auth model

Auth is scoped API keys via the shared `platform_auth` package, matching `llm` — **DB-free**, keys seeded from config, no `api_keys` table.

- Every HTTP request needs `X-API-Key`; the WebSocket takes the same key as a `?key=` query param (see below). A key is either the shared bootstrap env key (`API_KEY`, scope: `admin`) or a per-consumer key seeded from `CONSUMER_KEYS`.
- **`CONSUMER_KEYS` is a JSON array**, each entry `{"name", "prefix", "key_hash", "scopes"}`. At boot, the key store (`src/key_store.py`) parses it into an in-memory store satisfying `platform_auth`'s `ApiKeyStore` protocol — a malformed `CONSUMER_KEYS` fails fast at startup.
- Keys use the **`meeting_` envelope** (`meeting_<prefix>_<secret>`). There is a single scope, `meetings`, covering every **protected** endpoint (HTTP and WS) — this service has one internal consumer class (the Discord bot), so separate read/write/stream scopes were a deliberate simplification. The one exception is `/health`, which is unauthenticated (see below).

### Minting consumer keys

Use the `meeting-keys` CLI. It prints the plaintext key **once** to stdout and the `CONSUMER_KEYS` JSON entry to stderr — it does not (and cannot) write to any store:

```bash
uv run meeting-keys --name discord-bot --scopes meetings
# stdout: meeting_<prefix>_<secret>   (the key — give it to the consumer, shown ONCE)
# stderr: {"name": "discord-bot", "prefix": "...", "key_hash": "$argon2id$...", "scopes": ["meetings"]}
```

Append the printed JSON object to the service's `CONSUMER_KEYS` array and redeploy. To **revoke** a key, drop its entry from `CONSUMER_KEYS` and redeploy — there is no revoke command because there is no database.

## API at a glance

Every **protected** endpoint (HTTP and WS) requires `X-API-Key` + the `meetings` scope (the `admin` wildcard also satisfies it) — `/health` is the unauthenticated exception.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe → `{"status": "ok"}`. No auth. |
| GET | `/meetings/{id}/transcript` | Poll the rolling transcript for an active session → `TranscriptView`. |
| POST | `/meetings/{id}/stop` | End the session: close the Transcribe streams, finalize transcript, generate minutes + PDF, tear the session down → `StopResponse`. |
| WS | `/meetings/{id}/stream` | Live audio ingest + control channel (see below). |

`{id}` (`session_id`) must match `^[A-Za-z0-9_-]{1,64}$`. (The service writes nothing to disk, so this is input hygiene rather than path safety — see `test_session_lifecycle_never_touches_the_filesystem`.) A `session_id` unknown to the in-memory registry (never started, already stopped, or the process restarted) returns **404** on `/transcript` and `/stop`.

**`GET /meetings/{id}/transcript` → `TranscriptView`:** `{"segments": [{"speaker", "start_ms", "text"}, ...]}`. Reads whatever each speaker's live Transcribe stream has finalized so far. Polling is **free** — no AWS call, no audio re-sent — so it can be polled as often as you like. It is a cumulative view, not a diff/append stream.

**`POST /meetings/{id}/stop` → `StopResponse`:** `{"transcript", "minutes": {"title", "summary", "decisions": [...], "action_items": [...]}, "pdf_b64"}`. `pdf_b64` is the base64-encoded minutes PDF. **No meeting audio is returned** — it exists only as transcription input and is never mixed or persisted.

### WebSocket: `WS /meetings/{id}/stream`

The wire contract the Discord-bot side mirrors.

**Connect:** `ws://.../meetings/{session_id}/stream?key=<consumer-key>&guild_id=<guild-id>`

- `key` (query param, preferred): validated against the key store before the socket is accepted; on failure the connection is rejected during the handshake. If `key` is omitted from the query string, the server instead accepts the socket and requires the *first* message to be a text frame `{"key": "..."}` — if that's missing, malformed, or invalid, the socket is closed with WS code **1008** (policy violation) before any audio is processed.
- `guild_id` (optional): passed to session creation; if omitted, `session_id` is used as the guild_id.
- An invalid `session_id` (fails the regex above) closes with code 1008 before a session is created.

**Once authenticated, two kinds of message are accepted — control (text) and audio (binary):**

1. **Control** (WebSocket text frame, UTF-8 JSON). Two shapes:

   `{"speaker_id": "<id>", "display_name": "<name>"}` — registers/updates the display name shown for a speaker_id. Send this whenever a speaker's identity becomes known (e.g. a Discord user joins voice).

   `{"end_of_audio": true}` — send **once**, after the bot stops recording and has forwarded every frame it captured. The socket delivers in order, so the server treats this as proof that all audio has arrived, and `POST /stop` waits for it before finalizing. Without it (an older bot, a crash, a dropped socket) `/stop` proceeds after `sessions.AUDIO_DRAIN_TIMEOUT_S` (5s) and logs a warning; the tail of the transcript may then be short, which is the failure this signal exists to prevent.

   Unknown/malformed text frames are ignored, not fatal.
2. **Audio** (WebSocket binary frame), one raw Opus packet per frame, framed as:

   ```text
   [2 bytes  big-endian uint16]  speaker_id_len
   [speaker_id_len bytes, UTF-8] speaker_id
   [8 bytes  big-endian uint64]  ts_ms
   [remaining bytes]             raw Opus packet payload
   ```

   i.e. a length-prefixed speaker id, then an 8-byte millisecond timestamp, then the raw Opus payload with no further framing. Truncated/undersized frames are dropped, not fatal.

**Disconnect:** if the client disconnects (or errors) without a preceding `POST /stop`, the server tears the session down itself (`session.discard()`) — a lightweight cleanup (abort each speaker's Transcribe stream, deregister) that deliberately skips the finalize pipeline (transcription flush, minutes, PDF), since that involves a blocking `llm` HTTP call not worth paying for on an abrupt drop.

## Private deploy (Railway)

This service is not exposed publicly. It sits on Railway's private network, addressed by internal consumers (the Discord bot) as `meeting.railway.internal:<PORT>` — mirroring how `llm` is reached internally. `railway.json` builds from `Dockerfile` (Dockerfile builder, build context = repo root), health-checks `/health`, and starts uvicorn bound to Railway's injected `${PORT}`.

## STATEFULNESS (read before deploying more than one instance)

Unlike `llm`, **this service holds state in process memory**: `SessionRegistry` (`src/sessions.py`) keeps one `MeetingSession` per active meeting — one live Transcribe stream per speaker plus the rolling transcript they produce — for the lifetime of that meeting. The audio itself is not held: each chunk goes straight to AWS and is dropped. This means:

- **Not horizontally scalable as-is.** A given `session_id`'s WebSocket, `/transcript` polls, and `/stop` call must all land on the *same* process/instance. Running multiple replicas behind a load balancer without sticky routing on `session_id` will misroute `/transcript` and `/stop` calls to a process that never saw that session.
- **Ephemeral by design — nothing is persisted.** A process restart or crash mid-meeting loses all in-flight state: buffered audio, the rolling transcript, everything. There is no database and no durable queue backing the session, and **nothing is ever written to disk**. If a meeting's minutes/PDF matter, the consumer (the bot) must call `POST /stop` and persist the response itself — this service will not retain it after replying.

## Live-verify caveats (flagged for sub-plan 3 integration, not this task)

A few integration assumptions are baked in and documented in-code, but need confirming against the real Discord bot / AWS account rather than unit tests:

- **Speaker-timeline anchoring** (`src/sessions.py`): a speaker's stream carries only the frames they spoke, so Transcribe's stream-relative word times are mapped back onto meeting time via anchors recorded at each detected silence. The 200 ms gap tolerance is reasoned from Discord's ~20 ms frame cadence, not yet measured against a live call.
- **AWS session restarts:** Transcribe ends a streaming session on its own (idle timeout, 4h cap). The wrapper reopens on the next audio and offsets the new session's word times by the audio already delivered — unit-tested against a fake, not yet observed against a real timeout.
- **Concurrent stream limits:** one open Transcribe stream per active speaker, for the whole meeting. Check the account's concurrent-stream quota before a large meeting.
- **Amazon Transcribe streaming** needs real AWS credentials (`AWS_REGION` + the standard credential chain) and realistic audio pacing to validate end-to-end; the test suite exercises `src/stt/transcribe.py` against a fake client only, never a live Transcribe session.

## Testing

```bash
uv run pytest
```

No Docker, no network, no AWS credentials — the Transcribe/LLM clients are faked via `app.dependency_overrides` and injected `deps`.

```bash
uv run ruff check .
uv run ruff format .
```

## Status

v0.1: stateful per-meeting session registry, live per-speaker audio ingest over WebSocket with binary frame parsing, persistent per-speaker Transcribe streams behind a free-to-poll rolling transcript, `llm`-backed minutes generation, and PDF rendering — all in-memory/ephemeral, with no ffmpeg binary and no disk use.

**Not implemented (by design):**

- **Persistence** — no database, no durable session store. A process restart loses all in-flight meetings.
- **Horizontal scaling** — one process must own a given session end-to-end; no shared session state across replicas.

## Documentation

- [docs/API.md](docs/API.md) — consumer-facing endpoint reference: request/response shapes, errors, curl examples
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — contributor orientation: why the service is shaped this way, boundaries, trade-offs
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) — task walkthroughs and the pre-push checklist
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — deploy shape, variables, key provisioning, troubleshooting
