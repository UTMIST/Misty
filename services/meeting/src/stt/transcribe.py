"""AWS Transcribe streaming wrapper over the async ``amazon-transcribe`` client.

One ``_StreamingTranscription`` per speaker, held open for the whole meeting:
audio is pushed in as it arrives and never replayed, so each second of speech is
billed exactly once and reading the rolling transcript is free.

The real client (``amazon_transcribe.client.TranscribeStreamingClient``) is only
imported lazily, and only when a client isn't injected — this keeps tests free
of any AWS SDK/network/credential dependency.
"""

import asyncio
import contextlib
import logging

LANGUAGE_CODE = "en-US"
MEDIA_ENCODING = "pcm"
PRONUNCIATION_ITEM_TYPE = "pronunciation"

# How long aclose() waits for AWS to flush its final results after end_stream().
FINAL_FLUSH_TIMEOUT_S = 15

# Opening a Transcribe session has no timeout of its own. Without this a hung
# connect blocks the pump, aclose()'s `await self._task` never returns, and
# MeetingSession.stop() -- which has no timeout either -- hangs with it, leaving
# the session registered and the meeting unfinalized.
SESSION_CONNECT_TIMEOUT_S = 20

# Consecutive AWS sessions that may end without accepting any audio before we
# stop reopening for this speaker (prevents a hot reopen loop).
_MAX_BARREN_SESSIONS = 3

# Consecutive session failures tolerated before this speaker is given up on.
_MAX_SESSION_FAILURES = 3

_logger = logging.getLogger("meeting.audit")


def _words_from_result(result, offset_ms: int = 0) -> list[dict]:
    """Pronunciation items of a FINAL result, as {text, start_ms} shifted by
    ``offset_ms`` (the audio already sent through earlier AWS sessions)."""
    if not result.alternatives:
        return []
    return [
        {"text": item.content, "start_ms": round((item.start_time or 0.0) * 1000) + offset_ms}
        for item in (result.alternatives[0].items or [])
        if item.item_type == PRONUNCIATION_ITEM_TYPE
    ]


class _StreamingTranscription:
    """One speaker's PERSISTENT Transcribe session.

    Audio is pushed in with ``send()`` as it arrives and is never replayed, so
    each second of speech is billed exactly once and reading the rolling
    transcript costs nothing. (This replaced a one-shot transcriber that re-ran
    over a speaker's whole accumulated buffer on every poll, making AWS cost
    grow with the SQUARE of meeting length and each poll progressively slower.)

    Threading: ``send()`` is called from the WS ingest worker thread, while the
    pump runs on the event loop. Handoff goes through ``call_soon_threadsafe``,
    so ``send()`` never blocks the ingest path and never touches asyncio state
    from the wrong thread.

    Public contract: ``start`` / ``send`` / ``words`` / ``aclose`` / ``abort``,
    plus optional ``set_final_callback`` and the ``incomplete`` flush indicator.

    Restarts: AWS ends a streaming session on its own (idle timeout, or the 4h
    per-stream cap). Each session reports word times relative to ITS OWN start,
    so a per-session byte offset shifts each new session's words back onto
    the speaker's single continuous buffer timeline -- which is the timebase
    ``sessions.py``'s anchors expect.
    """

    def __init__(self, region: str, client=None, sample_rate: int = 16000):
        self._region = region
        self._client = client
        self._sample_rate = sample_rate
        # s16 mono: 2 bytes per sample. Derived from the configured rate so a
        # non-16kHz stream cannot silently produce wrong restart offsets.
        self._bytes_per_ms = sample_rate * 2 // 1000
        self._queue: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._words: list[dict] = []
        self._on_final = None
        self.incomplete = False
        self._closing = False
        # Distinct from ``_closing``: abort() is a hard teardown, and it can
        # land BEFORE the scheduled _spawn() has run. ``_spawn`` checks this so
        # it doesn't create a pump that would park forever on a queue nothing
        # will feed. aclose() must NOT set it -- that path still needs the pump
        # to run in order to flush and collect final results.
        self._aborted = False
        # Bytes handed to AWS across ALL sessions so far (``_sent_bytes`` counts
        # what send() accepted; ``_delivered_bytes`` what actually reached AWS).
        self._sent_bytes = 0
        self._delivered_bytes = 0

    def set_final_callback(self, callback) -> None:
        """Observe each immutable final result on the pump's event loop.

        The observer must be synchronous/nonblocking. Subtitle delivery never
        owns the transcript: store words first and isolate observer failures.
        """
        self._on_final = callback

    def _resolve_client(self):
        if self._client is None:
            from amazon_transcribe.client import TranscribeStreamingClient

            self._client = TranscribeStreamingClient(region=self._region)
        return self._client

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Arm the stream. SYNCHRONOUS and callable from any thread, because
        speakers are first seen inside ``feed()``, which runs on the WS ingest
        worker thread -- there is no running loop there to await on. ``loop``
        must therefore be passed in from the event-loop side (the session
        captures it at construction); it defaults to the running loop when
        called from the loop itself, as tests do.
        """
        self._loop = loop or asyncio.get_running_loop()
        self._queue = asyncio.Queue()

        def _spawn() -> None:
            if self._aborted:
                return  # torn down before we got scheduled; never start
            self._task = asyncio.ensure_future(self._pump())

        self._loop.call_soon_threadsafe(_spawn)

    def send(self, pcm: bytes) -> None:
        """Queue audio for AWS. Safe to call from a worker thread; never blocks.

        Dropped silently once closing/closed -- a late frame has nowhere to go,
        and raising here would propagate into the WS ingest loop.
        """
        if self._closing or self._loop is None or self._queue is None:
            # Silent until now: a frame landing in the closing window just
            # vanished, which is exactly the "transcript cut off" symptom.
            _logger.debug("dropping %s bytes of audio: stream is closing", len(pcm))
            return
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, pcm)
        except RuntimeError:
            pass  # loop already closed; nothing to do
        else:
            # Only count audio that was actually queued, so _sent_bytes and
            # _delivered_bytes stay comparable if an enqueue ever fails.
            self._sent_bytes += len(pcm)

    def words(self) -> list[dict]:
        """Finalized words so far, on the speaker's buffer timeline. A read --
        no AWS call, no audio replayed."""
        return list(self._words)

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _await_task(self) -> None:
        """Wait for the pump, tolerating the window before _spawn() has run."""
        for _ in range(100):
            if self._task is not None or self._aborted:
                break
            await asyncio.sleep(0)

    async def _signal_end_of_audio(self) -> bool:
        """Enqueue the close sentinel through the SAME call_soon_threadsafe path
        send() uses, so it cannot overtake audio whose send() has RETURNED.

        A direct put_nowait() here would run immediately while send()'s enqueue
        is still a pending callback -- the pump would end the stream and that
        audio would land in a queue nobody reads. In production that is the end
        of every meeting: the ingest worker thread delivers the last frames and
        POST /stop arrives right behind them, so the transcript cuts off before
        the last thing anyone said.

        The guarantee is precisely "audio whose send() has returned". A call
        that has passed the _closing check but not yet reached
        call_soon_threadsafe can still lose its frame; that residue is at most
        one in-flight feed(), because MeetingSession._stopping stops accepting
        frames the moment /stop begins.

        Returns True once the sentinel is queued, False if enqueuing failed.
        """
        enqueued = self._loop.create_future()

        def _enqueue_sentinel() -> None:
            ok = True
            try:
                self._queue.put_nowait(None)
            except Exception:  # noqa: BLE001 -- reported via the future below
                ok = False
            finally:
                # Always resolve. Otherwise aclose() waits forever and takes the
                # whole meeting finalize with it -- MeetingSession.stop() has no
                # timeout of its own.
                if not enqueued.done():
                    enqueued.set_result(ok)

        self._loop.call_soon_threadsafe(_enqueue_sentinel)
        return await enqueued

    async def aclose(self) -> list[dict]:
        """Flush, end the AWS stream, wait for its final results, return all
        words. Closing is what turns AWS's trailing partial results into finals,
        so this can yield more than the last ``words()`` did."""
        self._closing = True
        await self._await_task()
        if self._queue is not None and self._loop is not None:
            if not await self._signal_end_of_audio():
                # The pump can never be told to stop, so waiting on it below
                # would hang. Tear it down instead and keep what was finalized.
                _logger.warning("could not signal end-of-audio; aborting the stream")
                self.abort()

        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                # Only swallow when it is the PUMP that was cancelled (abort()
                # raced this close). If aclose() itself is being cancelled --
                # e.g. a caller wrapped it in wait_for -- re-raise: swallowing
                # makes a timed-out close look like a successful one.
                if not (self._aborted and self._task.cancelled()):
                    raise
            except Exception as exc:  # noqa: BLE001 -- finalize must not raise
                self.incomplete = True
                _logger.warning("transcription stream ended with an error: %s", exc)
        return list(self._words)

    def abort(self) -> None:
        """Synchronous teardown for an abrupt disconnect (``discard()``). Cancels
        the pump so the AWS stream is released instead of lingering until its
        idle timeout. Fire-and-forget: no results are collected."""
        self._aborted = True
        self.incomplete = True
        self._closing = True
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _pump(self) -> None:
        """Own the AWS session(s) for this speaker's whole lifetime."""
        pending: list[bytes] = []
        barren_sessions = 0
        failures = 0
        while True:
            # Wait for audio ONLY when there is none in hand. When AWS ends a
            # session it can hand back a chunk it never got to send (see
            # ``_run_session``); reopening immediately for it is what keeps that
            # audio from being dropped if the close sentinel is what arrives
            # next. Blocking on the queue here instead would lose it.
            if not pending:
                chunk = await self._queue.get()
                if chunk is None:
                    break
                pending.append(chunk)

            delivered_before = self._delivered_bytes
            try:
                if await self._run_session(pending):
                    break  # sentinel consumed inside the session: we're done
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- a dead stream must not kill the meeting
                pending.clear()

                # A session that ACCEPTED audio and then raised is not a
                # failure: that is how AWS ends an idle session. Transcribe
                # surfaces "no new audio was received for 15 seconds" by raising
                # out of the output stream rather than closing it, and because
                # this design only ever sends speech (silence is never
                # buffered), every pause longer than that ends a session exactly
                # this way. Counting them meant a participant who paused three
                # times was dropped for the REST of the meeting -- their opening
                # remarks transcribed and nothing after.
                if self._delivered_bytes > delivered_before:
                    _logger.debug("Transcribe session ended after delivering audio: %s", exc)
                    failures = 0
                    continue

                # Raised WITHOUT taking any audio -- genuinely broken (connect
                # refused, throttled, credentials). Those do count.
                failures += 1
                if failures >= _MAX_SESSION_FAILURES:
                    self.incomplete = True
                    _logger.warning(
                        "transcription failed %s times in a row (%s); giving up on this "
                        "speaker -- words already finalized are kept",
                        failures,
                        exc,
                    )
                    # Keep draining so send() never backs up.
                    await self._drain_remaining()
                    return
                _logger.warning("transcription session failed, retrying: %s", exc)
                continue

            # Guard against a hot reopen loop: if AWS keeps ending sessions
            # before we manage to deliver anything, stop rather than spinning up
            # sessions as fast as the network allows.
            if self._delivered_bytes == delivered_before:
                barren_sessions += 1
                if barren_sessions >= _MAX_BARREN_SESSIONS:
                    self.incomplete = True
                    _logger.warning(
                        "%s consecutive Transcribe sessions ended without accepting audio; "
                        "giving up on this speaker",
                        barren_sessions,
                    )
                    pending.clear()
                    await self._drain_remaining()
                    return
            else:
                barren_sessions = 0
                failures = 0

    async def _drain_remaining(self) -> None:
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return

    async def _run_session(self, pending: list[bytes]) -> bool:
        """Run ONE AWS streaming session until it ends, feeding it queued audio.

        Returns True if the close sentinel was consumed (nothing more is
        coming), False if AWS ended the session on its own -- in which case the
        caller reopens when the speaker next talks.
        """
        client = self._resolve_client()
        # Offset = audio already DELIVERED to earlier AWS sessions. Not
        # ``_sent_bytes``, which also counts audio still sitting in the queue
        # and about to go through THIS session at its own relative time zero.
        sent_bytes_at_stream_start = self._delivered_bytes
        stream = await asyncio.wait_for(
            client.start_stream_transcription(
                language_code=LANGUAGE_CODE,
                media_sample_rate_hz=self._sample_rate,
                media_encoding=MEDIA_ENCODING,
            ),
            SESSION_CONNECT_TIMEOUT_S,
        )
        offset_ms = sent_bytes_at_stream_start // self._bytes_per_ms

        async def consume() -> None:
            async for event in stream.output_stream:
                transcript = getattr(event, "transcript", None)
                if transcript is None:
                    continue
                for result in transcript.results or []:
                    # Partials get revised; only finals may reach the transcript.
                    if result.is_partial:
                        continue
                    words = _words_from_result(result, offset_ms)
                    self._words.extend(words)
                    if words and self._on_final is not None:
                        try:
                            self._on_final(words)
                        except Exception:  # noqa: BLE001 -- subtitles are optional
                            _logger.warning("subtitle final-result observer failed")

        consume_task = asyncio.ensure_future(consume())
        ended = False
        try:
            for chunk in pending:
                await stream.input_stream.send_audio_event(audio_chunk=chunk)
                self._delivered_bytes += len(chunk)
            pending.clear()

            while True:
                get_next = asyncio.ensure_future(self._queue.get())
                await asyncio.wait({get_next, consume_task}, return_when=asyncio.FIRST_COMPLETED)

                # Check the AWS side FIRST. asyncio.wait can return with BOTH
                # ready, and if the queue wins that tie we would send audio into
                # a session AWS has already closed -- which raises, and used to
                # take the speaker down for the rest of the meeting. Anything
                # pulled off the queue here is carried into the NEXT session
                # instead.
                if consume_task.done():
                    if get_next.done() and not get_next.cancelled():
                        chunk = get_next.result()
                        if chunk is None:
                            await consume_task
                            return True  # sentinel: nothing more is coming
                        pending.append(chunk)
                    else:
                        get_next.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await get_next
                    await consume_task
                    return False

                chunk = get_next.result()
                if chunk is None:  # sentinel from aclose()
                    await stream.input_stream.end_stream()
                    ended = True
                    # AWS flushes its last results and closes the output stream
                    # in response to end_stream(). Bound the wait anyway: a
                    # service that never closes would otherwise hang stop() --
                    # and with it the whole meeting finalize -- indefinitely.
                    # Words already finalized are kept.
                    try:
                        await asyncio.wait_for(consume_task, FINAL_FLUSH_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        self.incomplete = True
                        _logger.warning(
                            "timed out waiting %ss for final Transcribe results",
                            FINAL_FLUSH_TIMEOUT_S,
                        )
                    return True
                await stream.input_stream.send_audio_event(audio_chunk=chunk)
                self._delivered_bytes += len(chunk)
                continue
        finally:
            if not consume_task.done():
                consume_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await consume_task
            if not ended:
                with contextlib.suppress(Exception):
                    await stream.input_stream.end_stream()


def create_transcription_stream(region: str, client=None, sample_rate: int = 16000):
    """Build one speaker's persistent transcription stream. ``client`` is
    injectable (tests use a fake, no network)."""
    return _StreamingTranscription(region, client=client, sample_rate=sample_rate)
