# meeting — deployment

Service-specific deploy notes. The full platform runbook is [`docs/RAILWAY-DEPLOYMENT.md`](../../../docs/RAILWAY-DEPLOYMENT.md); the cross-component design is [`docs/MEETING-RECORDING.md`](../../../docs/MEETING-RECORDING.md). This page covers only what's different about meeting.

> **Read [Statefulness](#statefulness-read-before-scaling) before changing the replica count.** This is the one service that breaks if you scale it the normal way.

## Shape

| | |
|---|---|
| **Database** | None. No Alembic, no `preDeployCommand`. |
| **Builder** | Dockerfile, build context = **repo root** (needs the uv workspace + `packages/auth`) |
| **Healthcheck** | `/health` |
| **Port** | Binds Railway's injected `${PORT}`; 8004 locally |
| **Exposure** | Private. Reached as `meeting.railway.internal:${PORT}`. |
| **State** | **In process memory.** One session per active meeting. |
| **Dependencies** | `llm` (hard), AWS Transcribe |
| **Binaries** | None. No `ffmpeg` — PyAV bundles its own libraries. |
| **Start command** | Not the plain platform template — carries `--ws-ping-interval 60 --ws-ping-timeout 60`. See below. |

**The WebSocket keepalive is widened on purpose.** uvicorn's defaults (20 s ping, 20 s pong deadline) close a socket whose client is briefly slow to answer — mid-recording, with a clean close code and no error on either side, which is a leading suspect for two production meetings that dropped. `railway.json`'s `startCommand` and the Dockerfile `CMD` both pass `--ws-ping-interval 60 --ws-ping-timeout 60`; 60 s is still well inside any proxy idle timeout. Keep the two in step, and don't normalize this back to the template the other five services use. Railway reads `railway.json`, so that file governs the deploy.

## Variables

| Var | Staging / production | Notes |
|---|---|---|
| `MEETING_ENV` | `staging` / `production` | Anything but `local` turns on the boot check |
| `API_KEY` | a real random string | Provision a real bootstrap key; the boot check only rejects the built-in dev default (see below) |
| `CONSUMER_KEYS` | JSON array | Malformed → boot failure |
| `AWS_REGION` | e.g. `us-east-1` | **Required.** Region for Transcribe streaming. |
| `LLM_BASE_URL` | `http://${{llm.RAILWAY_PRIVATE_DOMAIN}}:${{llm.PORT}}` | **Required.** Use the `${{service.VAR}}` reference form — a literal `${PORT}` resolves to *meeting's own* port, not llm's, and fails silently until the first `/stop`. See [RAILWAY-DEPLOYMENT.md](../../../docs/RAILWAY-DEPLOYMENT.md). |
| `LLM_API_KEY` | a `chat`-scoped `llm_` key | **Required** |
| `REQUEST_TIMEOUT_S` | `60` | Timeout on the `llm` call during `/stop` |
| `MAX_MEETING_MS` | `14400000` (4 h) | Backstop; also Transcribe's per-stream cap |
| `DISCONNECT_GRACE_S` | `60` | How long a disconnected session is held so `POST /stop` can still finalize it |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. Not in `.env.example` — set it explicitly if you want `DEBUG` while diagnosing a live recording. |

AWS credentials come from the standard chain (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`).

Outside `local`, the boot check rejects the built-in `API_KEY` default and empty
`AWS_REGION`, `LLM_BASE_URL`, or `LLM_API_KEY`, listing each failing check. An
absent `API_KEY` falls back to the rejected dev default, but an explicitly empty
value currently passes and disables bootstrap authentication. This validation
gap does not bypass authentication: callers still need a valid key. Provision
real caller credentials and verify access; startup alone does not prove a usable
bootstrap or consumer key, working AWS credentials, or a reachable `llm`.
Configuring `DOCS_BASE_URL` also enables the [catalog credential check](#durable-record-hand-off).

## Deploy order: `llm` first, always

Outside `local`, `meeting` **refuses to boot** without `LLM_BASE_URL` and `LLM_API_KEY`. If `llm` is unreachable during a recording, `/stop` still returns the transcript and PDF with placeholder minutes; see [troubleshooting](#troubleshooting).

1. Deploy `llm`.
2. Mint a `chat`-scoped key named `meeting`:
   ```bash
   uv --project services/llm run llm-keys --name meeting --scopes chat
   ```
3. Append the stderr JSON entry to **llm's** `CONSUMER_KEYS`; redeploy llm.
4. Set the stdout plaintext key as **meeting's** `LLM_API_KEY`, and set `LLM_BASE_URL` to `http://${{llm.RAILWAY_PRIVATE_DOMAIN}}:${{llm.PORT}}` (not a literal `${PORT}`).
5. Deploy `meeting`.

## Consumer keys

No `api_keys` table — keys live in `CONSUMER_KEYS`, and the CLI only *prints*:

```bash
uv --project services/meeting run meeting-keys --name discord-bot --scopes meetings
```

- **stdout** — the plaintext key, shown **once**. Set as the bot's `MEETING_API_KEY`.
- **stderr** — the JSON object. Append to meeting's `CONSUMER_KEYS`.

Then redeploy. Revoking is the reverse: drop the entry, redeploy.

> Always pass `--project` — a bare invocation can resolve another service's CLI in the shared workspace venv. Verify the token starts with `meeting_`.

## The bot degrades gracefully without this service

`MEETING_BASE_URL` is genuinely optional on the discord-bot. Leave it blank and the bot boots normally with `/record` reporting **"not configured"**. That's intended degradation, not a crash — you can run the whole platform without `meeting` deployed.

## Statefulness (read before scaling)

`SessionRegistry` holds active meetings in **process memory**. Two consequences that are easy to get wrong:

**Do not run multiple replicas without sticky routing on `session_id`.** A meeting's WebSocket, its `/transcript` polls, and its `/stop` call must all reach the *same* process. Without stickiness, `/transcript` and `/stop` land on a process that never saw the session and return 404 — visible, but broken. One replica is the correct configuration today.

**Every redeploy kills in-flight meetings.** There is no live-session drain or durable session store. A restart mid-meeting loses the rolling transcript and the open Transcribe streams; the bot's `/record stop` will then 404. It also loses completed text still awaiting catalog acceptance. Deploy when no one is recording and pending [record hand-offs](#durable-record-hand-off) have been accepted; retain the Discord PDF when acceptance fails.

Nothing is ever written to disk, so there's no volume to provision and no cleanup to run.

## Rollback

Use the platform's [revert-PR procedure](../../../docs/RAILWAY-DEPLOYMENT.md#redeploy-and-rollback).
There is no local schema or migration. Completed records already accepted by
documentation-system remain catalog-owned; reverting meeting does not undo them.

The same in-flight caveat applies: a rollback is a restart, and a restart drops active meetings and unaccepted record hand-offs.

## AWS Transcribe operational notes

- **One open stream per active speaker, for the whole meeting.** A 20-person call with everyone talking means up to 20 concurrent streams. Check the account's concurrent-stream quota before a large meeting — exceeding it fails the stream open, not the whole session.
- **Transcribe ends sessions on its own** (idle timeout, and a hard 4 h cap). The wrapper reopens on the next audio and offsets the new session's word times. Verify this with the [live checklist](../README.md#live-verification) when changing audio handling; offline tests use fakes.
- **`MAX_MEETING_MS` defaults to 4 h**, matching Transcribe's cap. Past it, the session stops accepting audio. It's a backstop for a forgotten recording, not the normal path — normal ends are `/record stop` or auto-stop when the voice channel empties.
- **Cost scales with audio duration, once.** Audio is sent to Transcribe exactly once and never replayed, so polling `/transcript` costs nothing. If you see cost scaling with poll frequency, something has reintroduced the replay design — see [ARCHITECTURE.md](ARCHITECTURE.md#the-cost-bug-that-shaped-the-design).

## Privacy posture

Worth stating plainly, because it's a recording service:

- **Audio is never persisted, never mixed, and never returned.** It streams to AWS as transcription input and is dropped.
- **No audio or report artifacts are written to disk.** The session filesystem test uses fake collaborators; the real PDF renderer reads bundled fonts and the logo while rendering.
- **`/stop` deregisters the live session.** Callers must retain the PDF response. With `record` context, finalized text/metadata is handed to documentation-system or held temporarily in memory for retry after the reply; accepted records persist there. See [durable record hand-off](#durable-record-hand-off).
- An abrupt disconnect **holds** the session for `DISCONNECT_GRACE_S` (default 60 s), allowing prompt `/stop` recovery attempts. Starting stop does not cancel grace teardown: a session still registered at expiry is discarded, which can interrupt an unfinished transcript flush. See the [disconnect contract](API.md#disconnect).

## Troubleshooting

- **Container dies at boot.** A non-`local` [configuration check](#variables) failed — the error lists failing variables. Or `CONSUMER_KEYS` isn't a JSON **array**.
- **`/record stop` returns 404.** The session isn't in the registry. Most likely the service restarted mid-meeting (a deploy), or `/stop` was already called, or requests are being load-balanced across replicas.
- **`/stop` hangs ~5 seconds then returns a short transcript.** The client never sent `{"end_of_audio": true}`, so the drain barrier timed out. Check the bot's voice surface — a warning is logged on this path.
- **Minutes come back as `(minutes unavailable: LLM service error)`.** This is the symptom of an `llm` problem — **not** an error status. `LlmUnavailable` is caught in `pipeline/minutes.py` and degrades to placeholder minutes, so `/stop` still returns **200** with a PDF; only the summary/decisions/action-items are empty. Don't go looking for a 502. Check `LLM_BASE_URL` resolves on the private network, that meeting's `LLM_API_KEY` is in llm's `CONSUMER_KEYS` with the `chat` scope, and the `LLM unavailable, returning placeholder minutes` warning in the logs.
- **Transcript is empty but audio was flowing.** Usually AWS credentials or region — Transcribe stream opens fail silently from the client's perspective. Check the logs for stream-open errors.
- **Speakers' words are interleaved at wrong times.** Check timeline anchoring with realistic audio pacing and silence gaps; see the [live verification checklist](../README.md#live-verification).
- **Everything green but `/record` says "not configured".** That's the *bot*, not this service — `MEETING_BASE_URL` is unset on the discord-bot.

## Durable record hand-off

Set `DOCS_BASE_URL` to documentation-system's private Railway URL and
`DOCS_API_KEY` to an issued key named for the meeting service with
`meetings:write`. Outside local, configuring the URL without the key fails at
boot. `RECORD_HANDOFF_HOLD_S` defaults to 1800 seconds. If hand-off expires,
admins investigate `meeting.record_handoff_failed` by meeting ID; retain the
Discord PDF. Pending text is memory-only until accepted by documentation-system.
Deploy the catalog migration and writer configuration first; see the
[shared rollout](../../../docs/RAILWAY-DEPLOYMENT.md#meeting-record-persistence).
