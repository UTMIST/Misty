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
| `API_KEY` | a real random string | **Required** by the boot check |
| `CONSUMER_KEYS` | JSON array | Malformed → boot failure |
| `AWS_REGION` | e.g. `us-east-1` | **Required.** Region for Transcribe streaming. |
| `LLM_BASE_URL` | `http://${{llm.RAILWAY_PRIVATE_DOMAIN}}:${{llm.PORT}}` | **Required.** Use the `${{service.VAR}}` reference form — a literal `${PORT}` resolves to *meeting's own* port, not llm's, and fails silently until the first `/stop`. See [RAILWAY-DEPLOYMENT.md](../../../docs/RAILWAY-DEPLOYMENT.md). |
| `LLM_API_KEY` | a `chat`-scoped `llm_` key | **Required** |
| `REQUEST_TIMEOUT_S` | `60` | Timeout on the `llm` call during `/stop` |
| `MAX_MEETING_MS` | `14400000` (4 h) | Backstop; also Transcribe's per-stream cap |
| `DISCONNECT_GRACE_S` | `60` | How long a disconnected session is held so `POST /stop` can still finalize it |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. Not in `.env.example` — set it explicitly if you want `DEBUG` while diagnosing a live recording. |

AWS credentials come from the standard chain (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`).

**The boot check requires all four of `API_KEY`, `AWS_REGION`, `LLM_BASE_URL`, and `LLM_API_KEY`** outside `local`, and the error names every one that's missing. It does not verify that the credentials work or that `llm` is reachable — those surface on a real recording.

## Deploy order: `llm` first, always

This is a hard dependency, not a soft one. `meeting` **refuses to boot** without `LLM_BASE_URL` and `LLM_API_KEY`, and even with them a recording fails at `/stop` if `llm` isn't reachable.

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

**Every redeploy kills in-flight meetings.** There is no drain, no handoff, no persistence. A restart mid-meeting loses the rolling transcript and the open Transcribe streams; the bot's `/record stop` will then 404. Deploy when no one is recording, and treat "is anyone in a voice channel right now?" as a real pre-deploy check.

Nothing is ever written to disk, so there's no volume to provision and no cleanup to run.

## Rollback

`git revert` + push. No schema, no migration, no persisted state to reconcile.

The same in-flight caveat applies: a rollback is a restart, and a restart drops active meetings.

## AWS Transcribe operational notes

- **One open stream per active speaker, for the whole meeting.** A 20-person call with everyone talking means up to 20 concurrent streams. Check the account's concurrent-stream quota before a large meeting — exceeding it fails the stream open, not the whole session.
- **Transcribe ends sessions on its own** (idle timeout, and a hard 4 h cap). The wrapper reopens on the next audio and offsets the new session's word times. This is unit-tested against a fake but **never observed against a real timeout** — a genuinely long meeting is the first real test of it.
- **`MAX_MEETING_MS` defaults to 4 h**, matching Transcribe's cap. Past it, the session stops accepting audio. It's a backstop for a forgotten recording, not the normal path — normal ends are `/record stop` or auto-stop when the voice channel empties.
- **Cost scales with audio duration, once.** Audio is sent to Transcribe exactly once and never replayed, so polling `/transcript` costs nothing. If you see cost scaling with poll frequency, something has reintroduced the replay design — see [ARCHITECTURE.md](ARCHITECTURE.md#the-cost-bug-that-shaped-the-design).

## Privacy posture

Worth stating plainly, because it's a recording service:

- **Audio is never persisted, never mixed, and never returned.** It streams to AWS as transcription input and is dropped.
- **Nothing is written to disk** — there's a test enforcing it.
- **The service retains nothing after `/stop` replies.** Transcript, minutes, and PDF are handed to the caller and the session is deregistered. If they matter, the bot must persist them.
- An abrupt disconnect **holds** the session for `DISCONNECT_GRACE_S` (default 60 s) so `POST /stop` can still produce the minutes and PDF. Only when that window expires with no `/stop` does `discard()` run, and only then are they lost.

## Troubleshooting

- **Container dies at boot.** `MEETING_ENV` is non-`local` and one of the four required vars is missing — the error lists them. Or `CONSUMER_KEYS` isn't a JSON **array**.
- **`/record stop` returns 404.** The session isn't in the registry. Most likely the service restarted mid-meeting (a deploy), or `/stop` was already called, or requests are being load-balanced across replicas.
- **`/stop` hangs ~5 seconds then returns a short transcript.** The client never sent `{"end_of_audio": true}`, so the drain barrier timed out. Check the bot's voice surface — a warning is logged on this path.
- **Minutes come back as `(minutes unavailable: LLM service error)`.** This is the symptom of an `llm` problem — **not** an error status. `LlmUnavailable` is caught in `pipeline/minutes.py` and degrades to placeholder minutes, so `/stop` still returns **200** with a PDF; only the summary/decisions/action-items are empty. Don't go looking for a 502. Check `LLM_BASE_URL` resolves on the private network, that meeting's `LLM_API_KEY` is in llm's `CONSUMER_KEYS` with the `chat` scope, and the `LLM unavailable, returning placeholder minutes` warning in the logs.
- **Transcript is empty but audio was flowing.** Usually AWS credentials or region — Transcribe stream opens fail silently from the client's perspective. Check the logs for stream-open errors.
- **Speakers' words are interleaved at wrong times.** Timeline anchoring. The 200 ms gap tolerance is unverified against live audio; see [ARCHITECTURE.md → Live-verify caveats](ARCHITECTURE.md#live-verify-caveats).
- **Everything green but `/record` says "not configured".** That's the *bot*, not this service — `MEETING_BASE_URL` is unset on the discord-bot.
