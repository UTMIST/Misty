# meeting

Stateful HTTP + WebSocket service that ingests live per-speaker Discord voice audio, transcribes it via Amazon Transcribe streaming, and produces a rolling transcript plus end-of-meeting minutes and a PDF for UTMIST's internal consumers (the Discord bot).

## What this service does

A Discord bot opens one WebSocket per meeting and streams each speaker's raw Opus audio frame-by-frame as it arrives from voice channels. `meeting` decodes each frame to PCM in-process (one stateful PyAV Opus decoder per speaker) and streams it straight into that speaker's **persistent Amazon Transcribe session**, held open for the whole meeting. Audio is sent once and never replayed, so the rolling transcript is a free read of what those sessions have finalized so far. On `POST /stop`, it assembles the final transcript, calls the `llm` service for structured minutes, and renders a PDF — returning transcript, minutes, and PDF. Summarization has [input and parsing limits](docs/API.md#post-meetingssession_idstop). A client disconnect instead holds the session for recovery (see below), without automatically producing artifacts. Meeting audio is never mixed, persisted, or returned.

Two design choices shape everything:

- **Stateful, but ephemeral.** Unlike `llm`, this service *does* hold state — one `MeetingSession` per active meeting, alive only in process memory for the meeting's duration (see the STATEFULNESS note below). Live sessions have no database and do not survive a restart. Completed records accepted by documentation-system do survive; see the [hand-off contract](docs/API.md#durable-record-context).
- **`llm` as the summarization choke point.** `meeting` never calls Bedrock directly. It POSTs bounded transcript text to the `llm` service's `/chat` endpoint (via `LLM_BASE_URL`/`LLM_API_KEY`) and parses the completion into `Minutes`. Model/provider configuration lives in `llm`; meeting owns the minutes and Q&A prompts in `src/pipeline/` (see the [prompt walkthrough](docs/CONTRIBUTING.md#walkthrough-change-the-minutes-prompt-or-pdf)).

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

`MEETING_ENV` declares which environment this instance represents: `local` (default), `staging`, or `production`. Outside `local`, `create_app()` calls `verify_production_secrets()`: it rejects the built-in `API_KEY` default and empty `AWS_REGION`, `LLM_BASE_URL`, or `LLM_API_KEY`. An explicitly empty `API_KEY` currently passes this guard, so startup alone does not establish usable caller credentials. Provision real keys; see the [boot-check limits](docs/DEPLOYMENT.md#variables) and conditional catalog credential check there.

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
| `DOCS_BASE_URL` | `""` | documentation-system URL for durable meeting hand-off. |
| `DOCS_API_KEY` | `""` | Consumer key with `meetings:write`; required outside local when `DOCS_BASE_URL` is configured. |
| `RECORD_HANDOFF_HOLD_S` | `1800` | In-memory retry window after a failed catalog hand-off. |
| `MAX_MEETING_MS` | `14400000` (4h) | Safety backstop: a session stops accepting audio past this. The normal end is `/record stop` or auto-stop-on-empty; this only bounds a forgotten meeting. 4h is also AWS Transcribe's per-stream cap. |
| `DISCONNECT_GRACE_S` | `60` | Delay before tearing down a session after an abrupt WebSocket disconnect. Call `/stop` promptly to attempt recovery; starting stop does not cancel the timer. `0` discards immediately. See the [disconnect contract](docs/API.md#disconnect). |
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
| POST | `/meetings/{id}/qna` | Ask about finalized speech in the last 15 minutes → `QnaResponse`. See the [Q&A contract](docs/API.md#post-meetingssession_idqna). |
| POST | `/meetings/{id}/stop` | End the session: close the Transcribe streams, finalize transcript, generate minutes + PDF, tear the session down → `StopResponse`. |
| WS | `/meetings/{id}/stream` | Live audio ingest + control channel (see below). |

`{id}` (`session_id`) must match `^[A-Za-z0-9_-]{1,64}$`. (The service writes nothing to disk, so this is input hygiene rather than path safety — see `test_session_lifecycle_never_touches_the_filesystem`.) A `session_id` unknown to the in-memory registry (never started, already stopped, or the process restarted) returns **404** on `/transcript`, `/qna`, and `/stop`.

**`GET /meetings/{id}/transcript` → `TranscriptView`:** `{"segments": [{"speaker", "start_ms", "text"}, ...]}`. Reads whatever each speaker's live Transcribe stream has finalized so far. Polling is **free** — no AWS call, no audio re-sent — so it can be polled as often as you like. It is a cumulative view, not a diff/append stream.

**`POST /meetings/{id}/stop` → `StopResponse`:** `{"transcript", "minutes": {"title", "summary", "decisions": [...], "action_items": [...]}, "pdf_b64", "record"}`. `record` carries durable-save status when requested (see [the contract](docs/API.md#durable-record-context)). `pdf_b64` is the base64-encoded minutes PDF. **No meeting audio is returned** — it exists only as transcription input and is never mixed or persisted.

### WebSocket: `WS /meetings/{id}/stream`

The wire contract the Discord-bot side mirrors.

**Connect:** `ws://.../meetings/{session_id}/stream?key=<consumer-key>&guild_id=<guild-id>`

- `key` (query param, preferred): validated against the key store before the socket is accepted; on failure the connection is rejected during the handshake. If `key` is omitted from the query string, the server instead accepts the socket and requires the *first* message to be a text frame `{"key": "..."}` — if that's missing, malformed, or invalid, the socket is closed with WS code **1008** (policy violation) before any audio is processed.
- `guild_id` (optional): passed to session creation; if omitted, `session_id` is used as the guild_id.
- An invalid `session_id` (fails the regex above) closes with code 1008 before a session is created.

**Once authenticated, two kinds of message are accepted — control (text) and audio (binary):**

1. **Control** (WebSocket text frame, UTF-8 JSON). Two shapes:

   `{"speaker_id": "<id>", "display_name": "<name>"}` — registers/updates the display name shown for a speaker_id. Send this whenever a speaker's identity becomes known (e.g. a Discord user joins voice).

   `{"end_of_audio": true}` — send **once**, after the bot stops recording and has forwarded every frame it captured. The socket delivers in order, so the server treats this as proof that all audio has arrived, and `POST /stop` waits for it before finalizing. If neither this signal nor a disconnect has been observed, `/stop` proceeds after `sessions.AUDIO_DRAIN_TIMEOUT_S` (5s) and logs a warning; the tail of the transcript may then be short. An observed disconnect marks audio complete and skips that wait.

   Invalid JSON and unrecognized control shapes are ignored. Send string `speaker_id` and `display_name` values: malformed control values are not fully validated and can end the handler (see the [current limitation](docs/API.md#messages-the-server-accepts)).
2. **Audio** (WebSocket binary frame), one raw Opus packet per frame, framed as:

   ```text
   [2 bytes  big-endian uint16]  speaker_id_len
   [speaker_id_len bytes, UTF-8] speaker_id
   [8 bytes  big-endian uint64]  ts_ms
   [remaining bytes]             raw Opus packet payload
   ```

   i.e. a length-prefixed speaker id, then an 8-byte millisecond timestamp, then the raw Opus payload with no further framing. `ts_ms` is nonnegative elapsed time since recording start, including silence, using one shared monotonic clock for all speakers; it is neither Unix time nor accumulated speaking time. Truncated/undersized frames are dropped, not fatal.

**Disconnect:** if the client disconnects (or errors) without a preceding `POST /stop`, the server holds the session for `DISCONNECT_GRACE_S` (default 60 s) and marks audio complete. Call `/stop` promptly to attempt recovery. Starting stop does not cancel the grace timer: if the session is still registered at expiry, `session.discard()` aborts its streams and deregisters it, potentially interrupting an unfinished transcript flush. Discard itself produces no artifacts. `DISCONNECT_GRACE_S=0` discards immediately; see the [disconnect contract](docs/API.md#disconnect).

## Private deploy (Railway)

This service is not exposed publicly. It sits on Railway's private network, addressed by internal consumers (the Discord bot) as `meeting.railway.internal:<PORT>` — mirroring how `llm` is reached internally. `railway.json` builds from `Dockerfile` (Dockerfile builder, build context = repo root), health-checks `/health`, and starts uvicorn bound to Railway's injected `${PORT}`.

## STATEFULNESS (read before deploying more than one instance)

Unlike `llm`, **this service holds state in process memory**: `SessionRegistry` (`src/sessions.py`) keeps one `MeetingSession` per active meeting — one live Transcribe stream per speaker plus the rolling transcript they produce — for the lifetime of that meeting. The audio itself is not held: each chunk goes straight to AWS and is dropped. This means:

- **Not horizontally scalable as-is.** A given `session_id`'s WebSocket, `/transcript` polls, and `/stop` call must all land on the *same* process/instance. Running multiple replicas behind a load balancer without sticky routing on `session_id` will misroute `/transcript` and `/stop` calls to a process that never saw that session.
- **Live sessions are ephemeral.** A process restart or crash loses the rolling transcript and open streams. There is no database or durable queue backing the session, and **nothing is written to disk**. `/stop` deregisters the session; callers must retain the PDF response. With `record` context, finalized text/metadata is handed to documentation-system or held temporarily in memory for retry after the reply. A restart loses unaccepted hand-offs; accepted records survive in the catalog. See the [retention contract](docs/API.md#durable-record-context).

## Live verification

Offline tests use fake Transcribe and LLM clients. For a deployment that changes
audio handling, verify these integration assumptions with the real Discord bot
and AWS account, and record the results in the release PR:

- **Speaker-timeline anchoring** (`src/sessions.py`): a speaker's stream carries only the frames they spoke, so Transcribe's stream-relative word times are mapped back onto meeting time via anchors recorded at each detected silence. Check that the 200 ms gap tolerance preserves speaker timing with realistic Discord audio pacing.
- **AWS session restarts:** Transcribe ends a streaming session on its own (idle timeout, 4h cap). Verify that the wrapper reopens on the next audio and offsets the new session's word times by the audio already delivered.
- **Concurrent stream limits:** one open Transcribe stream per active speaker, for the whole meeting. Check the account's concurrent-stream quota before a large meeting.
- **Amazon Transcribe streaming** needs real AWS credentials (`AWS_REGION` + the standard credential chain). Use an authorized live test to verify the full recording and finalization flow; keep repository tests offline.

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

Stateful per-meeting session registry, live per-speaker audio ingest over WebSocket with binary frame parsing, persistent per-speaker Transcribe streams behind a free-to-poll rolling transcript, `llm`-backed minutes generation, and PDF rendering. Live state and pending hand-offs stay in memory; completed text records are persisted by documentation-system. No ffmpeg binary or runtime disk writes.

**Not implemented (by design):**

- **Durable live sessions** — no database or durable session store here. A process restart loses all in-flight meetings and unaccepted hand-offs.
- **Horizontal scaling** — one process must own a given session end-to-end; no shared session state across replicas.

## Documentation

- [docs/API.md](docs/API.md) — consumer-facing endpoint reference: request/response shapes, errors, curl examples
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — contributor orientation: why the service is shaped this way, boundaries, trade-offs
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) — task walkthroughs and the pre-push checklist
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — deploy shape, variables, key provisioning, troubleshooting
