"""Tests for the meeting HTTP + WebSocket endpoints, using a fake SessionRegistry
(no real ffmpeg/AWS/network) injected via app.dependency_overrides."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.deps import get_key_store
from src.api.hashing import generate_key
from src.api.wiring import get_session_registry
from src.contracts import Minutes, Segment, StopResponse
from src.key_store import InMemoryKeyStore
from src.sessions import SessionAlreadyExistsError


class FakeSession:
    def __init__(self, session_id, guild_id):
        self.session_id = session_id
        self.guild_id = guild_id
        self.feed_calls: list[tuple[str, str, bytes, int]] = []
        self.stop_called = False
        self.discard_called = False
        self._segments = [Segment(speaker="alice", start_ms=0, text="hello")]
        self.raise_on_feed_for: set[str] = set()
        self.audio_complete = False
        self.drops = {}

    def feed(self, speaker_id, display_name, opus_payload, ts_ms):
        if speaker_id in self.raise_on_feed_for:
            raise RuntimeError("simulated ffmpeg decode failure")
        self.feed_calls.append((speaker_id, display_name, opus_payload, ts_ms))

    async def transcript_view(self):
        return self._segments

    async def stop(self):
        self.stop_called = True
        return StopResponse(
            transcript="[00:00] alice: hello",
            minutes=Minutes(summary="s", decisions=[], action_items=[]),
            pdf_b64="ZmFrZQ==",
        )

    def mark_audio_complete(self):
        self.audio_complete = True

    def note_drop(self, reason, count=1):
        self.drops[reason] = self.drops.get(reason, 0) + count

    def discard(self):
        self.discard_called = True


class FakeRegistry:
    def __init__(self):
        self.sessions: dict[str, FakeSession] = {}
        self.created_with: list[tuple[str, str]] = []

    def create(self, session_id, guild_id):
        if session_id in self.sessions:
            raise SessionAlreadyExistsError(session_id)
        session = FakeSession(session_id, guild_id)
        self.sessions[session_id] = session
        self.created_with.append((session_id, guild_id))
        return session

    def get(self, session_id):
        return self.sessions.get(session_id)


@pytest.fixture
def store():
    return InMemoryKeyStore()


@pytest.fixture
def consumer_key(store):
    plaintext, prefix, key_hash = generate_key()
    store.add(prefix=prefix, key_hash=key_hash, name="bot", scopes=["meetings"])
    return plaintext


@pytest.fixture
def registry():
    return FakeRegistry()


@pytest.fixture
def client(store, registry, monkeypatch):
    monkeypatch.setenv("API_KEY", "test-bootstrap-key")
    # Grace period OFF by default so a disconnect tears down synchronously and
    # these tests stay deterministic (no task left sleeping past the TestClient
    # context). The HELD behavior that production runs is covered explicitly by
    # `client_held` and the _discard_after_grace unit tests below.
    monkeypatch.setenv("DISCONNECT_GRACE_S", "0")
    from src.config import get_settings

    get_settings.cache_clear()

    app = create_app()
    app.dependency_overrides[get_key_store] = lambda: store
    app.dependency_overrides[get_session_registry] = lambda: registry
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def client_held(store, registry, monkeypatch):
    """A client with the production disconnect grace period configured."""
    monkeypatch.setenv("API_KEY", "test-bootstrap-key")
    monkeypatch.setenv("DISCONNECT_GRACE_S", "60")
    from src.config import get_settings

    get_settings.cache_clear()

    app = create_app()
    app.dependency_overrides[get_key_store] = lambda: store
    app.dependency_overrides[get_session_registry] = lambda: registry
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_get_transcript_returns_fake_segments(client, registry, consumer_key):
    registry.create("s1", "g1")

    resp = client.get("/meetings/s1/transcript", headers={"X-API-Key": consumer_key})

    assert resp.status_code == 200
    body = resp.json()
    assert body["segments"] == [{"speaker": "alice", "start_ms": 0, "text": "hello"}]


def test_get_transcript_requires_key(client, registry):
    registry.create("s1", "g1")

    resp = client.get("/meetings/s1/transcript")

    assert resp.status_code == 401


def test_get_transcript_unknown_session_404(client, consumer_key):
    resp = client.get("/meetings/nope/transcript", headers={"X-API-Key": consumer_key})
    assert resp.status_code == 404


def test_get_transcript_invalid_session_id_400(client, consumer_key):
    resp = client.get("/meetings/bad!id/transcript", headers={"X-API-Key": consumer_key})
    assert resp.status_code == 400


def test_post_stop_returns_fake_stop_response(client, registry, consumer_key):
    session = registry.create("s1", "g1")

    resp = client.post("/meetings/s1/stop", headers={"X-API-Key": consumer_key})

    assert resp.status_code == 200
    body = resp.json()
    assert body["transcript"] == "[00:00] alice: hello"
    assert body["minutes"]["summary"] == "s"
    assert session.stop_called is True


def test_post_stop_requires_key(client, registry):
    registry.create("s1", "g1")

    resp = client.post("/meetings/s1/stop")

    assert resp.status_code == 401


def test_post_stop_unknown_session_404(client, consumer_key):
    resp = client.post("/meetings/nope/stop", headers={"X-API-Key": consumer_key})
    assert resp.status_code == 404


def _frame(speaker_id: str, ts_ms: int, opus_payload: bytes) -> bytes:
    speaker_bytes = speaker_id.encode("utf-8")
    return (
        len(speaker_bytes).to_bytes(2, "big")
        + speaker_bytes
        + ts_ms.to_bytes(8, "big")
        + opus_payload
    )


def test_ws_stream_rejects_bad_key(client, registry):
    with pytest.raises(Exception):
        with client.websocket_connect("/meetings/s1/stream?key=wrong-key&guild_id=g1"):
            pass
    assert "s1" not in registry.sessions


def test_ws_stream_authenticates_and_feeds_decoded_frames(client, registry, consumer_key):
    with client.websocket_connect(
        f"/meetings/ws-session-1/stream?key={consumer_key}&guild_id=guild-42"
    ) as ws:
        ws.send_text('{"speaker_id": "alice-id", "display_name": "Alice"}')
        ws.send_bytes(_frame("alice-id", 0, b"opus-frame-1"))
        ws.send_bytes(_frame("bob-id", 100, b"opus-frame-2"))

    assert registry.created_with == [("ws-session-1", "guild-42")]
    session = registry.sessions["ws-session-1"]
    assert session.feed_calls == [
        ("alice-id", "Alice", b"opus-frame-1", 0),
        ("bob-id", "bob-id", b"opus-frame-2", 100),
    ]
    # Disconnect without POST /stop -- the WS handler must tear the session down
    # via the lightweight discard(), NOT the full stop() pipeline. (Immediate
    # here only because this client sets DISCONNECT_GRACE_S=0.)
    assert session.discard_called is True
    assert session.stop_called is False


def test_ws_disconnect_holds_the_session_so_stop_can_still_finalize(
    client_held, registry, consumer_key
):
    """The bug that lost two production meetings: an abrupt disconnect used to
    discard a fully-transcribed session immediately, so the bot's follow-up
    POST /stop 404'd and the minutes were gone. With a grace period configured
    the session must SURVIVE the disconnect."""
    with client_held.websocket_connect(
        f"/meetings/held-session/stream?key={consumer_key}&guild_id=g1"
    ) as ws:
        ws.send_bytes(_frame("alice-id", 0, b"opus-frame-1"))

    session = registry.sessions["held-session"]
    assert session.discard_called is False
    assert registry.get("held-session") is session
    # A closed socket cannot deliver more audio, so /stop's end-of-audio barrier
    # must pass immediately instead of waiting out its drain timeout.
    assert session.audio_complete is True

    # ...and the salvage actually works: /stop finalizes the held session.
    resp = client_held.post("/meetings/held-session/stop", headers={"X-API-Key": consumer_key})
    assert resp.status_code == 200
    assert session.stop_called is True


def test_ws_stream_fallback_auth_binary_first_frame_closes_cleanly(client, registry):
    # No ?key= query param -> fallback path expects a text frame with the key
    # first. A binary frame instead must close cleanly (1008), not crash the
    # handler with an unhandled KeyError from Starlette's receive_text().
    with pytest.raises(Exception):
        with client.websocket_connect("/meetings/s1/stream?guild_id=g1") as ws:
            ws.send_bytes(_frame("alice-id", 0, b"opus-frame"))
            ws.receive_text()
    assert "s1" not in registry.sessions


def test_ws_stream_invalid_session_id_closes(client, registry, consumer_key):
    with pytest.raises(Exception):
        with client.websocket_connect(f"/meetings/bad!id/stream?key={consumer_key}&guild_id=g1"):
            pass
    assert "bad!id" not in registry.sessions


def test_ws_stream_duplicate_session_id_closes_1008(client, registry, consumer_key):
    # Fix #5: a second connect for an id that already has an active session
    # must close cleanly (1008), not crash the handler, and must leave the
    # existing session (and its registration) untouched.
    registry.create("dup-session", "g1")
    existing = registry.sessions["dup-session"]

    with pytest.raises(Exception):
        with client.websocket_connect(
            f"/meetings/dup-session/stream?key={consumer_key}&guild_id=g1"
        ) as ws:
            # The server accepts (key is valid) before registry.create() raises,
            # so the close happens post-accept -- confirm it via a subsequent
            # receive rather than expecting connect() itself to fail.
            ws.receive_text()

    assert registry.sessions["dup-session"] is existing
    assert existing.discard_called is False


def test_ws_stream_decode_error_drops_frame_but_keeps_session_alive(client, registry, consumer_key):
    # Fix #3: a single frame that fails to decode (e.g. RuntimeError from
    # ffmpeg) must not kill the whole meeting -- it's logged and dropped, and
    # subsequent good frames are still fed.
    with client.websocket_connect(
        f"/meetings/decode-err-session/stream?key={consumer_key}&guild_id=g1"
    ) as ws:
        session = registry.sessions["decode-err-session"]
        session.raise_on_feed_for.add("bad-speaker")

        ws.send_bytes(_frame("bad-speaker", 0, b"undecodable"))
        ws.send_bytes(_frame("good-speaker", 100, b"opus-frame-ok"))

    session = registry.sessions["decode-err-session"]
    assert session.feed_calls == [("good-speaker", "good-speaker", b"opus-frame-ok", 100)]
    # The connection stayed alive through the bad frame and tore down normally
    # via the disconnect path (not an unhandled-exception 1011).
    assert session.discard_called is True


def test_ws_end_of_audio_control_frame_marks_the_session(client, registry, consumer_key):
    """The barrier POST /stop waits on. Because the socket delivers in order,
    this frame arriving proves every audio frame before it has been fed -- so it
    must be routed to the session, and must not be mistaken for a speaker
    registration."""
    with client.websocket_connect(f"/meetings/ws-eoa/stream?key={consumer_key}&guild_id=g1") as ws:
        ws.send_text('{"speaker_id": "alice-id", "display_name": "Alice"}')
        ws.send_bytes(_frame("alice-id", 0, b"opus-frame-1"))
        ws.send_text('{"end_of_audio": true}')

    session = registry.sessions["ws-eoa"]
    assert session.audio_complete is True
    # The audio that preceded it still landed, and the signal itself is not a
    # speaker.
    assert session.feed_calls == [("alice-id", "Alice", b"opus-frame-1", 0)]


def test_ws_counts_frames_it_throws_away(client, registry, consumer_key):
    """A malformed frame used to `continue` with no signal whatsoever. Since the
    binary layout is caller-supplied, a bot-side framing bug looked exactly like
    a quiet meeting."""
    with client.websocket_connect(
        f"/meetings/ws-drops/stream?key={consumer_key}&guild_id=g1"
    ) as ws:
        ws.send_bytes(b"\x00\x40truncated")  # claims a 64-byte speaker id
        ws.send_bytes(b"")

    session = registry.sessions["ws-drops"]
    assert session.drops.get("malformed_ws_frame", 0) >= 1


def test_discard_after_grace_discards_a_session_nobody_claimed():
    """Grace expiring with no POST /stop is the one path that may still throw
    the transcript away."""
    from src.api.routers.meetings import _discard_after_grace

    registry = FakeRegistry()
    session = registry.create("abandoned", "g1")

    asyncio.run(_discard_after_grace(registry, session, "abandoned", 0))

    assert session.discard_called is True


def test_discard_after_grace_leaves_a_session_that_stop_already_claimed():
    """POST /stop inside the window deregisters the session, so the pending
    teardown must find nothing to do rather than discard a second time."""
    from src.api.routers.meetings import _discard_after_grace

    registry = FakeRegistry()
    session = registry.create("claimed", "g1")
    del registry.sessions["claimed"]  # what sessions.py stop() does

    asyncio.run(_discard_after_grace(registry, session, "claimed", 0))

    assert session.discard_called is False
