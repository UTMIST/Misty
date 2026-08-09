"""Tests for the AWS Transcribe streaming wrapper, using a fake client (no network)."""

import asyncio

from amazon_transcribe.model import Alternative, Item, Result, Transcript, TranscriptEvent

from src.stt.transcribe import create_transcription_stream


class _FakeInputStream:
    """Mimics amazon_transcribe's AudioStream (input_stream)."""

    def __init__(self, output_stream=None):
        self.sent_chunks = []
        self.ended = False
        self._output_stream = output_stream

    async def send_audio_event(self, audio_chunk):
        # The real service rejects audio once it has closed the session; a fake
        # that silently accepts it would hide sends into a dead stream.
        if self._output_stream is not None and self._output_stream.closed:
            raise RuntimeError("send_audio_event on a closed stream")
        self.sent_chunks.append(audio_chunk)

    async def end_stream(self):
        self.ended = True


def _final_event() -> TranscriptEvent:
    alternative = Alternative(
        transcript="hello world",
        items=[
            Item(start_time=0.1, end_time=0.4, item_type="pronunciation", content="hello"),
            Item(start_time=0.4, end_time=0.42, item_type="punctuation", content=","),
            Item(start_time=0.512, end_time=0.9, item_type="pronunciation", content="world"),
        ],
        entities=None,
    )
    result = Result(
        result_id="r1", start_time=0.1, end_time=0.9, is_partial=False, alternatives=[alternative]
    )
    return TranscriptEvent(transcript=Transcript(results=[result]))


def _partial_event() -> TranscriptEvent:
    alternative = Alternative(
        transcript="hel",
        items=[Item(start_time=0.1, end_time=0.3, item_type="pronunciation", content="hel")],
        entities=None,
    )
    result = Result(
        result_id="r1", start_time=0.1, end_time=0.3, is_partial=True, alternatives=[alternative]
    )
    return TranscriptEvent(transcript=Transcript(results=[result]))


# --- persistent per-speaker streaming ---------------------------------------


async def drain(stream, max_rounds: int = 2000):
    """Test-only synchronisation: yield until every byte handed to ``send()``
    has reached the fake AWS client and any emitted events have been consumed.

    This lives in the test module, not on the production class, so the runtime
    contract stays ``start/send/words/aclose/abort``. It pokes private state on
    purpose -- that is a test's prerogative, not production API surface. The
    round cap keeps a wedged pump from hanging the suite.
    """
    for _ in range(max_rounds):
        task = stream._task
        if task is not None and task.done():
            return
        caught_up = (
            stream._queue is not None
            and stream._queue.empty()
            and (stream._delivered_bytes == stream._sent_bytes)
        )
        if caught_up:
            await asyncio.sleep(0)  # let the output consumer run
            if stream._queue.empty() and stream._delivered_bytes == stream._sent_bytes:
                return
        await asyncio.sleep(0)


class _LiveOutputStream:
    """An output stream fed events on demand, so a test can control WHEN results
    become visible -- mimicking AWS emitting finals as the speaker talks rather
    than all at once at the end."""

    def __init__(self):
        self._queue = asyncio.Queue()
        self.closed = False

    def push(self, event):
        self._queue.put_nowait(event)

    def finish(self):
        self._queue.put_nowait(None)

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        while True:
            event = await self._queue.get()
            if event is None:
                self.closed = True
                return
            yield event


class _LiveStream:
    def __init__(self):
        self.output_stream = _LiveOutputStream()
        self.input_stream = _FakeInputStream(self.output_stream)
        # Faithful to the real service: ending the input stream makes AWS flush
        # its remaining results and CLOSE the output stream. Without this the
        # fake would let a consumer wait on it forever, which the real one never
        # does.
        _end = self.input_stream.end_stream

        async def end_stream():
            await _end()
            self.output_stream.finish()

        self.input_stream.end_stream = end_stream


class _LiveClient:
    def __init__(self):
        self.streams = []
        self.calls = []

    async def start_stream_transcription(self, **kwargs):
        self.calls.append(kwargs)
        stream = _LiveStream()
        self.streams.append(stream)
        return stream


def test_streaming_sends_audio_once_and_exposes_words_as_they_finalize():
    """The persistent stream: audio pushed in with send() reaches AWS exactly
    once, and finalized words become readable via words() WITHOUT closing or
    re-sending anything."""

    async def scenario():
        client = _LiveClient()
        stream = create_transcription_stream(region="us-east-1", client=client)
        stream.start()

        stream.send(b"\x00" * 640)
        stream.send(b"\x01" * 640)
        await drain(stream)

        assert stream.words() == []  # nothing finalized yet

        client.streams[0].output_stream.push(_final_event())
        await drain(stream)

        # Readable mid-flight, with the stream still open.
        assert stream.words() == [
            {"text": "hello", "start_ms": 100},
            {"text": "world", "start_ms": 512},
        ]
        assert client.streams[0].input_stream.ended is False

        words = await stream.aclose()
        assert words == [
            {"text": "hello", "start_ms": 100},
            {"text": "world", "start_ms": 512},
        ]
        # Exactly one AWS stream, each chunk sent exactly once.
        assert len(client.streams) == 1
        assert client.streams[0].input_stream.sent_chunks == [b"\x00" * 640, b"\x01" * 640]
        assert client.streams[0].input_stream.ended is True

    asyncio.run(scenario())


def test_streaming_reopens_after_the_aws_stream_ends_and_offsets_word_times():
    """AWS ends a streaming session on its own (idle timeout, or the 4h cap). If
    the speaker talks again afterwards we must transparently open a NEW stream --
    and offset its word times by the audio already sent, because each stream
    reports times relative to its own start."""

    async def scenario():
        client = _LiveClient()
        stream = create_transcription_stream(region="us-east-1", client=client)
        stream.start()

        # 1000ms of audio through the first stream, which then ends by itself.
        stream.send(b"\x00" * (32 * 1000))
        await drain(stream)
        client.streams[0].output_stream.push(_final_event())
        client.streams[0].output_stream.finish()
        await drain(stream)

        # The speaker resumes: a second stream opens transparently.
        stream.send(b"\x02" * 640)
        await drain(stream)
        client.streams[1].output_stream.push(_final_event())
        words = await stream.aclose()

        assert len(client.streams) == 2, "a new stream must be opened after the first ends"
        # Second stream's words repeat start_ms 100/512, but land 1000ms later
        # because 1000ms of audio preceded them.
        assert words == [
            {"text": "hello", "start_ms": 100},
            {"text": "world", "start_ms": 512},
            {"text": "hello", "start_ms": 1100},
            {"text": "world", "start_ms": 1512},
        ]

    asyncio.run(scenario())


def test_streaming_abort_is_sync_and_stops_the_stream():
    """discard() (abrupt WS drop) is synchronous and must still tear the AWS
    stream down rather than leaking it until its idle timeout."""

    async def scenario():
        client = _LiveClient()
        stream = create_transcription_stream(region="us-east-1", client=client)
        stream.start()
        stream.send(b"\x00" * 640)
        await drain(stream)

        stream.abort()  # sync, no await

        # Cancellation still has to propagate through the pump's nested waits,
        # so give the loop turns to settle rather than assuming a fixed number.
        for _ in range(100):
            if not stream.is_running():
                break
            await asyncio.sleep(0)

        assert stream.is_running() is False
        # And it must stay torn down -- no reopen behind our back.
        assert stream.words() == []

    asyncio.run(scenario())


def test_streaming_survives_a_failing_stream_and_keeps_earlier_words():
    """A mid-meeting AWS/network failure must not crash the meeting: words
    already finalized are kept and the speaker's later audio is simply dropped
    rather than propagating an exception into the WS ingest loop."""

    class _ExplodingClient:
        def __init__(self):
            self.calls = 0

        async def start_stream_transcription(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                stream = _LiveStream()
                self.first = stream
                return stream
            raise RuntimeError("AWS is unhappy")

    async def scenario():
        client = _ExplodingClient()
        stream = create_transcription_stream(region="us-east-1", client=client)
        stream.start()
        stream.send(b"\x00" * 640)
        await drain(stream)
        client.first.output_stream.push(_final_event())
        await drain(stream)
        # First stream ends; reopening will raise.
        client.first.output_stream.finish()
        await drain(stream)
        stream.send(b"\x01" * 640)
        await drain(stream)

        words = await stream.aclose()
        assert words == [
            {"text": "hello", "start_ms": 100},
            {"text": "world", "start_ms": 512},
        ]

    asyncio.run(scenario())


def test_streaming_ignores_partial_results():
    """AWS emits partial hypotheses that get revised. Only finalized results may
    reach the transcript, or words would appear and then change under the
    reader."""

    async def scenario():
        client = _LiveClient()
        stream = create_transcription_stream(region="us-east-1", client=client)
        stream.start()
        stream.send(b"\x00" * 640)
        await drain(stream)

        client.streams[0].output_stream.push(_partial_event())
        await drain(stream)
        assert stream.words() == [], "a partial result must not be surfaced"

        client.streams[0].output_stream.push(_final_event())
        words = await stream.aclose()
        assert words == [
            {"text": "hello", "start_ms": 100},
            {"text": "world", "start_ms": 512},
        ]

    asyncio.run(scenario())


def test_pending_replay_audio_is_flushed_when_close_follows_a_restart():
    """When AWS ends a session, a chunk pulled off the queue in that same turn
    is carried into ``pending`` for the NEXT session. If the close sentinel is
    what arrives next, the pump must still flush that audio rather than exiting
    with it in hand.

    Driven entirely through the public API: `send` -> AWS ends -> `send` ->
    `aclose`, with no further audio in between."""

    async def scenario():
        client = _LiveClient()
        stream = create_transcription_stream(region="us-east-1", client=client)
        stream.start()
        stream.send(b"a" * 640)
        await drain(stream)

        # AWS ends the session in the same turn the next chunk lands, so that
        # chunk becomes pending replay audio...
        client.streams[0].output_stream.finish()
        stream.send(b"b" * 640)
        await drain(stream)

        # ...and the meeting stops before this speaker says anything more.
        await stream.aclose()

        delivered = [bytes(c) for st in client.streams for c in st.input_stream.sent_chunks]
        assert b"b" * 640 in delivered, (
            f"replay audio was dropped on close; delivered {[d[:1] for d in delivered]}"
        )

    asyncio.run(scenario())


def test_abort_before_the_pump_spawns_does_not_leak_a_task():
    """``start()`` schedules the pump via ``call_soon_threadsafe``, so there is a
    window where ``_task`` is still None. An ``abort()`` landing in that window
    must not let the pump spawn anyway and park forever on a queue nothing will
    ever feed -- that leaks a task on the event loop for the life of the
    process. Reachable on the abrupt-disconnect path."""

    async def scenario():
        stream = create_transcription_stream(region="us-east-1", client=object())
        stream.start()
        stream.abort()  # lands before _spawn() has run
        for _ in range(50):
            await asyncio.sleep(0)

        assert stream.is_running() is False, "pump spawned and leaked despite abort()"

    asyncio.run(scenario())


def test_session_ending_alongside_new_audio_restarts_instead_of_dying():
    """`asyncio.wait(FIRST_COMPLETED)` can return with BOTH the queue read and
    the output-stream consumer done. If the queue is checked first, the pump
    sends into a session AWS has already closed -- which raises, trips the
    failure handler, and leaves the speaker a black hole for the REST of the
    meeting (audio drained and discarded, one log line as the only signal).

    AWS ends sessions on idle timeout, at the 4h cap, and on transient service
    errors, so this is an ordinary occurrence, not an exotic race. The session
    must restart and keep transcribing."""

    async def scenario():
        client = _LiveClient()
        stream = create_transcription_stream(region="us-east-1", client=client)
        stream.start()
        stream.send(b"a" * 640)
        await drain(stream)

        # AWS ends the session in the same turn that more audio arrives.
        client.streams[0].output_stream.finish()
        stream.send(b"b" * 640)
        await drain(stream)

        # ...and the speaker keeps talking afterwards.
        stream.send(b"c" * 640)
        await drain(stream)

        assert len(client.streams) == 2, (
            f"expected a restart, got {len(client.streams)} session(s) -- "
            "the speaker stopped being transcribed"
        )
        later = [bytes(c) for c in client.streams[1].input_stream.sent_chunks]
        assert later == [b"b" * 640, b"c" * 640], (
            f"audio after the restart never reached AWS: {[c[:1] for c in later]}"
        )
        await stream.aclose()

    asyncio.run(scenario())


def test_a_transient_session_failure_does_not_disable_the_speaker():
    """One failed session must not be terminal. Previously any exception --
    including a single transient AWS error -- sent the pump into a drain-and-
    discard loop for the remainder of the meeting."""

    class _FlakyClient:
        def __init__(self):
            self.calls = 0
            self.streams = []

        async def start_stream_transcription(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient AWS blip")
            stream = _LiveStream()
            self.streams.append(stream)
            return stream

    async def scenario():
        client = _FlakyClient()
        stream = create_transcription_stream(region="us-east-1", client=client)
        stream.start()
        stream.send(b"a" * 640)
        await drain(stream)
        stream.send(b"b" * 640)
        await drain(stream)

        assert client.streams, "the pump gave up after one transient failure"
        delivered = [bytes(c) for c in client.streams[0].input_stream.sent_chunks]
        assert b"b" * 640 in delivered, "audio after the blip never reached AWS"
        await stream.aclose()

    asyncio.run(scenario())


class _FlushingStream:
    """A stream whose `end_stream()` behaves like the real service: it emits one
    LAST final result and only then closes the output stream.

    The default `_LiveStream` closes synchronously and emits nothing, which
    silently makes aclose()'s whole reason for existing untestable."""

    def __init__(self, final_event):
        self.output_stream = _LiveOutputStream()
        self.input_stream = _FakeInputStream(self.output_stream)
        _end = self.input_stream.end_stream

        async def end_stream():
            await _end()
            self.output_stream.push(final_event)
            self.output_stream.finish()

        self.input_stream.end_stream = end_stream


def test_aclose_surfaces_finals_emitted_only_after_end_stream():
    """Closing is what turns AWS's trailing partials into finals, so aclose()
    can legitimately return MORE than the last words() did. Without this the
    final utterance of every meeting would be missing from the transcript."""

    async def scenario():
        final = _final_event()

        class _C:
            def __init__(self):
                self.streams = []

            async def start_stream_transcription(self, **kwargs):
                st = _FlushingStream(final)
                self.streams.append(st)
                return st

        stream = create_transcription_stream(region="us-east-1", client=_C())
        stream.start()
        stream.send(b"\x00" * 640)
        await drain(stream)

        assert stream.words() == [], "nothing has been finalized yet"

        words = await stream.aclose()
        assert words == [
            {"text": "hello", "start_ms": 100},
            {"text": "world", "start_ms": 512},
        ], "the post-end_stream flush was lost"

    asyncio.run(scenario())


def test_aclose_gives_up_on_a_stream_that_never_closes():
    """A service that accepts end_stream() but never closes its output stream
    must not hang finalize -- stop() would block the whole meeting, and the bot
    would time out and lose the minutes. Words already finalized are kept."""

    class _NeverClosingStream:
        def __init__(self):
            self.output_stream = _LiveOutputStream()
            self.input_stream = _FakeInputStream(self.output_stream)
            # end_stream() succeeds but the output stream is never finished.

    class _C:
        def __init__(self):
            self.streams = []

        async def start_stream_transcription(self, **kwargs):
            st = _NeverClosingStream()
            self.streams.append(st)
            return st

    async def scenario():
        client = _C()
        stream = create_transcription_stream(region="us-east-1", client=client)
        stream.start()
        stream.send(b"\x00" * 640)
        await drain(stream)
        client.streams[0].output_stream.push(_final_event())
        await drain(stream)

        import src.stt.transcribe as mod

        original = mod.FINAL_FLUSH_TIMEOUT_S
        mod.FINAL_FLUSH_TIMEOUT_S = 0.05  # keep the test fast
        try:
            words = await asyncio.wait_for(stream.aclose(), 5)
        finally:
            mod.FINAL_FLUSH_TIMEOUT_S = original

        # Bounded, and the words finalized before the hang are preserved.
        assert words == [
            {"text": "hello", "start_ms": 100},
            {"text": "world", "start_ms": 512},
        ]

    asyncio.run(scenario())


def test_audio_sent_just_before_close_is_not_jumped_by_the_sentinel():
    """`send()` hands audio to the loop via `call_soon_threadsafe`, so the
    enqueue is DEFERRED. If `aclose()` enqueues its sentinel directly it
    overtakes that pending audio: the pump ends the stream, and the audio then
    lands in a queue nobody reads.

    In production this is the end of every meeting -- the WS ingest worker
    thread delivers the last frames and `POST /stop` arrives right behind them
    -- so the symptom is a transcript that cuts off before the last thing
    anyone said."""
    import threading

    async def scenario():
        client = _LiveClient()
        stream = create_transcription_stream(region="us-east-1", client=client)
        stream.start()
        stream.send(b"early" * 128)
        await drain(stream)

        # Final frames arrive from the ingest worker thread...
        handed_off = threading.Event()

        def worker():
            stream.send(b"LAST1" * 128)
            stream.send(b"LAST2" * 128)
            handed_off.set()

        thread = threading.Thread(target=worker)
        thread.start()
        handed_off.wait(5)
        thread.join(5)

        # ...and /stop lands immediately behind them.
        await stream.aclose()

        delivered = [bytes(c)[:5] for st in client.streams for c in st.input_stream.sent_chunks]
        assert b"LAST1" in delivered and b"LAST2" in delivered, (
            f"the end of the meeting was dropped; AWS only got {delivered}"
        )

    asyncio.run(scenario())


def test_aclose_returns_even_if_the_sentinel_enqueue_raises():
    """`aclose()` waits on a future that the sentinel callback resolves. If that
    callback raises, the loop swallows the exception and the future is never
    resolved -- so `/stop` waits forever, wedging the whole meeting finalize
    (it runs inside `MeetingSession.stop`'s gather, which has no timeout of its
    own). Resolving in a `finally` keeps a broken enqueue recoverable."""

    async def scenario():
        client = _LiveClient()
        stream = create_transcription_stream(region="us-east-1", client=client)
        stream.start()
        stream.send(b"\x00" * 640)
        await drain(stream)

        class _ExplodingQueue:
            def put_nowait(self, item):
                raise RuntimeError("queue is broken")

            def empty(self):
                return True

        stream._queue = _ExplodingQueue()

        # Must return rather than hang -- AND actually keep what was finalized
        # before the break, which is the claim that matters to a reader.
        client.streams[0].output_stream.push(_final_event())
        await asyncio.sleep(0)
        words = await asyncio.wait_for(stream.aclose(), 5)
        assert words == [
            {"text": "hello", "start_ms": 100},
            {"text": "world", "start_ms": 512},
        ]

    asyncio.run(scenario())


class _RaisingOutputStream(_LiveOutputStream):
    """An output stream that can RAISE mid-iteration, which is how the real
    service ends a session.

    `amazon-transcribe` surfaces `BadRequestException: no new audio was received
    for 15 seconds` by raising out of the `async for`, not by closing the
    iterator. `_LiveOutputStream` can only finish cleanly, so it cannot express
    the single most common way an AWS session ends -- which is why the drop this
    file now guards against went unnoticed.
    """

    def raise_next(self, exc):
        self._queue.put_nowait(exc)

    async def _iter(self):
        while True:
            event = await self._queue.get()
            if event is None:
                self.closed = True
                return
            if isinstance(event, Exception):
                raise event
            yield event


class _RaisingStream:
    def __init__(self):
        self.output_stream = _RaisingOutputStream()
        self.input_stream = _FakeInputStream(self.output_stream)


class _IdlingClient:
    def __init__(self):
        self.streams = []

    async def start_stream_transcription(self, **kwargs):
        stream = _RaisingStream()
        self.streams.append(stream)
        return stream


def test_repeated_aws_idle_timeouts_do_not_disable_a_speaker():
    """Every pause longer than ~15s ends that speaker's AWS session with a
    raised BadRequestException, because this design only ever sends speech --
    silence is never buffered. So an idle timeout is not an error, it is the
    normal shape of a conversation.

    Counting each one toward the give-up threshold meant a participant who
    paused three times was silently dropped for the REST of the meeting: their
    opening remarks appeared and nothing else ever did."""

    async def scenario():
        client = _IdlingClient()
        stream = create_transcription_stream(region="us-east-1", client=client)
        stream.start()

        for i in range(6):
            stream.send(b"\x00" * 640)
            await drain(stream)
            current = client.streams[-1]
            current.output_stream.push(_final_event())
            await drain(stream)
            current.output_stream.raise_next(
                RuntimeError("no new audio was received for 15 seconds")
            )
            await drain(stream)

        words = await stream.aclose()
        # Two pronunciation items per utterance, six utterances.
        assert len(words) == 12, (
            f"speaker dropped after repeated idle timeouts: {len(words)} words "
            f"from {len(client.streams)} sessions"
        )

    asyncio.run(scenario())


def test_a_session_that_never_delivers_still_counts_as_a_failure():
    """The give-up path must survive: a stream that fails WITHOUT ever accepting
    audio is genuinely broken, and retrying it forever would spin."""

    class _AlwaysFailing:
        def __init__(self):
            self.calls = 0

        async def start_stream_transcription(self, **kwargs):
            self.calls += 1
            raise RuntimeError("AWS is down")

    async def scenario():
        client = _AlwaysFailing()
        stream = create_transcription_stream(region="us-east-1", client=client)
        stream.start()
        for _ in range(6):
            stream.send(b"\x00" * 640)
            await drain(stream)
        await stream.aclose()

        assert client.calls <= 4, f"retried {client.calls} times; the give-up bound is gone"

    asyncio.run(scenario())


def test_a_hanging_connect_does_not_wedge_finalize():
    """`start_stream_transcription` has no timeout of its own. If AWS accepts
    the socket and never answers, the pump blocks forever, `aclose()`'s
    `await self._task` never returns, and `MeetingSession.stop()` -- which has
    no timeout either -- hangs with it, leaving the session registered. The only
    thing that eventually unwedges it is the bot's HTTP client giving up."""

    class _HangingClient:
        async def start_stream_transcription(self, **kwargs):
            await asyncio.sleep(3600)

    async def scenario():
        stream = create_transcription_stream(region="us-east-1", client=_HangingClient())
        stream.start()
        stream.send(b"\x00" * 640)
        await asyncio.sleep(0)

        import src.stt.transcribe as mod

        original = mod.SESSION_CONNECT_TIMEOUT_S
        mod.SESSION_CONNECT_TIMEOUT_S = 0.05
        try:
            words = await asyncio.wait_for(stream.aclose(), 5)
        finally:
            mod.SESSION_CONNECT_TIMEOUT_S = original
        assert words == []

    asyncio.run(scenario())
