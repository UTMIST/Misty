# Contributing

Task walkthroughs for working on `meeting`. Assumes you've read the [README](../README.md) and skimmed [ARCHITECTURE.md](ARCHITECTURE.md) — that doc explains the *why*; this one is the *how*.

This is the platform's most unusual service: stateful, WebSocket-driven, and dependent on two external systems (AWS Transcribe, the `llm` service). Read [ARCHITECTURE.md → The cost bug that shaped the design](ARCHITECTURE.md#the-cost-bug-that-shaped-the-design) before touching the audio path — the obvious implementation is the one that was removed for costing O(n²).

## Conventions you need to know first

- **`src/sessions.py` does no I/O and imports no vendor SDK.** Every collaborator that touches the outside world arrives through the injected `deps` dict. Adding an `import boto3` or `import httpx` there breaks the property the whole test suite depends on. If you need a new collaborator, add a `deps` key and wire it in `src/api/wiring.py`. (It does import `assemble_transcript` from `src/pipeline/` directly — pure text assembly, no I/O. That's the allowed shape.)
- **`wiring.py` is the only place fakes become real.** One module builds the real decoders, the real Transcribe factory, and the real report builder. That's the seam.
- **Audio is sent once and never retained.** Each decoded chunk goes straight to that speaker's stream and is dropped. Do not add a buffer "just in case" — that's the design that was removed.
- **Nothing touches the filesystem.** There's a test asserting it (`test_session_lifecycle_never_touches_the_filesystem`). Fonts and the logo are package assets read at import, not runtime writes.
- **Malformed input is non-fatal.** Truncated frames, bad JSON, unknown text frames — skip and continue; one buggy client frame must never kill a meeting. Binary-path drops are counted via `note_drop`; the text/control path currently counts nothing. If you add a control message, counting its malformed case is a genuine improvement, not a requirement you're violating.
- **Credentials are `SecretStr`.** Unwrap with `.get_secret_value()` at the boundary — `wiring.py` does this for `LLM_API_KEY` precisely because `LlmClient` puts it straight into a header.

## Local setup

```bash
cd services/meeting
cp .env.example .env
uv sync --extra dev
uv run pytest          # no Docker, no network, no AWS credentials
```

No `ffmpeg` binary is needed — PyAV bundles its own libraries. No database, no Docker.

To exercise a **real** recording you need AWS Transcribe credentials, a running `llm` on 8002, and the real Discord surface (`npm start` in `discord-bot` — the playground has no voice equivalent).

## Walkthrough: change the WebSocket protocol

The wire format is a contract with the Discord bot. Changing it means changing two repos' worth of code in one PR, or shipping it backward-compatibly.

1. **Update the module docstring** in `src/api/routers/meetings.py`. It's the canonical statement of the protocol; [API.md](API.md) mirrors it.
2. **Handle the new message shape** in the receive loop. Follow the existing pattern: unknown/malformed control frames `continue` rather than raise.
3. **Stay backward compatible if you can.** `{"end_of_audio": true}` is the model — a client that never sends it still works, degrading to a 5 s timeout with a logged warning. Prefer that shape over a hard requirement.
4. **Update the bot side** in the same PR (`discord-bot/src/`), or the deploy order becomes a coordination problem across two Railway services.
5. **Update [API.md](API.md)** — the WS route isn't in OpenAPI, so that document *is* the published contract.
6. **Test it** in `tests/test_meetings_api.py`, which drives the socket with FastAPI's `TestClient`. Cover the happy path, the malformed variant, and the omitted variant.

## Walkthrough: add a session-lifecycle behavior

Say you want to emit a warning when a speaker's stream restarts.

1. **Add it to `MeetingSession`** in `src/sessions.py`, using only what's in `deps`. Need the current time? Use `deps["now"]()`, never `datetime.now()` directly — the clock is injected so tests can control it.
2. **If you need a new collaborator**, add a `deps` key rather than importing the real thing:
   ```python
   # sessions.py
   self._notify = deps.get("notify", lambda *_: None)
   ```
   then wire the real implementation in `wiring.py`'s `get_session_registry`.
3. **Test with fakes** in `tests/test_sessions.py`. The whole suite constructs `SessionRegistry(deps)` with stub callables — copy an existing case.
4. **If it changes `/stop`'s response**, update `src/contracts.py`, [API.md](API.md), and the bot's parsing.

## Walkthrough: tune the segmentation constants

`_SEGMENT_GAP_MS` (1500) and `_MAX_SEGMENT_MS` (5000) in `src/sessions.py` control how words become segments.

Before changing either, read the comments — both are reasoned, and `_MAX_SEGMENT_MS` was **measured**: at 10 s, an interjection inside the first 10 s of someone's turn still sorted after their whole block, which is the bug it exists to prevent.

1. Change the constant, never a call site.
2. Update its comment with your reasoning and, ideally, what you measured.
3. Update the tests in `tests/test_sessions.py` that assert segment boundaries — several construct word lists straddling these thresholds on purpose.
4. Update the table in [ARCHITECTURE.md](ARCHITECTURE.md#speaker-timeline-anchoring).

The same applies to `AUDIO_DRAIN_TIMEOUT_S` (5 s). Raising it makes `/stop` slower for every client whose end-of-audio signal is missing; lowering it risks truncating the tail on a slow network.

## Walkthrough: change the minutes prompt or PDF

- **Prompt** — `src/pipeline/minutes.py`. It posts to `llm`'s `/chat` and parses the completion into `Minutes`. Keep the parse defensive: an LLM that returns prose instead of the expected shape must not crash `/stop`. Test against a fake `LlmClient` in `tests/test_minutes.py`.
- **PDF** — `src/pipeline/pdf.py`. Fonts (DejaVu) and the UTMIST logo are bundled in `src/assets/` and must stay bundled — the service reads no external files and writes nothing. If you add an asset, add it to the package data so it survives the Docker build. Test in `tests/test_pdf.py`; assert on bytes/structure, never by writing a file.
- **`Minutes` shape** — `src/contracts.py`. Changing it means updating the prompt, the parser, the PDF renderer, and the bot's rendering, together.

## Walkthrough: work on transcription

`src/stt/transcribe.py` wraps Amazon Transcribe streaming. It is exercised **only against a fake client** — never a live session.

1. Keep the fakes in sync with any interface change. They are **not** in `conftest.py` (which holds only the dotenv/settings-cache fixtures) — each suite defines its own: `tests/test_transcribe.py`, `tests/test_sessions.py`, `tests/test_meetings_api.py`, `tests/test_minutes.py`.
2. The wrapper handles AWS ending a stream on its own (idle timeout, 4 h cap) by reopening on the next audio and offsetting the new session's word times by the audio already delivered. That logic is unit-tested against a fake but **never observed against a real timeout** — treat changes there as unverified until someone runs a long live meeting.
3. **Never write a test that opens a real Transcribe stream.** The suite must stay offline and free.

## Testing

```bash
uv run pytest
```

No Docker, no network, no AWS credentials. Transcribe and `llm` clients are faked via `app.dependency_overrides` and the injected `deps`.

Useful invocations:

```bash
uv run pytest tests/test_sessions.py     # the lifecycle core
uv run pytest tests/test_meetings_api.py # HTTP + WebSocket
uv run pytest -k transcript
uv run pytest -x -q
```

Tests worth not breaking:

- `test_session_lifecycle_never_touches_the_filesystem` — the no-disk guarantee.
- The segment-ordering cases in `test_sessions.py` — they encode the interjection bug.
- The WS auth cases — both the query-param and first-frame paths, plus the 1008 rejections.

## Linting and formatting

```bash
uv run ruff check .
uv run ruff format .
```

`meeting-test` runs `uv run pytest`, `ruff check`, **and** `ruff format --check`. The format check was deferred for a long time (12 files were unformatted); that has been cleared, so a formatting slip now fails the job.

This service reached staging with **no CI coverage at all** at one point — no test job, and `docker-build` skipped its image. Everything is wired up now.

## Checklist before you push

- [ ] `src/sessions.py` still does no I/O and imports no vendor SDK — new outside-world collaborators go through `deps`.
- [ ] No new audio buffering; audio is still sent once and dropped.
- [ ] Nothing writes to disk; the filesystem test still passes.
- [ ] Malformed client input is skipped, not fatal — it never kills the session.
- [ ] `deps["now"]()` used instead of `datetime.now()`.
- [ ] WS protocol changes are reflected in the router docstring **and** [API.md](API.md), and the bot side ships in the same PR (or the change is backward compatible).
- [ ] Tuning-constant changes updated the comment, the tests, and ARCHITECTURE.md.
- [ ] No test opens a real Transcribe stream or calls a real `llm`.
- [ ] `uv run pytest`, `uv run ruff check .`, and `uv run ruff format --check .` are clean.
