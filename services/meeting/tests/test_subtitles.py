"""Subtitle lifecycle and ordered transport; no cloud or network providers."""

import asyncio

from src.sessions import SessionRegistry
from tests.test_sessions import FakeTranscriptionStream, _make_deps


async def drain_sender(sender, timeout_s=0.5):
    """Test synchronization; production sends continuously until socket close."""
    await asyncio.wait_for(sender._queue.join(), timeout_s)


class FinalStream(FakeTranscriptionStream):
    def set_final_callback(self, callback):
        self.callback = callback

    def emit(self, words):
        self._words.extend(words)
        self.callback(words)

    async def aclose(self):
        self.emit([{"text": "tail", "start_ms": 40}])
        return await super().aclose()


def test_final_chunks_map_anchors_use_updated_names_and_flush_before_complete():
    async def scenario():
        stream = FinalStream()
        session = SessionRegistry(_make_deps([stream])).create("s1", "g1")
        events = []
        session.set_subtitle_sink(events.append)
        session.feed("u1", "u1", b"\x00" * 640, 12_000)
        session.update_speaker_name("u1", "Alice")
        stream.emit([{"text": "hello", "start_ms": 0}])
        stream.emit([{"text": "world", "start_ms": 20}])
        session.mark_audio_complete()
        report = await session.stop()
        assert [(e["text"], e["start_ms"]) for e in events[:-1]] == [
            ("hello", 12_000),
            ("world", 12_020),
            ("tail", 12_040),
        ]
        assert all(e["display_name"] == "Alice" for e in events[:-1])
        assert all(e["speaker_id"] == "u1" for e in events[:-1])
        assert events[-1] == {"type": "subtitles.complete", "status": "complete"}
        assert "hello world tail" in report.transcript

    asyncio.run(scenario())


def test_subtitle_failure_and_report_failure_do_not_drop_finalized_words():
    async def scenario():
        stream = FinalStream()
        deps = _make_deps([stream])

        def broken_report(*_):
            raise RuntimeError("report unavailable")

        deps["report_builder"] = broken_report
        session = SessionRegistry(deps).create("s1", "g1")
        events = []

        def sink(event):
            events.append(event)
            if event["type"] == "subtitles.chunk":
                raise RuntimeError("subtitle failure")

        session.set_subtitle_sink(sink)
        session.feed("u1", "Alice", b"\x00" * 640, 0)
        stream.emit([{"text": "hello", "start_ms": 0}])
        assert (await session.transcript_view())[0].text == "hello"
        session.mark_audio_complete()
        try:
            await session.stop()
        except RuntimeError:
            pass
        assert events[-1]["type"] == "subtitles.complete"
        assert stream.aclosed

    asyncio.run(scenario())


def test_sender_serializes_concurrent_producers_and_completion_behind_tail():
    async def scenario():
        from src.api.subtitles import SubtitleSender

        sent = []
        gate = asyncio.Event()

        class Socket:
            async def send_json(self, event):
                if event["type"] == "subtitles.chunk" and event["sequence"] == 1:
                    await gate.wait()
                sent.append(event)

        sender = SubtitleSender(Socket(), "s1")
        sender.start()
        sender.enqueue({"type": "session.ready", "subtitle_events": True})

        async def producer(text):
            sender.enqueue({"type": "subtitles.chunk", "text": text})

        await asyncio.gather(producer("later speech"), producer("earlier speech"))
        sender.enqueue({"type": "subtitles.complete", "status": "complete"})
        await asyncio.sleep(0)
        assert not any(e["type"] == "subtitles.complete" for e in sent)
        gate.set()
        await drain_sender(sender)
        assert [e["type"] for e in sent] == [
            "session.ready",
            "subtitles.chunk",
            "subtitles.chunk",
            "subtitles.complete",
        ]
        assert [e["sequence"] for e in sent[1:3]] == [1, 2]
        assert sent[-1]["last_sequence"] == 2
        assert all(e["session_id"] == "s1" for e in sent)
        await sender.close()

    asyncio.run(scenario())


def test_sender_overflow_disables_subtitles_without_raising_to_audio_producer():
    async def scenario():
        from src.api.subtitles import SubtitleSender

        sent = []

        class Socket:
            async def send_json(self, event):
                sent.append(event)

        sender = SubtitleSender(Socket(), "s1", max_events=1)
        sender.enqueue({"type": "subtitles.chunk", "text": "first"})
        sender.enqueue({"type": "subtitles.chunk", "text": "overflow"})
        sender.enqueue({"type": "subtitles.chunk", "text": "ignored"})
        sender.start()
        await drain_sender(sender)
        assert sent == [{"type": "subtitles.error", "session_id": "s1", "code": "queue_overflow"}]
        await sender.close()

    asyncio.run(scenario())


def test_sender_timeout_stops_events_without_blocking_finalization():
    async def scenario():
        from src.api.subtitles import SubtitleSender

        attempts = []

        class Socket:
            async def send_json(self, event):
                attempts.append(event["type"])
                await asyncio.Event().wait()

        sender = SubtitleSender(Socket(), "s1", timeout_s=0.01)
        sender.start()
        sender.enqueue({"type": "subtitles.chunk", "text": "first"})
        sender.enqueue({"type": "subtitles.complete", "status": "complete"})
        await drain_sender(sender)
        sender.enqueue({"type": "subtitles.chunk", "text": "ignored"})
        await drain_sender(sender)
        assert attempts == ["subtitles.chunk", "subtitles.error"]
        await sender.close()

    asyncio.run(scenario())


def test_completion_reports_a_speaker_that_could_not_flush():
    async def scenario():
        stream = FinalStream()
        stream.incomplete = True
        session = SessionRegistry(_make_deps([stream])).create("s1", "g1")
        events = []
        session.set_subtitle_sink(events.append)
        session.feed("u1", "Alice", b"\x00" * 640, 0)
        session.mark_audio_complete()
        await session.stop()
        assert events[-1] == {"type": "subtitles.complete", "status": "incomplete"}

    asyncio.run(scenario())
