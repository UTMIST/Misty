"""Meeting endpoints: transcript polling, stop, and the live audio-ingest WebSocket.

WebSocket protocol -- ``WS /meetings/{session_id}/stream``
============================================================
This is the wire contract the Discord-bot sub-plan mirrors on the client side.

Connect
-------
``ws://.../meetings/{session_id}/stream?key=<consumer-key>&guild_id=<guild-id>``

- ``key`` (query param, preferred): the consumer's API key (same key used for
  ``X-API-Key`` on the HTTP endpoints). Validated against the key store before
  the socket is accepted; on failure the connection is closed during the
  handshake (HTTP-level reject).
- If ``key`` is omitted from the query string, the server instead accepts the
  socket and waits for exactly one text frame of the form ``{"key": "..."}``
  as the very first message. If that message is missing, malformed, or the
  key fails validation, the socket is closed immediately with WS close code
  ``1008`` (policy violation) before any audio is processed.
- ``guild_id`` (query param, optional): passed straight through to
  ``SessionRegistry.create``. If omitted, ``session_id`` is used as the
  guild_id (explicit fallback -- this task does not implement sourcing
  guild_id from a control message; see task report for rationale).
- ``session_id`` must match ``^[A-Za-z0-9_-]{1,64}$`` -- otherwise the
  connection is refused with close code 1008 before any session is created.

Once authenticated, two kinds of application messages are accepted:

1. Control messages (WebSocket **text** frames, UTF-8 JSON):
   ``{"speaker_id": "<id>", "display_name": "<name>"}``
   Registers/updates the display name shown for a speaker_id. Sent whenever a
   speaker's identity becomes known/changes (e.g. a Discord user joins voice).
   Unknown/malformed text frames are ignored (not fatal).

   ``{"end_of_audio": true}``
   Sent once, after the bot stops recording and has forwarded every frame it
   captured. Because the socket delivers in order, the server treats this as
   proof that all audio has arrived, and ``POST /stop`` waits for it before
   finalizing. Without it (an older bot, a crash, a dropped socket) ``/stop``
   proceeds after ``sessions.AUDIO_DRAIN_TIMEOUT_S`` and logs a warning; the
   tail of the transcript may then be short, which is the failure this signal
   exists to prevent.

2. Audio frames (WebSocket **binary** frames), one raw Opus packet each,
   framed as:

   ``[2 bytes: speaker_id_len, big-endian uint16]``
   ``[speaker_id_len bytes: speaker_id, UTF-8]``
   ``[8 bytes: ts_ms, big-endian uint64]``
   ``[remaining bytes: one Opus packet payload]``

   i.e. a length-prefixed speaker id, then an 8-byte millisecond timestamp,
   then the raw Opus payload with no further framing. Truncated/undersized
   frames are dropped (not fatal). Each valid frame is fed to the session as
   ``session.feed(speaker_id, display_name, opus_payload, ts_ms)`` where
   ``display_name`` is whatever was last registered for that speaker_id (or
   the speaker_id itself if never registered).

   ``session.feed`` decodes each frame in-process with that speaker's own
   stateful ``OpusStreamDecoder`` (see ``wiring.AudioAdapter`` and
   ``audio/decoder.py``) -- no subprocess per frame. The decoded PCM is
   buffered in memory only; nothing is written to disk.

Disconnect
----------
If the client disconnects (or the connection errors) without the consumer
having called ``POST /meetings/{session_id}/stop`` first, the session is
**held**, not destroyed, for ``DISCONNECT_GRACE_S`` (default 60s). A dropped
socket does not mean the meeting is lost: the transcript is already assembled
server-side, so within that window ``POST /stop`` still finalizes it into
minutes exactly as a normal stop would. The disconnect also marks
end-of-audio, since no further frames can arrive on a closed socket.

Only when the grace period expires with no ``/stop`` is the session torn down
via ``session.discard()`` -- a lightweight teardown that drops the buffered
audio and deregisters the session. Neither path runs the finalize pipeline
(transcription flush, minutes, PDF) on its own; that involves a blocking LLM
HTTP call and is only worth paying for when a consumer actually asks for it.
Teardown failures are logged (not swallowed). Set ``DISCONNECT_GRACE_S=0`` to
discard immediately instead.

The WebSocket close code is logged on every abrupt disconnect. It is the only
signal that says *why* a live recording dropped, and both sides previously
discarded it.
"""

import asyncio
import json
import logging
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from platform_auth import ADMIN_SCOPE, parse_prefix, verify_key

from src.api.auth import require_scope
from src.api.deps import get_key_store
from src.api.wiring import get_session_registry
from src.config import get_settings
from src.contracts import StopResponse, TranscriptView
from src.key_store import InMemoryKeyStore
from src.sessions import SessionAlreadyExistsError, SessionRegistry

router = APIRouter()

_logger = logging.getLogger("meeting.audit")

# Single scope covering all meeting endpoints (HTTP + WS). This service has one
# internal consumer class (the Discord bot) so a single scope is a deliberate
# simplification rather than separate read/write/stream scopes.
MEETINGS_SCOPE = "meetings"

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ENVELOPE = "meeting_"

_SPEAKER_LEN_BYTES = 2
_TS_BYTES = 8

# Strong refs to in-flight grace-period teardowns (see _schedule_disconnect_teardown).
_pending_teardowns: set[asyncio.Task] = set()


def _valid_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID_RE.fullmatch(session_id))


def _discard(session, session_id: str, why: str) -> None:
    """Lightweight teardown, never allowed to raise out of a teardown path."""
    try:
        session.discard()
    except Exception as e:  # noqa: BLE001 - log, don't crash teardown
        _logger.warning("ws disconnect teardown failed for %s: %s", session_id, e)
    else:
        _logger.info("session %s discarded (%s)", session_id, why)


async def _discard_after_grace(registry, session, session_id: str, grace_s: float) -> None:
    """Hold a disconnected session for ``grace_s``, then discard it if nobody
    claimed it.

    The window exists so a dropped socket is survivable: the transcript is
    already assembled server-side, and POST /stop can still turn it into
    minutes. If /stop does arrive, sessions.py's stop() deregisters the
    session and this wakes up to find nothing to do.
    """
    await asyncio.sleep(grace_s)
    if registry.get(session_id) is None:
        return  # POST /stop claimed it inside the window -- nothing to discard
    _logger.warning(
        "session %s: no POST /stop within the %ss disconnect grace period; "
        "discarding the transcript",
        session_id,
        grace_s,
    )
    _discard(session, session_id, "grace period expired")


def _schedule_disconnect_teardown(registry, session, session_id: str) -> None:
    """Discard now, or after the grace period if one is configured."""
    grace_s = get_settings().disconnect_grace_s
    if grace_s <= 0:
        _discard(session, session_id, "no grace period configured")
        return
    _logger.info(
        "session %s: holding the transcript for %ss so POST /stop can still finalize it",
        session_id,
        grace_s,
    )
    # Held so the task isn't garbage-collected mid-sleep (asyncio keeps only a
    # weak reference to running tasks), and discarded once it completes.
    task = asyncio.create_task(_discard_after_grace(registry, session, session_id, grace_s))
    _pending_teardowns.add(task)
    task.add_done_callback(_pending_teardowns.discard)


def _authenticate_ws(key: str | None, store: InMemoryKeyStore, scope: str) -> bool:
    """Validate a raw API key string for WS auth (mirrors platform_auth's
    require_api_key logic, but standalone since the WS handshake can't reuse
    the HTTP Depends() chain the same way)."""
    if not key:
        return False

    prefix = parse_prefix(key, _ENVELOPE)
    if prefix is not None:
        key_hash = store.get_api_key_hash(prefix)
        if key_hash is None or not verify_key(key, key_hash):
            return False
        row = store.get_api_key_by_prefix(prefix)
        if row is None or not row.active or row.revoked_at is not None:
            return False
        return scope in row.scopes or ADMIN_SCOPE in row.scopes

    # Unwrap: compare_digest below needs a real str (SecretStr has no .encode).
    env_key = get_settings().api_key.get_secret_value()
    if env_key and secrets.compare_digest(key.encode(), env_key.encode()):
        return True  # env-bootstrap key carries admin scope
    return False


def _parse_frame(data: bytes) -> tuple[str, int, bytes] | None:
    """Parse one binary audio frame per the framing documented in the module
    docstring. Returns None for truncated/malformed frames (caller drops them)."""
    if len(data) < _SPEAKER_LEN_BYTES:
        return None
    speaker_len = int.from_bytes(data[:_SPEAKER_LEN_BYTES], "big")
    header_end = _SPEAKER_LEN_BYTES + speaker_len
    if len(data) < header_end + _TS_BYTES:
        return None
    speaker_id = data[_SPEAKER_LEN_BYTES:header_end].decode("utf-8", errors="replace")
    ts_ms = int.from_bytes(data[header_end : header_end + _TS_BYTES], "big")
    opus_payload = data[header_end + _TS_BYTES :]
    return speaker_id, ts_ms, opus_payload


@router.get("/meetings/{session_id}/transcript", response_model=TranscriptView)
async def get_transcript(
    session_id: str,
    _key=Depends(require_scope(MEETINGS_SCOPE)),
    registry: SessionRegistry = Depends(get_session_registry),
) -> TranscriptView:
    if not _valid_session_id(session_id):
        raise HTTPException(status_code=400, detail="invalid session_id")
    session = registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    segments = await session.transcript_view()
    return TranscriptView(segments=segments)


@router.post("/meetings/{session_id}/stop", response_model=StopResponse)
async def stop_meeting(
    session_id: str,
    _key=Depends(require_scope(MEETINGS_SCOPE)),
    registry: SessionRegistry = Depends(get_session_registry),
) -> StopResponse:
    if not _valid_session_id(session_id):
        raise HTTPException(status_code=400, detail="invalid session_id")
    session = registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    return await session.stop()


@router.websocket("/meetings/{session_id}/stream")
async def stream_meeting(
    websocket: WebSocket,
    session_id: str,
    key: str | None = Query(default=None),
    guild_id: str | None = Query(default=None),
    store: InMemoryKeyStore = Depends(get_key_store),
    registry: SessionRegistry = Depends(get_session_registry),
) -> None:
    if not _valid_session_id(session_id):
        await websocket.close(code=1008)
        return

    if key is not None:
        # Auth-at-handshake path: reject before accept() so a bad key never
        # gets a full WS connection.
        if not _authenticate_ws(key, store, MEETINGS_SCOPE):
            await websocket.close(code=1008)
            return
        await websocket.accept()
    else:
        # Fallback: accept, then require the first text frame to be the key.
        await websocket.accept()
        resolved_key = None
        try:
            first = await websocket.receive_text()
            payload = json.loads(first)
            if isinstance(payload, dict):
                resolved_key = payload.get("key")
        except WebSocketDisconnect:
            return
        except Exception:  # noqa: BLE001 - any failure to get a valid text key
            # (malformed JSON, a binary frame first -- Starlette's receive_text
            # raises KeyError on those -- or anything else) means auth fails;
            # close cleanly rather than let an unhandled exception escape.
            await websocket.close(code=1008)
            return
        if not _authenticate_ws(resolved_key, store, MEETINGS_SCOPE):
            await websocket.close(code=1008)
            return

    try:
        session = registry.create(session_id, guild_id or session_id)
    except SessionAlreadyExistsError:
        # Fix #5: a second WS connect for an already-active session_id must not
        # crash the handler post-accept -- close cleanly (1008) and leave the
        # existing session untouched.
        await websocket.close(code=1008)
        return
    display_names: dict[str, str] = {}

    close_code: int | None = None

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                # The close code is the ONE datum that says why a live recording
                # dropped, and it was previously thrown away on both sides --
                # which made a pair of production disconnects undiagnosable.
                close_code = message.get("code")
                break

            text = message.get("text")
            if text is not None:
                try:
                    control = json.loads(text)
                except (ValueError, TypeError):
                    continue
                if isinstance(control, dict) and control.get("end_of_audio"):
                    # The bot has stopped recording and sent everything it has.
                    # The socket delivers in order, so reaching this frame means
                    # every audio frame before it has already been fed -- which
                    # is exactly the barrier POST /stop needs before finalizing.
                    session.mark_audio_complete()
                elif isinstance(control, dict) and "speaker_id" in control:
                    speaker_id = control["speaker_id"]
                    display_names[speaker_id] = control.get("display_name", speaker_id)
                continue

            data = message.get("bytes")
            if not data:
                session.note_drop("empty_ws_frame")
                continue
            frame = _parse_frame(data)
            if frame is None:
                session.note_drop("malformed_ws_frame")
                continue
            speaker_id, ts_ms, opus_payload = frame
            display_name = display_names.get(speaker_id, speaker_id)
            try:
                # Fix #2: session.feed() does blocking CPU work (Opus decode)
                # -- offload to a thread so one meeting's decode work doesn't
                # stall the event loop for other connections.
                # Fix #3: a raising session.feed() doesn't crash the whole
                # meeting -- only this single frame is dropped and the receive
                # loop continues. This
                # is deliberately NOT `except WebSocketDisconnect` -- that must
                # still propagate up and break the loop (handled below).
                await asyncio.to_thread(session.feed, speaker_id, display_name, opus_payload, ts_ms)
            except Exception as e:  # noqa: BLE001 - see comment above
                _logger.warning(
                    "frame feed failed for %s speaker %s: %s", session_id, speaker_id, e
                )
                session.note_drop("feed_raised")
                continue
    except WebSocketDisconnect as e:
        close_code = e.code
    finally:
        # If the consumer already called POST /stop, sessions.py's stop() has
        # deregistered the session -- registry.get returns None and we skip.
        if registry.get(session_id) is not None:
            _logger.info(
                "session %s: WS closed without POST /stop (code=%s)",
                session_id,
                close_code if close_code is not None else "unknown",
            )
            # The socket is gone, so no further audio can arrive. Saying so
            # turns stop()'s end-of-audio barrier into an immediate pass
            # instead of a 5s wait plus a "no end-of-audio signal" warning.
            try:
                session.mark_audio_complete()
            except Exception as e:  # noqa: BLE001 - log, don't crash teardown
                _logger.warning("mark_audio_complete failed for %s: %s", session_id, e)
            _schedule_disconnect_teardown(registry, session, session_id)
