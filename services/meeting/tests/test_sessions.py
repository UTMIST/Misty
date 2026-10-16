"""Tests for the stateful session registry + meeting lifecycle, using fakes only
(no real AWS/network). The session layer touches no filesystem at all, so
these need no temp dirs."""

import asyncio
import sys
import threading
from datetime import datetime, timezone

import pytest

from src.contracts import Minutes
from src.sessions import (
    _MAX_SEGMENT_MS,
    _PCM_BYTES_PER_MS,
    SessionAlreadyExistsError,
    SessionRegistry,
    words_to_segments,
)


class _FakeDecoder:
    """Pass-through per-speaker 'decoder': in these tests the opus_frame_bytes
    IS the pcm payload, so we can assert routing without any real Opus decode.
    Records into the FakeAudio-shared ``decode_calls`` list so tests can
    assert call order across speakers (each speaker gets its OWN instance, as
    make_decoder() is a factory, but they all log to the same list)."""

    def __init__(self, decode_calls: list[bytes]):
        self._decode_calls = decode_calls

    def decode(self, opus_frame_bytes: bytes) -> bytes:
        self._decode_calls.append(opus_frame_bytes)
        return opus_frame_bytes


class FakeAudio:
    """Factory for per-speaker fake decoders (mirrors the real
    ``AudioAdapter.make_decoder()`` contract)."""

    def __init__(self):
        self.decode_calls = []

    def make_decoder(self) -> _FakeDecoder:
        return _FakeDecoder(self.decode_calls)


class FakeTranscriptionStream:
    """Stand-in for ONE speaker's persistent Transcribe stream.

    Mirrors the real contract in src/stt/transcribe.py: audio is pushed in with
    ``send()`` as it arrives and is never replayed, finalized words are readable
    at any time via ``words()``, and the stream is closed exactly once (either
    ``aclose()`` for a real finalize or ``abort()`` for an abrupt teardown)."""

    def __init__(self, words=None):
        self._words = list(words or [])
        self.sent: list[bytes] = []
        self.started = False
        self.aclosed = False
        self.aborted = False

    def start(self, loop=None) -> None:
        self.started = True

    def send(self, pcm: bytes) -> None:
        assert self.started, "audio sent before the stream was started"
        assert not (self.aclosed or self.aborted), "audio sent after the stream was closed"
        self.sent.append(pcm)

    def words(self) -> list[dict]:
        return list(self._words)

    async def aclose(self) -> list[dict]:
        self.aclosed = True
        return list(self._words)

    def abort(self) -> None:
        self.aborted = True


def _fake_report_builder(segments, meta):
    minutes = Minutes(summary="Test summary", decisions=["d1"], action_items=["a1"])
    pdf_bytes = b"%PDF-1.4 fake pdf content for testing purposes only"
    return minutes, pdf_bytes


def _make_deps(streams_by_speaker, audio=None, now=None):
    queue = list(streams_by_speaker)

    def make_transcription_stream():
        return queue.pop(0)

    return {
        "make_transcription_stream": make_transcription_stream,
        "audio": audio or FakeAudio(),
        "report_builder": _fake_report_builder,
        # Tests that don't exercise the barrier shouldn't pay its timeout.
        "audio_drain_timeout_s": 0.05,
        "now": now or (lambda: datetime(2026, 7, 25, 18, 0, tzinfo=timezone.utc)),
    }


def test_words_to_segments_splits_on_gap_over_3s():
    words = [
        {"text": "hi", "start_ms": 100},
        {"text": "friend", "start_ms": 4000},
    ]
    segments = words_to_segments("bob", words, gap_ms=3000)

    assert [(s.start_ms, s.text) for s in segments] == [(100, "hi"), (4000, "friend")]
    assert all(s.speaker == "bob" for s in segments)


def test_words_to_segments_keeps_close_words_in_one_segment():
    words = [
        {"text": "hello", "start_ms": 0},
        {"text": "there", "start_ms": 500},
    ]
    segments = words_to_segments("alice", words, gap_ms=3000)

    assert len(segments) == 1
    assert segments[0].start_ms == 0
    assert segments[0].text == "hello there"


def test_create_rejects_duplicate_session_id():
    registry = SessionRegistry(_make_deps([]))
    registry.create("s1", "g1")

    with pytest.raises(SessionAlreadyExistsError):
        registry.create("s1", "g1")


def test_feed_and_transcript_view_merge_chronologically_across_speakers():
    # Both speakers' Transcribe output starts near 0 -- this is realistic: AWS
    # Transcribe's word start_ms is relative to the start of each speaker's OWN
    # concatenated PCM buffer, not to the meeting. Bob's FIRST fed frame has
    # ts_ms=30000 (he starts speaking ~30s into the meeting), even though his
    # own Transcribe words start at ~0/100 relative to his own buffer. The
    # session must anchor bob's words to his base ts_ms so the merged,
    # cross-speaker transcript sorts him AFTER alice, not before.
    alice_words = [
        {"text": "hello", "start_ms": 0},
        {"text": "there", "start_ms": 500},
    ]
    bob_words = [
        {"text": "hi", "start_ms": 0},
        {"text": "friend", "start_ms": 100},
    ]
    alice_stream = FakeTranscriptionStream(alice_words)
    bob_stream = FakeTranscriptionStream(bob_words)
    audio = FakeAudio()
    deps = _make_deps([alice_stream, bob_stream], audio=audio)
    registry = SessionRegistry(deps)

    # Frames are sized like real ones (20ms of 16 kHz mono s16le = 640 bytes),
    # since buffer->meeting time anchoring is derived from buffered PCM length.
    def frame(label: str) -> bytes:
        return label.encode().ljust(_PCM_BYTES_PER_MS * 20, b"\x00")

    session = registry.create("session-1", "guild-1")
    session.feed("alice-id", "alice", frame("alice-frame-1"), ts_ms=0)
    session.feed("bob-id", "bob", frame("bob-frame-1"), ts_ms=30000)
    session.feed("alice-id", "alice", frame("alice-frame-2"), ts_ms=500)
    session.feed("bob-id", "bob", frame("bob-frame-2"), ts_ms=30100)

    # Routing: decode was called once per fed frame.
    assert audio.decode_calls == [
        frame("alice-frame-1"),
        frame("bob-frame-1"),
        frame("alice-frame-2"),
        frame("bob-frame-2"),
    ]

    view = asyncio.run(session.transcript_view())

    # Bob's words, anchored to his base ts_ms (30000), must sort AFTER alice's,
    # even though Bob's raw Transcribe start_ms values (0, 100) are smaller
    # than alice's (0, 500) in isolation.
    assert [(s.speaker, s.start_ms, s.text) for s in view] == [
        ("alice", 0, "hello there"),
        ("bob", 30000, "hi friend"),
    ]

    result = asyncio.run(session.stop())

    assert result.transcript == ("[00:00] alice: hello there\n[00:30] bob: hi friend")
    assert result.minutes.summary == "Test summary"

    import base64

    assert base64.b64decode(result.pdf_b64)[:4] == b"%PDF"

    # Cleanup: the session must be deregistered and its buffers dropped.
    assert registry.get("session-1") is None
    assert session._speakers == {}
    # Re-creating the same id must now succeed (fully deregistered).
    registry.create("session-1", "guild-1")


def _pcm(ms: int, fill: bytes = b"\x00") -> bytes:
    """`ms` milliseconds of fake 16 kHz mono s16le PCM (32 bytes per ms)."""
    return fill * (32 * ms)


def test_words_after_a_mid_meeting_silence_anchor_to_wall_clock():
    """A speaker's PCM buffer holds ONLY the frames they spoke -- silence is
    never buffered -- so AWS Transcribe's buffer-relative word start_ms
    under-counts every real gap. Anchoring on the speaker's FIRST frame alone
    fixes only their first word; a speaker who goes quiet mid-meeting and then
    resumes must have their later words anchored to the wall-clock ts_ms of
    the frame that resumed, not to their compressed buffer timeline."""
    words = [
        {"text": "opening", "start_ms": 0},
        # Buffer-relative: the second utterance starts right after the first
        # 1000ms of speech, because the 59s of silence between them was never
        # written to the buffer.
        {"text": "closing", "start_ms": 1000},
    ]
    stream = FakeTranscriptionStream(words)
    deps = _make_deps([stream])
    registry = SessionRegistry(deps)
    session = registry.create("session-gap", "guild-1")

    # Alice speaks for 1s at the top of the meeting...
    session.feed("alice-id", "alice", _pcm(1000), ts_ms=0)
    # ...then says nothing for a minute and speaks again at t=60s.
    session.feed("alice-id", "alice", _pcm(1000), ts_ms=60_000)

    view = asyncio.run(session.transcript_view())

    # "closing" must land at ~60s (its real wall-clock time), NOT at 1000ms.
    # And because the gap is now visible, it must be its OWN segment rather
    # than being glued onto "opening" by the 3s-gap rule.
    assert [(s.speaker, s.start_ms, s.text) for s in view] == [
        ("alice", 0, "opening"),
        ("alice", 60_000, "closing"),
    ]


def test_continuous_speech_does_not_create_spurious_anchors():
    """Back-to-back frames (ordinary continuous speech, plus a little network
    jitter) must NOT be treated as a silence gap -- otherwise every frame
    would re-anchor and the mapped timeline would stretch."""
    words = [{"text": "one", "start_ms": 0}, {"text": "two", "start_ms": 20}]
    stream = FakeTranscriptionStream(words)
    deps = _make_deps([stream])
    registry = SessionRegistry(deps)
    session = registry.create("session-continuous", "guild-1")

    # Five 20ms frames arriving ~20ms apart, with a few ms of jitter.
    for i, ts in enumerate([0, 21, 39, 62, 80]):
        session.feed("alice-id", "alice", _pcm(20), ts_ms=ts)

    view = asyncio.run(session.transcript_view())

    # One anchor only -> buffer-relative times pass through unshifted.
    assert [(s.start_ms, s.text) for s in view] == [(0, "one two")]


def test_audio_is_streamed_once_and_never_replayed_across_polls():
    """The whole point of the persistent per-speaker stream: each chunk of PCM
    is handed to Transcribe exactly once, as it arrives.

    The previous design re-ran transcription over the ENTIRE buffer on every
    ``transcript_view()`` call, so a meeting polled k times re-sent roughly k/2
    copies of its own audio -- AWS billing that grows with the SQUARE of meeting
    length. Polling must now be free."""
    stream = FakeTranscriptionStream([{"text": "hi", "start_ms": 0}])
    deps = _make_deps([stream])
    registry = SessionRegistry(deps)
    session = registry.create("session-stream", "guild-1")

    first, second = _pcm(20, b"\x01"), _pcm(20, b"\x02")
    session.feed("alice-id", "alice", first, ts_ms=0)
    asyncio.run(session.transcript_view())
    asyncio.run(session.transcript_view())
    session.feed("alice-id", "alice", second, ts_ms=20)
    asyncio.run(session.transcript_view())

    # Three polls, two frames: each frame sent exactly once regardless of polls.
    assert stream.sent == [first, second]


def test_transcript_view_reads_accumulated_words_without_closing_the_stream():
    """Polling is a read of what the live stream has finalized so far -- it must
    not end the stream, or the speaker would stop being transcribed."""
    stream = FakeTranscriptionStream([{"text": "rolling", "start_ms": 0}])
    deps = _make_deps([stream])
    registry = SessionRegistry(deps)
    session = registry.create("session-poll", "guild-1")
    session.feed("alice-id", "alice", _pcm(20), ts_ms=0)

    view = asyncio.run(session.transcript_view())

    assert [(s.speaker, s.text) for s in view] == [("alice", "rolling")]
    assert stream.aclosed is False
    assert stream.aborted is False


def test_stop_closes_each_stream_and_uses_its_final_words():
    """stop() must flush and close the stream (so AWS emits any last finalized
    results) and build the transcript from what comes back."""
    stream = FakeTranscriptionStream([{"text": "final", "start_ms": 0}])
    deps = _make_deps([stream])
    registry = SessionRegistry(deps)
    session = registry.create("session-final", "guild-1")
    session.feed("alice-id", "alice", _pcm(20), ts_ms=0)

    result = asyncio.run(session.stop())

    assert stream.aclosed is True
    assert result.transcript == "[00:00] alice: final"


def test_pcm_is_not_retained_after_being_streamed():
    """With no replay there is nothing to keep the audio for. Retaining it would
    reinstate the old O(meeting duration) memory growth for no benefit."""
    stream = FakeTranscriptionStream([])
    deps = _make_deps([stream])
    registry = SessionRegistry(deps)
    session = registry.create("session-nomem", "guild-1")

    for i in range(50):
        session.feed("alice-id", "alice", _pcm(20), ts_ms=i * 20)

    buf = session._speakers["alice-id"]
    assert not hasattr(buf, "pcm_chunks"), "audio must not be buffered for replay"
    # The byte COUNT is still tracked -- anchoring needs it.
    assert buf.buffered_ms == 50 * 20


def test_empty_decodes_do_not_anchor_or_shift_the_timeline():
    """A DTX/malformed packet decodes to b"" (see decoder.decode's error path).
    It advances the buffer by nothing, so it must not record an anchor either:
    anchoring on it would attribute the start of the NEXT real audio to the
    dud frame's timestamp instead of its own."""
    words = [{"text": "after", "start_ms": 0}]
    stream = FakeTranscriptionStream(words)
    deps = _make_deps([stream])
    registry = SessionRegistry(deps)
    session = registry.create("session-dtx", "guild-1")

    # A dud frame lands first, a full second before any real audio.
    session.feed("alice-id", "alice", b"", ts_ms=0)
    session.feed("alice-id", "alice", _pcm(20), ts_ms=1000)

    buf = session._speakers["alice-id"]
    assert buf.anchors == [(0, 1000)], "the dud frame must not create an anchor"

    view = asyncio.run(session.transcript_view())
    assert [(s.start_ms, s.text) for s in view] == [(1000, "after")]


def test_buffer_timebase_does_not_accumulate_per_chunk_rounding():
    """Buffer position must be tracked in BYTES and converted once, not summed
    from per-chunk truncated milliseconds. Chunks whose length isn't a whole
    number of ms are legal (the resampler emits variable sizes), and flooring
    each one independently accumulates a monotonic undercount that eventually
    manufactures a false silence gap mid-speech."""
    stream = FakeTranscriptionStream([])
    deps = _make_deps([stream])
    registry = SessionRegistry(deps)
    session = registry.create("session-rounding", "guild-1")

    # 100 chunks of 48 bytes = 1.5ms each. Truncating each to 1ms loses 50ms
    # in total; tracking bytes keeps the exact 150ms.
    for i in range(100):
        session.feed("alice-id", "alice", b"\x00" * 48, ts_ms=i)

    buf = session._speakers["alice-id"]
    assert buf.buffered_ms == 150


def test_feed_is_rejected_once_stop_has_begun():
    """stop() snapshots each speaker's buffer, then awaits transcription and
    report generation. The WS ingest loop is still live during that window (the
    bot only closes the socket AFTER /stop returns), so a late frame could be
    appended to a buffer that has already been transcribed -- silently dropped
    from the transcript, then discarded by cleanup. Ingest must be quiesced the
    moment finalization starts, so a late frame is refused outright rather than
    accepted-then-lost."""
    # Observed INSIDE the stop window -- checking after stop() returns would be
    # vacuous, since _cleanup() clears _speakers on the way out either way.
    accepted_during_stop = []

    class FeedingStream(FakeTranscriptionStream):
        """Simulates the WS ingest loop delivering a frame while stop() is
        suspended awaiting aclose() -- exactly when the real one runs."""

        async def aclose(self):
            session.feed("bob-id", "bob", _pcm(20), ts_ms=5000)
            accepted_during_stop.append("bob-id" in session._speakers)
            return await super().aclose()

    deps = _make_deps([])
    deps["make_transcription_stream"] = lambda: FeedingStream([{"text": "hi", "start_ms": 0}])
    registry = SessionRegistry(deps)
    session = registry.create("session-late-feed", "guild-1")
    session.feed("alice-id", "alice", _pcm(20), ts_ms=0)

    asyncio.run(session.stop())

    assert accepted_during_stop, "the late feed never ran; test would be vacuous"
    assert accepted_during_stop == [False], "a frame fed during stop() was accepted then lost"


def test_session_lifecycle_never_touches_the_filesystem(monkeypatch):
    """Per-speaker PCM is buffered in memory ONLY -- the service persists no
    audio anywhere, so a full create -> feed -> stop cycle must open no files
    and create no directories.

    This also subsumes the old path-traversal regression test: speaker_id is
    caller-controlled (decoded straight off the WS binary frame with no
    validation -- see api/routers/meetings.py), and it used to be hashed
    before being interpolated into an on-disk path. With no filesystem writes
    at all there is no traversal surface left, and this test is what stands
    behind that.

    Note the patch list below is deliberately broad: patching ``builtins.open``
    ALONE is not sufficient. ``io.open`` is a separate name binding to the same
    original function, so rebinding ``builtins.open`` leaves ``io.open`` -- and
    therefore ``pathlib.Path.open``/``write_bytes`` -- resolving to the real
    thing. ``os.open``, ``os.mkdir`` and ``tempfile.mkdtemp`` bypass it too.
    """
    blocked = []

    def _blocker(label):
        def _raise(*args, **kwargs):
            blocked.append(label)
            raise AssertionError(f"session touched the filesystem via {label}: {args!r}")

        return _raise

    deps = _make_deps([FakeTranscriptionStream([{"text": "hi", "start_ms": 0}])])
    for target in (
        "builtins.open",
        "io.open",
        "os.open",
        "os.mkdir",
        "os.makedirs",
        "tempfile.mkdtemp",
        "tempfile.NamedTemporaryFile",
    ):
        monkeypatch.setattr(target, _blocker(target))

    registry = SessionRegistry(deps)
    session = registry.create("session-nodisk", "guild-1")
    session.feed("../../etc/evil", "attacker", _pcm(20), ts_ms=0)
    result = asyncio.run(session.stop())

    assert result.transcript == "[00:00] attacker: hi"


def test_stop_returns_no_audio():
    """Meeting audio is deliberately NOT part of the finalize output: the bot
    posts the minutes PDF only."""
    stream = FakeTranscriptionStream([{"text": "hi", "start_ms": 0}])
    deps = _make_deps([stream])
    registry = SessionRegistry(deps)
    session = registry.create("session-noaudio", "guild-1")
    session.feed("alice-id", "alice", _pcm(20), ts_ms=0)

    result = asyncio.run(session.stop())

    assert not hasattr(result, "audio_b64")


def test_stop_with_no_tracks_returns_an_empty_transcript():
    deps = _make_deps([])
    registry = SessionRegistry(deps)
    session = registry.create("session-empty", "guild-1")

    result = asyncio.run(session.stop())

    assert result.transcript == ""


def test_discard_cleans_up_without_running_finalize_pipeline():
    alice_words = [{"text": "hello", "start_ms": 0}]
    stream = FakeTranscriptionStream(alice_words)
    audio = FakeAudio()
    deps = _make_deps([stream], audio=audio)
    registry = SessionRegistry(deps)

    session = registry.create("session-discard", "guild-1")
    session.feed("alice-id", "alice", b"alice-frame-1", ts_ms=0)

    session.discard()

    # Cleanup happened: deregistered from the registry, buffered audio dropped.
    assert registry.get("session-discard") is None
    assert session._speakers == {}

    # The full finalize pipeline (report_builder/PDF) must NOT run -- but the
    # speaker's Transcribe stream MUST still be torn down, or an abrupt WS drop
    # would leak an open AWS stream for the rest of its idle timeout.
    assert stream.aborted is True
    assert stream.aclosed is False

    # Idempotent: calling discard() again must not raise.
    session.discard()

    # Re-creating the same id must now succeed (fully deregistered).
    registry.create("session-discard", "guild-1")


def test_stop_meta_duration_label_reflects_elapsed_time():
    # Fix #8: duration_label must be computed from started_at -> now, not left
    # empty.
    clock = {"t": datetime(2026, 7, 25, 18, 0, tzinfo=timezone.utc)}

    def now():
        return clock["t"]

    captured_meta = {}

    def capturing_report_builder(segments, meta):
        captured_meta.update(meta)
        return Minutes(summary="s", decisions=[], action_items=[]), b"%PDF-fake"

    deps = _make_deps([], now=now)
    deps["report_builder"] = capturing_report_builder
    registry = SessionRegistry(deps)
    session = registry.create("session-duration", "guild-1")

    # Advance the clock by 5 minutes 30 seconds before stop().
    clock["t"] = datetime(2026, 7, 25, 18, 5, 30, tzinfo=timezone.utc)
    asyncio.run(session.stop())

    assert captured_meta["duration_label"] == "5m"


def test_stop_meta_includes_caller_title():
    captured_meta = {}

    def capturing_report_builder(segments, meta):
        captured_meta.update(meta)
        return Minutes(summary="s", decisions=[], action_items=[]), b"%PDF-fake"

    deps = _make_deps([])
    deps["report_builder"] = capturing_report_builder
    registry = SessionRegistry(deps)
    session = registry.create("session-title", "guild-1")

    asyncio.run(session.stop("Sponsorship Sync"))

    assert captured_meta["title"] == "Sponsorship Sync"


def test_feed_drops_frames_and_logs_once_past_max_meeting_ms():
    # Fix #4: once elapsed time exceeds max_meeting_ms, further frames are
    # dropped to bound how long a single meeting can run. (The separate
    # re-billing issue this used to reference is gone: audio is now streamed
    # once into a persistent per-speaker Transcribe stream, never replayed.)
    clock = {"t": datetime(2026, 7, 25, 18, 0, tzinfo=timezone.utc)}

    def now():
        return clock["t"]

    audio = FakeAudio()
    stream = FakeTranscriptionStream([])
    deps = _make_deps([stream], audio=audio, now=now)
    deps["max_meeting_ms"] = 60_000  # 1 minute cap
    registry = SessionRegistry(deps)
    session = registry.create("session-cap", "guild-1")

    session.feed("alice-id", "alice", b"frame-within-cap", ts_ms=0)
    assert len(audio.decode_calls) == 1

    # Advance well past the cap.
    clock["t"] = datetime(2026, 7, 25, 18, 2, 0, tzinfo=timezone.utc)
    session.feed("alice-id", "alice", b"frame-past-cap", ts_ms=120_000)

    # The past-cap frame was dropped: no additional decode call, and the
    # buffer only contains the earlier, within-cap frame.
    assert len(audio.decode_calls) == 1
    assert stream.sent == [b"frame-within-cap"]


def test_feed_does_not_drop_frames_when_max_meeting_ms_is_none():
    # With no cap (the default -- max_meeting_ms absent/None), a meeting runs
    # indefinitely: frames are buffered no matter how much time has elapsed,
    # well past the OLD 4h default.
    clock = {"t": datetime(2026, 7, 25, 18, 0, tzinfo=timezone.utc)}

    def now():
        return clock["t"]

    audio = FakeAudio()
    # One speaker ("alice-id") -> one stream created on her first frame.
    stream = FakeTranscriptionStream([])
    deps = _make_deps([stream], audio=audio, now=now)
    assert deps.get("max_meeting_ms") is None  # no cap by default
    registry = SessionRegistry(deps)
    session = registry.create("session-nocap", "guild-1")

    session.feed("alice-id", "alice", b"frame-1", ts_ms=0)

    # Advance the clock 12 hours -- far beyond the old 4h cap.
    clock["t"] = datetime(2026, 7, 26, 6, 0, tzinfo=timezone.utc)
    session.feed("alice-id", "alice", b"frame-2", ts_ms=43_200_000)

    # Both frames were buffered: nothing dropped.
    assert len(audio.decode_calls) == 2
    assert stream.sent == [b"frame-1", b"frame-2"]


def test_stop_deregisters_even_on_report_builder_error():
    def boom(segments, meta):
        raise RuntimeError("report builder exploded")

    deps = _make_deps([])
    deps["report_builder"] = boom
    registry = SessionRegistry(deps)
    session = registry.create("session-err", "guild-1")

    with pytest.raises(RuntimeError):
        asyncio.run(session.stop())

    assert registry.get("session-err") is None
    assert session._speakers == {}


def test_caller_controlled_speaker_ids_stay_plain_dict_keys():
    """speaker_id is attacker/caller-controlled (decoded straight off the WS
    binary frame with no validation upstream -- see api/routers/meetings.py).
    It is now only ever used as an in-memory dict key and a display label, so
    even hostile values are inert; see
    test_session_lifecycle_never_touches_the_filesystem for the guarantee that
    nothing turns one into a path again."""
    audio = FakeAudio()
    malicious_ids = ["../../etc/evil", "/abs/path/evil", "../../../../tmp/pwned"]
    streams = [FakeTranscriptionStream([]) for _ in malicious_ids]
    deps = _make_deps(streams, audio=audio)
    registry = SessionRegistry(deps)
    session = registry.create("session-traversal", "guild-1")

    for speaker_id in malicious_ids:
        session.feed(speaker_id, "display", b"frame", ts_ms=0)

    # Raw ids remain distinct dict keys, untouched and un-sanitized.
    assert set(session._speakers.keys()) == set(malicious_ids)


# NOTE: a test named test_transcript_view_tolerates_new_speaker_added_during
# _iteration used to live here. It covered a speaker being inserted while
# transcript_view() was SUSPENDED AT AN AWAIT mid-iteration. transcript_view()
# no longer awaits anything -- it reads words the live stream has already
# finalized -- so that interleaving is structurally impossible now. The
# remaining risk (a worker-thread feed() resizing self._speakers while the
# reader's comprehension walks it) is covered by the test below, which is
# verified to fail if transcript_view()'s lock is removed.


def test_feed_from_worker_thread_races_readers_without_crashing():
    """Regression for the thread-safety follow-up: feed() runs on a WORKER
    THREAD in production (asyncio.to_thread from the WS ingest loop) while
    transcript_view()/_meta() run on the event-loop thread. Both iterate
    self._speakers.values() directly, so without a lock a comprehension can
    observe the dict resizing mid-iteration and raise "RuntimeError: dictionary
    changed size during iteration".

    The reader here drives the REAL public entry points (transcript_view() and
    _meta()) rather than hand-reproducing their internals -- otherwise the test
    would still pass if the locking were stripped out of transcript_view()
    itself, which is the exact surface under test."""
    audio = FakeAudio()
    # Sustained insertion (not one burst) with enough speakers that a reader's
    # comprehension is virtually always mid-iteration when the dict resizes.
    # Verified to fail with "dictionary changed size during iteration" when the
    # lock is removed from transcript_view(); a smaller/burstier version of this
    # test does NOT reproduce it.
    n_feeders, per_feeder = 4, 150
    deps = _make_deps([], audio=audio)
    deps["make_transcription_stream"] = lambda: FakeTranscriptionStream([])
    registry = SessionRegistry(deps)
    session = registry.create("session-race", "guild-1")

    errors: list[BaseException] = []
    stop_reading = False

    # Force the GIL to hand off far more often than the 5ms default. Without
    # this the reader's comprehension finishes inside a single scheduling slice
    # and the race simply never interleaves, so the test passes even with the
    # locking removed -- i.e. it would be decorative rather than a regression
    # test. Restored in the finally below.
    original_switch_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)

    def feed_worker(w: int) -> None:
        try:
            for i in range(per_feeder):
                sid = f"speaker-{w}-{i}"
                session.feed(sid, f"display-{w}-{i}", _pcm(20), ts_ms=i)
        except BaseException as exc:  # noqa: BLE001 -- capture any race-induced crash
            errors.append(exc)

    def read_worker() -> None:
        try:
            while not stop_reading:
                session._meta()
                # The real reader path, on its own event loop (production runs
                # it on the main loop while feed() is off on a worker thread).
                asyncio.run(session.transcript_view())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    reader = threading.Thread(target=read_worker)
    reader.start()
    try:
        feeders = [threading.Thread(target=feed_worker, args=(w,)) for w in range(n_feeders)]
        for t in feeders:
            t.start()
        for t in feeders:
            t.join(timeout=30)
    finally:
        stop_reading = True
        reader.join(timeout=30)
        sys.setswitchinterval(original_switch_interval)

    assert errors == []
    expected = {f"speaker-{w}-{i}" for w in range(n_feeders) for i in range(per_feeder)}
    assert set(session._speakers.keys()) == expected

    # The session must still be fully usable afterwards (lock released cleanly,
    # no deadlock left behind).
    view = asyncio.run(session.transcript_view())
    assert isinstance(view, list)
    result = asyncio.run(session.stop())
    assert result.transcript == ""


def test_stop_finalizes_speakers_concurrently():
    """Each speaker's aclose() waits on AWS to flush, bounded at
    FINAL_FLUSH_TIMEOUT_S (15s). Finalizing sequentially makes worst-case /stop
    latency N x 15s -- a 10-speaker meeting would block for 150s, well past the
    bot's HTTP timeout, and the minutes would be lost entirely. The per-speaker
    work is independent, so it must overlap."""
    delay = 0.05
    n_speakers = 6

    class SlowStream(FakeTranscriptionStream):
        async def aclose(self):
            await asyncio.sleep(delay)
            return await super().aclose()

    deps = _make_deps([])
    deps["make_transcription_stream"] = lambda: SlowStream([])
    registry = SessionRegistry(deps)
    session = registry.create("session-parallel", "guild-1")
    for i in range(n_speakers):
        session.feed(f"speaker-{i}", f"name-{i}", _pcm(20), ts_ms=i * 20)

    loop = asyncio.new_event_loop()
    try:
        started = loop.time()
        loop.run_until_complete(session.stop())
        elapsed = loop.time() - started
    finally:
        loop.close()

    # Sequential would be >= n_speakers * delay; concurrent is ~delay.
    assert elapsed < delay * (n_speakers / 2), (
        f"stop() took {elapsed:.3f}s for {n_speakers} speakers at {delay}s each "
        "-- finalization is serialized"
    )


# --- interleaving ------------------------------------------------------------


def test_a_long_turn_is_split_so_others_can_interleave():
    """Segments are sorted by start time, so a segment that spans minutes gets
    emitted as one block -- and anyone who spoke DURING it is printed after it,
    reading as though they replied before the other person spoke.

    Splitting only on a 3s gap never fires during continuous speech, so a
    monologue was a single segment. Long turns must be broken up on duration
    too, even with no pause, so overlapping speech interleaves in real order.
    """
    alice = [{"text": f"a{i}", "start_ms": i * 1000} for i in range(60)]
    bob = [{"text": f"b{i}", "start_ms": 30_000 + i * 1000} for i in range(5)]

    segments = words_to_segments("alice", alice) + words_to_segments("bob", bob)
    segments.sort(key=lambda s: s.start_ms)

    speakers_in_order = [s.speaker for s in segments]
    assert "bob" in speakers_in_order
    bob_at = speakers_in_order.index("bob")
    # Alice must both precede AND follow Bob: he spoke in the middle of her turn.
    assert "alice" in speakers_in_order[:bob_at], "Bob's interjection sorted before all of Alice"
    assert "alice" in speakers_in_order[bob_at + 1 :], (
        "Alice's speech after Bob was emitted before him -- the interleaving is lost"
    )


def test_no_segment_spans_more_than_the_cap():
    """The cap is what bounds how far out of order an overlapping speaker can
    be pushed, so it must hold even for unbroken speech."""
    words = [{"text": f"w{i}", "start_ms": i * 500} for i in range(120)]  # 60s, no pauses

    for segment in words_to_segments("alice", words):
        span = segment.text.count(" ")  # words-1
        assert span * 500 <= _MAX_SEGMENT_MS, f"segment spans {span * 500}ms"


def test_a_natural_pause_still_starts_a_new_segment():
    """The gap rule still applies; the duration cap is an addition, not a
    replacement."""
    words = [
        {"text": "hello", "start_ms": 0},
        {"text": "there", "start_ms": 400},
        {"text": "later", "start_ms": 9000},
    ]
    segments = words_to_segments("alice", words)

    assert [(s.start_ms, s.text) for s in segments] == [(0, "hello there"), (9000, "later")]


# --- end-of-audio barrier ----------------------------------------------------


def test_stop_waits_for_end_of_audio_before_finalizing():
    """The WS ingest loop and the POST /stop handler are independent tasks, so
    /stop can begin while the last frames are still being read off the socket.
    Those frames were then refused and the transcript lost its tail.

    The bot signals end-of-audio on the WS after it stops recording. Because the
    socket delivers in order, seeing that signal means every prior audio frame
    has already been fed -- so stop() must wait for it before snapshotting."""
    stream = FakeTranscriptionStream([{"text": "last words", "start_ms": 0}])
    deps = _make_deps([stream])
    registry = SessionRegistry(deps)
    session = registry.create("session-barrier", "guild-1")

    async def scenario():
        stop_task = asyncio.ensure_future(session.stop())
        await asyncio.sleep(0)  # let stop() start and reach the barrier

        # A tail frame still coming off the socket must still be accepted.
        session.feed("alice-id", "alice", _pcm(20), ts_ms=0)
        session.mark_audio_complete()
        return await stop_task

    result = asyncio.run(scenario())

    assert stream.sent, "the tail frame was refused; stop() did not wait"
    assert "last words" in result.transcript


def test_stop_does_not_hang_when_end_of_audio_never_arrives():
    """An older bot, a crash, or a dropped socket means the signal never comes.
    Finalize must proceed on a bounded wait rather than hanging /stop."""
    deps = _make_deps([])
    registry = SessionRegistry(deps)
    session = registry.create("session-nosignal", "guild-1")

    result = asyncio.run(asyncio.wait_for(session.stop(), 5))

    assert result.transcript == ""


def test_feed_is_still_refused_after_the_audio_barrier_passes():
    """Once end-of-audio is signalled and finalize is under way, a late frame
    has nowhere to go and must not be appended to an already-closed stream."""
    stream = FakeTranscriptionStream([])
    deps = _make_deps([stream])
    registry = SessionRegistry(deps)
    session = registry.create("session-after", "guild-1")
    session.mark_audio_complete()

    asyncio.run(session.stop())
    before = len(stream.sent)
    session.feed("bob-id", "bob", _pcm(20), ts_ms=0)

    assert len(stream.sent) == before


def test_dropped_audio_is_counted_and_reported_at_stop(caplog):
    """Every path that discards audio was silent. Diagnosing "words went
    missing" then meant reading code and building repros rather than reading a
    log -- which is how most of this service's bugs were actually found.

    One line per meeting now names what was lost and why."""

    class DeadDecoder:
        """Mirrors decoder.decode's best-effort contract: a malformed packet
        yields b"" rather than raising."""

        def decode(self, opus_frame_bytes):
            return b""

    class DeadAudio:
        def make_decoder(self):
            return DeadDecoder()

    deps = _make_deps([FakeTranscriptionStream([])], audio=DeadAudio())
    registry = SessionRegistry(deps)
    session = registry.create("session-drops", "guild-1")
    session.mark_audio_complete()

    session.feed("alice-id", "alice", b"real-opus-bytes", ts_ms=0)
    assert session.drop_summary() == {"undecodable_frame": 1}

    with caplog.at_level("WARNING"):
        asyncio.run(session.stop())

    joined = " ".join(r.message for r in caplog.records)
    assert "undecodable_frame" in joined, f"the loss was never reported: {joined}"


def test_no_drop_report_when_nothing_was_dropped():
    """A clean meeting must not emit a scary warning."""
    deps = _make_deps([FakeTranscriptionStream([])])
    registry = SessionRegistry(deps)
    session = registry.create("session-clean", "guild-1")
    session.mark_audio_complete()
    session.feed("alice-id", "alice", _pcm(20), ts_ms=0)

    assert session.drop_summary() == {}


def test_discard_reports_drops_too(caplog):
    """discard() is the ABRUPT path -- the bot died, the socket dropped, the
    meeting was lost. That is the case where knowing what was discarded matters
    most, and it was the one path that never reported."""

    class DeadDecoder:
        def decode(self, opus_frame_bytes):
            return b""

    class DeadAudio:
        def make_decoder(self):
            return DeadDecoder()

    deps = _make_deps([FakeTranscriptionStream([])], audio=DeadAudio())
    registry = SessionRegistry(deps)
    session = registry.create("session-discard-drops", "guild-1")
    session.feed("alice-id", "alice", b"real-opus-bytes", ts_ms=0)

    with caplog.at_level("WARNING"):
        session.discard()

    joined = " ".join(r.message for r in caplog.records)
    assert "undecodable_frame" in joined, f"an abrupt teardown reported nothing: {joined}"
