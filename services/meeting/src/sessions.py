"""Stateful in-process session registry + meeting lifecycle orchestration.

All collaborators (transcriber factory, audio decoder, report builder, clock)
are injected via a ``deps`` dict so this module has zero direct dependency on
AWS/network -- tests use fakes exclusively.

Nothing here touches the filesystem, and nothing retains the audio: each PCM
chunk is handed straight to that speaker's Transcribe stream and dropped.
Audio is never persisted and never returned (see ``stop``).

Transcription is LIVE: each speaker gets one persistent AWS Transcribe stream
(``src/stt/transcribe.py``) opened on their first frame and fed as audio
arrives. ``transcript_view()`` is then a pure read of what that stream has
finalized so far -- no AWS call, no audio replayed -- and ``stop()`` just closes
the streams and collects their final results.

This replaced a design that re-transcribed each speaker's WHOLE accumulated
buffer on every poll and again at stop. That made AWS cost grow with the SQUARE
of meeting length (k polls re-sent ~k/2 copies of the audio), made each poll
progressively slower, and forced every speaker's PCM to be retained in memory
for the life of the meeting. None of that is true any more: nothing here keeps
the audio, because nothing replays it.
"""

import asyncio
import base64
import logging
import threading

from src.contracts import Segment, StopResponse
from src.pipeline.transcript import assemble_transcript

_logger = logging.getLogger("meeting.audit")


# A pause longer than this ends an utterance. Conversational pauses between
# sentences run a few hundred ms, so 1.5s is comfortably past "drawing breath"
# without splitting mid-thought.
_SEGMENT_GAP_MS = 1500

# Hard cap on how long one segment may span with no pause at all. Segments are
# ordered as whole blocks, so this bounds how far an overlapping speaker can be
# displaced -- at 5s, an interjection can never appear more than ~5s late.
# Measured: at 10s an interjection inside the first 10s of someone's turn still
# sorted after their whole block, which is the case this exists to fix.
_MAX_SEGMENT_MS = 5_000

# How long stop() waits for the bot's end-of-audio signal before finalizing
# anyway. Covers an older bot that never sends it, a crashed bot, or a dropped
# socket -- none of which may hang /stop.
AUDIO_DRAIN_TIMEOUT_S = 5.0


class SessionAlreadyExistsError(ValueError):
    """Raised by ``SessionRegistry.create`` when the session_id is already active."""


def words_to_segments(
    speaker: str,
    words: list[dict],
    gap_ms: int = _SEGMENT_GAP_MS,
    max_segment_ms: int = _MAX_SEGMENT_MS,
) -> list[Segment]:
    """Group a flat, chronological word list into segments for one speaker.

    A new segment starts on either of two conditions:

    * **A pause** longer than ``gap_ms`` -- the natural boundary between
      utterances.
    * **Duration** -- the segment has run for ``max_segment_ms`` with no pause.

    The duration cap is what makes overlapping speech readable. Segments are
    sorted by start time to build the transcript, so a segment is emitted as one
    indivisible block: if someone talks for two minutes straight, a pause-only
    rule produces a single segment, and everyone who spoke DURING those two
    minutes is printed after all of it -- reading as though they replied before
    the other person had spoken. Capping the span bounds how far out of order an
    overlapping speaker can be pushed.
    """
    segments: list[Segment] = []
    current_words: list[str] = []
    current_start: int | None = None
    last_start: int | None = None

    for word in words:
        start = word["start_ms"]
        too_long = current_start is not None and (start - current_start) > max_segment_ms
        gapped = current_words and last_start is not None and (start - last_start) > gap_ms
        if gapped or too_long:
            segments.append(
                Segment(speaker=speaker, start_ms=current_start, text=" ".join(current_words))
            )
            current_words = []
            current_start = None
        if current_start is None:
            current_start = start
        current_words.append(str(word["text"]))
        last_start = start

    if current_words:
        segments.append(
            Segment(speaker=speaker, start_ms=current_start, text=" ".join(current_words))
        )

    return segments


# 16 kHz mono s16le -> 2 bytes per sample * 16 samples per ms.
_PCM_BYTES_PER_MS = 32

# How far a frame's wall-clock ts_ms may run ahead of where the buffer says it
# should land before we treat the difference as a real silence gap rather than
# ordinary network/scheduling jitter. Discord delivers a frame every ~20ms, so
# 200ms is ~10 frames of slack: comfortably above jitter, far below any pause
# that matters in a transcript.
_ANCHOR_GAP_TOLERANCE_MS = 200


class _SpeakerBuffer:
    def __init__(self, display_name: str, stream, decoder):
        self.display_name = display_name
        # This speaker's PERSISTENT Transcribe stream. Audio is pushed into it
        # as it arrives and is never replayed: each second of speech is billed
        # once, and polling the rolling transcript is free.
        #
        # The previous design re-ran a fresh transcription over the speaker's
        # whole accumulated buffer on every transcript_view() AND at stop(), so
        # cost grew with the SQUARE of meeting length (k polls re-sent ~k/2
        # copies of the audio) and each poll got progressively slower.
        self.stream = stream
        # Opus decode is STATEFUL per stream (packet-loss concealment,
        # internal decoder history) -- this speaker's own decoder instance
        # must be fed only this speaker's packets, in order, for the life of
        # the session. See src/audio/decoder.py's OpusStreamDecoder docstring.
        self.decoder = decoder
        # Bytes of audio sent to the stream so far. Tracked in BYTES rather
        # than milliseconds on purpose: the resampler emits variable-length
        # chunks, and flooring each one to whole milliseconds would accumulate
        # a monotonic undercount that eventually fakes a silence gap mid-speech.
        # Converting once, at read time, bounds the error to <1ms total.
        #
        # NOTE this is a COUNT, not the audio: nothing retains the PCM, because
        # nothing replays it.
        self.buffered_bytes = 0
        # Anchors mapping this speaker's BUFFER timeline onto the MEETING
        # timeline, as ``(buffer_offset_ms, meeting_ts_ms)`` pairs in
        # increasing buffer_offset_ms order.
        #
        # Why this is needed: AWS Transcribe reports word start_ms relative to
        # the start of this speaker's own concatenated PCM buffer, and that
        # buffer contains ONLY the frames they actually spoke -- inter-utterance
        # silence is never written to it. So a speaker's buffer timeline is
        # their speaking time, compressed; it drifts further from wall-clock
        # with every pause. A single first-frame offset would anchor only their
        # first word (that was the previous behaviour, and it made transcripts
        # of any real meeting interleave in the wrong order and collapse into
        # one segment per speaker, since the 3s-gap split rule never saw a gap).
        #
        # Instead we record a new anchor each time a frame arrives later than
        # the buffer accounts for -- i.e. at the far side of each real silence.
        # Word times are then mapped through the nearest preceding anchor, so
        # real pauses reappear in the output and cross-speaker sorting reflects
        # actual chronology.
        self.anchors: list[tuple[int, int]] = []

    @property
    def buffered_ms(self) -> int:
        return self.buffered_bytes // _PCM_BYTES_PER_MS

    def append(self, pcm_bytes: bytes, ts_ms: int) -> None:
        # A packet that decoded to nothing (DTX, or a malformed frame the
        # decoder swallowed) carries no audio and must not anchor: doing so
        # would attribute the start of the next REAL audio to this dud frame's
        # timestamp instead of its own.
        if not pcm_bytes:
            return
        self._note_anchor(ts_ms)
        self.stream.send(pcm_bytes)
        self.buffered_bytes += len(pcm_bytes)

    def _note_anchor(self, ts_ms: int) -> None:
        """Record an anchor if this frame starts a new run of contiguous audio.

        Called BEFORE the frame is appended, so ``self.buffered_ms`` is the
        buffer offset at which this frame's audio begins.
        """
        if not self.anchors:
            self.anchors.append((0, ts_ms))
            return
        offset, meeting_ts = self.anchors[-1]
        # Where this frame *would* land if the speaker had been talking
        # continuously since the last anchor.
        expected_ts = meeting_ts + (self.buffered_ms - offset)
        if ts_ms - expected_ts > _ANCHOR_GAP_TOLERANCE_MS:
            self.anchors.append((self.buffered_ms, ts_ms))

    def snapshot(self) -> tuple[str, list[tuple[int, int]]]:
        """Copy what a reader needs from this buffer. Callers MUST hold the
        session lock: feed() runs on a worker thread and mutates both of these
        (``display_name`` is re-assigned on every frame)."""
        return self.display_name, list(self.anchors)

    @staticmethod
    def _to_meeting_ms(buffer_ms: int, anchors: list[tuple[int, int]]) -> int:
        """Map a buffer-relative time onto the meeting timeline via the nearest
        preceding anchor. Anchors are ordered, and there are at most a handful
        per speaker (one per silence), so a linear scan is fine.

        Known residual: a word AWS reports slightly BEFORE an anchor maps
        through the previous anchor, moving it back by that silence gap.
        Snapping words near a boundary forward was tried and reverted -- the
        distance between anchors in BUFFER time is often only a frame or two
        (a long wall-clock pause is no audio at all), so any snap window wide
        enough to absorb AWS's jitter also swallows genuinely earlier speech,
        which is both more common and more wrong.
        """
        if not anchors:
            return buffer_ms
        offset, meeting_ts = anchors[0]
        for anchor_offset, anchor_ts in anchors:
            if anchor_offset > buffer_ms:
                break
            offset, meeting_ts = anchor_offset, anchor_ts
        return meeting_ts + (buffer_ms - offset)

    def _map(self, words: list[dict], anchors: list[tuple[int, int]]) -> list[dict]:
        return [
            {**word, "start_ms": self._to_meeting_ms(word["start_ms"], anchors)} for word in words
        ]

    def words_so_far(self, anchors: list[tuple[int, int]]) -> list[dict]:
        """Finalized words the live stream has produced up to now, mapped onto
        the meeting timeline. Cheap: a read, not a transcription. ``anchors``
        must be a snapshot taken by the caller under the session lock."""
        return self._map(self.stream.words(), anchors)

    async def finalize(self, anchors: list[tuple[int, int]]) -> list[dict]:
        """Close the stream and return every word it produced, mapped onto the
        meeting timeline. Closing flushes AWS's last partial results into final
        ones, so this can return more than ``words_so_far`` did a moment ago."""
        return self._map(await self.stream.aclose(), anchors)


class MeetingSession:
    def __init__(self, session_id: str, guild_id: str, deps: dict, on_finalize):
        self.session_id = session_id
        self.guild_id = guild_id
        self._deps = deps
        self._on_finalize = on_finalize
        self._started_at = deps["now"]()
        self._speakers: dict[str, _SpeakerBuffer] = {}
        # Guards all reads/mutations of self._speakers (dict shape) and of any
        # individual buffer's mutable state (display_name, anchors,
        # buffered_bytes). feed() runs synchronously on a
        # worker thread (via asyncio.to_thread from the WS ingest loop) while
        # transcript_view()/stop()/_meta() run on the event-loop thread -- a
        # plain dict/list is not safe under that cross-thread access pattern
        # (e.g. a comprehension over self._speakers.values() can raise
        # "dictionary changed size during iteration" if feed() inserts a new
        # speaker mid-comprehension). Non-reentrant by design: lock scopes
        # below are kept small and never nested, and never held across an
        # ``await``.
        self._lock = threading.Lock()
        # Each speaker's Transcribe stream needs the event loop to schedule its
        # pump on, but speakers are first seen inside feed(), which runs on a
        # WORKER thread with no running loop. Capture it here instead: __init__
        # runs on the loop (the async WS handler calls registry.create). None in
        # sync unit tests, where the injected fake streams don't need a loop.
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._finalized = False
        # Set the instant finalization begins. stop() snapshots the buffers and
        # then awaits transcription + report building, during which the WS
        # ingest loop is still live (the bot only closes the socket after /stop
        # returns). Without this, a frame arriving in that window is appended to
        # a buffer that has already been transcribed -- accepted, then silently
        # dropped from the transcript. Refuse it outright instead.
        self._stopping = False
        # Set when the bot signals it has sent every audio frame. The WS ingest
        # loop and the POST /stop handler are independent tasks, so without this
        # /stop could finalize while the last frames were still being read off
        # the socket -- and _stopping would then refuse them, cutting the tail
        # off the transcript. The socket delivers in order, so the signal
        # arriving means all prior audio has already been fed.
        self._audio_complete = asyncio.Event()
        # Every path that discards audio increments one of these. They are
        # reported as a single line at stop, because "words went missing" with
        # nothing in the logs is how most of this service's bugs reached
        # production -- each one had to be found by reading code and building a
        # repro instead of reading a log.
        self._drops: dict[str, int] = {}
        self._cap_hit_logged = False

    def feed(self, speaker_id: str, display_name: str, opus_frame_bytes: bytes, ts_ms: int) -> None:
        # Finalization has started (or finished): anything arriving now could
        # never reach the transcript, so drop it rather than buffer it into a
        # snapshot that has already been taken. See ``_stopping`` in __init__.
        if self._stopping:
            self.note_drop("after_stop_began")
            return

        # Bound how long a single meeting can run, so a forgotten one cannot hold
        # an AWS stream open indefinitely (Transcribe caps a session at 4h in
        # any case). Frames past the cap are dropped, not buffered.
        max_meeting_ms = self._deps.get("max_meeting_ms")
        if max_meeting_ms is not None:
            elapsed_ms = (self._deps["now"]() - self._started_at).total_seconds() * 1000
            if elapsed_ms > max_meeting_ms:
                if not self._cap_hit_logged:
                    self._cap_hit_logged = True
                    _logger.warning(
                        "session %s exceeded max_meeting_ms=%s (elapsed=%.0fms); "
                        "dropping further audio frames",
                        self.session_id,
                        max_meeting_ms,
                        elapsed_ms,
                    )
                self.note_drop("past_max_meeting_ms")
                return

        # Get-or-create this speaker's buffer (and its OWN stateful Opus
        # decoder) under the lock -- creation touches shared state
        # (self._speakers).
        with self._lock:
            buf = self._speakers.get(speaker_id)
            if buf is None:
                stream = self._deps["make_transcription_stream"]()
                stream.start(self._loop)
                decoder = self._deps["audio"].make_decoder()
                buf = _SpeakerBuffer(display_name, stream, decoder)
                self._speakers[speaker_id] = buf
            else:
                buf.display_name = display_name

        # Opus decode is CPU work with no shared-state touch (each speaker's
        # decoder is only ever driven by this speaker's own feed() calls) --
        # do it OUTSIDE the lock so it doesn't block readers any longer than
        # necessary.
        pcm = buf.decoder.decode(opus_frame_bytes)
        # Real audio in, nothing out: the decoder swallowed an error (see
        # decoder.decode's best-effort contract). Silent until now.
        if opus_frame_bytes and not pcm:
            self.note_drop("undecodable_frame")

        with self._lock:
            buf.append(pcm, ts_ms)

    async def transcript_view(self) -> list[Segment]:
        segments: list[Segment] = []
        # Snapshot BOTH the speaker list and each buffer's accumulated PCM
        # under the lock, then release it before doing any async transcribe
        # work. feed() runs synchronously on a worker thread (via
        # asyncio.to_thread from the live WS ingest loop) and can insert a
        # brand-new speaker into self._speakers, or append to an existing
        # buffer's anchors, at any time -- including while this coroutine
        # is suspended at an `await`. Snapshotting under a short, non-async
        # lock section avoids both "dictionary changed size during iteration"
        # and reading a half-updated anchor list. A speaker who first appears
        # (or speaks more) mid-poll simply shows up fully on the next poll (or
        # at stop()) instead -- an acceptable, self-correcting gap.
        with self._lock:
            snapshot = [(buf, *buf.snapshot()) for buf in self._speakers.values()]
        for buf, display_name, anchors in snapshot:
            segments.extend(words_to_segments(display_name, buf.words_so_far(anchors)))
        segments.sort(key=lambda s: s.start_ms)
        return segments

    def _meta(self) -> dict:
        elapsed_s = (self._deps["now"]() - self._started_at).total_seconds()
        minutes = max(0, int(elapsed_s // 60))
        with self._lock:
            participants = [buf.display_name for buf in self._speakers.values()]
        return {
            # No code-invented title: the meeting title is LLM-generated (see
            # minutes.summarize_minutes / Minutes.title). Left blank so the PDF
            # falls back to a clean default only if the LLM produced none.
            "title": "",
            "started_at": str(self._started_at),
            "duration_label": f"{minutes}m",
            "participants": participants,
        }

    def note_drop(self, reason: str, count: int = 1) -> None:
        """Record that audio or words were discarded. Cheap and lock-free: an
        approximate count from a racing worker thread is fine, an exact one is
        not worth a lock on the ingest path."""
        self._drops[reason] = self._drops.get(reason, 0) + count

    def drop_summary(self) -> dict[str, int]:
        return dict(self._drops)

    def _report_drops(self) -> None:
        if not self._drops:
            return
        _logger.warning(
            "session %s finished with discarded audio: %s",
            self.session_id,
            ", ".join(f"{reason}={count}" for reason, count in sorted(self._drops.items())),
        )

    def mark_audio_complete(self) -> None:
        """Called by the WS ingest loop when the bot signals end-of-audio.

        Safe to call more than once, and before stop() -- the flag is what
        matters, not the ordering.
        """
        self._audio_complete.set()

    async def _await_audio_complete(self) -> None:
        """Block until the tail of the meeting has been fed, or give up."""
        if self._audio_complete.is_set():
            return
        timeout = self._deps.get("audio_drain_timeout_s", AUDIO_DRAIN_TIMEOUT_S)
        try:
            await asyncio.wait_for(self._audio_complete.wait(), timeout)
        except asyncio.TimeoutError:
            _logger.warning(
                "session %s: no end-of-audio signal after %ss; finalizing anyway "
                "(the end of the transcript may be short)",
                self.session_id,
                timeout,
            )

    async def stop(self) -> StopResponse:
        # Wait for the tail BEFORE quiescing: the last frames may still be in
        # flight on the WS, and refusing them is exactly how the transcript lost
        # its ending. Stream them in, THEN cut.
        await self._await_audio_complete()
        # Now quiesce, so no frame can slip in between the snapshot and the end
        # of finalization.
        self._stopping = True
        try:
            segments: list[Segment] = []
            # Same lock-scoped snapshot rationale as transcript_view() above:
            # a concurrent feed() must not mutate self._speakers or a
            # buffer's anchors while we're suspended at the await below.
            with self._lock:
                snapshot = [(buf, *buf.snapshot()) for buf in self._speakers.values()]
            # Finalize speakers CONCURRENTLY. Each finalize() closes an AWS
            # stream and waits for its flush (bounded at FINAL_FLUSH_TIMEOUT_S),
            # and the work is independent per speaker -- serializing it would
            # make worst-case /stop latency N x that bound, which for a large
            # meeting exceeds the bot's HTTP timeout and loses the minutes.
            per_speaker = await asyncio.gather(
                *(buf.finalize(anchors) for buf, _, anchors in snapshot),
                return_exceptions=True,
            )
            for (buf, display_name, _anchors), words in zip(snapshot, per_speaker):
                if isinstance(words, BaseException):
                    # One speaker's stream failing must not lose everyone else's
                    # transcript.
                    _logger.warning(
                        "finalizing speaker %s failed; their words are omitted: %s",
                        display_name,
                        words,
                    )
                    self.note_drop("speaker_finalize_failed")
                    continue
                segments.extend(words_to_segments(display_name, words))
            segments.sort(key=lambda s: s.start_ms)

            transcript_text = assemble_transcript(segments)
            # Fix #2: report_builder is sync and does a blocking LLM HTTP call
            # (up to request_timeout_s, default 60s) plus PDF rendering. Offload
            # to a thread so it doesn't stall the event loop for other meetings.
            # The sync internals (llm_client/minutes/pdf) are intentionally left
            # as-is -- thread-offloading at this async boundary is the chosen
            # fix, not an async rewrite of those modules.
            minutes, pdf_bytes = await asyncio.to_thread(
                self._deps["report_builder"], segments, self._meta()
            )
            pdf_b64 = base64.b64encode(pdf_bytes).decode("ascii")

            # Meeting audio is deliberately NOT returned: consumers get the
            # minutes PDF only. Nothing mixes, persists, or ships audio -- the
            # buffered PCM exists solely as transcription input and dies with
            # the session.
            return StopResponse(
                transcript=transcript_text,
                minutes=minutes,
                pdf_b64=pdf_b64,
            )
        finally:
            self._cleanup()

    def discard(self) -> None:
        """Lightweight teardown for abrupt disconnects (e.g. WS drop without a
        preceding ``POST /stop``): deregister the session and abort each
        speaker's Transcribe stream. Deliberately does NOT transcribe/summarize/build a PDF -- those
        are only worth paying for when a consumer actually wants the finalized
        meeting artifacts via ``stop()``.

        Idempotent: safe to call more than once, and safe to call after
        ``stop()`` has already run (both funnel through the same cleanup).
        """
        self._cleanup()

    def _cleanup(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        # Report here rather than in stop(): discard() is the ABRUPT path (the
        # bot died, the socket dropped), which is exactly when knowing what was
        # discarded matters most -- and it was the one path that never reported.
        # _cleanup is idempotent, so this fires once on either route.
        self._report_drops()
        # Covers the discard() path too: once torn down, nothing more is taken.
        self._stopping = True
        # Tear down every speaker's Transcribe stream. stop() has already
        # aclose()d them (abort() is then a no-op), but an abrupt disconnect
        # routes through discard() -> here without ever closing them, and a
        # leaked stream stays open on AWS's side until it idles out.
        with self._lock:
            speakers = list(self._speakers.values())
            self._speakers.clear()
        for buf in speakers:
            try:
                buf.stream.abort()
            except Exception as exc:  # noqa: BLE001 -- teardown must not raise
                _logger.warning("aborting stream for session %s failed: %s", self.session_id, exc)
        self._on_finalize(self.session_id)


class SessionRegistry:
    def __init__(self, deps: dict):
        self._deps = deps
        self._sessions: dict[str, MeetingSession] = {}

    def create(self, session_id: str, guild_id: str) -> MeetingSession:
        if session_id in self._sessions:
            raise SessionAlreadyExistsError(session_id)
        session = MeetingSession(session_id, guild_id, self._deps, on_finalize=self._deregister)
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> MeetingSession | None:
        return self._sessions.get(session_id)

    def _deregister(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
