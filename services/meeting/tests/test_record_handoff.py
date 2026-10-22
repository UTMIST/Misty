import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from src.contracts import RecordContext, RecordResult
from src.pipeline.record_handoff import HttpRecordHandoff
from src.sessions import SessionRegistry
from tests.test_sessions import FakeTranscriptionStream, _make_deps, _pcm


@pytest.mark.asyncio
async def test_handoff_before_cleanup_and_concurrent_stop_only_finalizes_once():
    stream = FakeTranscriptionStream([{"text": "hello", "start_ms": 1000}])
    deps = _make_deps([stream])
    entered, release = asyncio.Event(), asyncio.Event()
    captured = []

    class Handoff:
        async def submit(self, meeting_id, payload):
            captured.append(payload)
            entered.set()
            await release.wait()
            assert registry.get(meeting_id) is session
            return RecordResult(status="pending")

    deps["record_handoff"] = Handoff()
    registry = SessionRegistry(deps)
    meeting_id = str(uuid4())
    session = registry.create(meeting_id, "1")
    session.feed("4", "Angela", _pcm(20), ts_ms=0)
    session.mark_audio_complete()
    context = RecordContext(
        guild_id="1",
        text_channel_id="2",
        voice_channel_id="3",
        requester_discord_id="4",
        started_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
        participants=[{"discord_id": "4", "display_name": "Angela"}],
    )
    first = asyncio.create_task(session.stop("Title", context))
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(session.stop("Title", context))
    release.set()
    result = await second
    assert result.record.status == "pending"
    assert result.transcript in captured[0]["transcript"]
    assert captured[0]["title"] == "Title"
    assert captured[0]["decisions"] == result.minutes.decisions
    assert len(captured) == 1 and registry.get(meeting_id) is None
    assert "audio" not in str(captured[0].keys())


@pytest.mark.asyncio
async def test_network_failure_returns_pdf_status_and_retries_exact_payload():
    calls = []
    clock = [0]

    async def sleep(delay):
        clock[0] += delay
        await asyncio.sleep(0)

    def handle(request):
        calls.append(request.content)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(202, json={"persistence_status": "pending", "audience_team_ids": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        handoff = HttpRecordHandoff(
            "http://catalog", "key", 30, client=client, sleep=sleep, monotonic=lambda: clock[0]
        )
        result = await handoff.submit("id", {"transcript": "private"})
        assert result.status == "handoff_pending"
        await asyncio.gather(*handoff.tasks)
        assert len(calls) == 2 and calls[0] == calls[1]
        assert not handoff.pending


@pytest.mark.asyncio
async def test_hold_expiry_is_explicit_and_content_never_logged(caplog):
    clock = [0]

    async def sleep(delay):
        clock[0] += delay

    handoff = HttpRecordHandoff("", "", 20, sleep=sleep, monotonic=lambda: clock[0])
    result = await handoff.submit("safe-id", {"transcript": "SECRET WORDS"})
    assert result.status == "handoff_pending"
    await asyncio.gather(*handoff.tasks)
    assert "meeting.record_handoff_failed" in caplog.text and "SECRET WORDS" not in caplog.text
    assert not handoff.pending


@pytest.mark.asyncio
async def test_invalid_start_time_fails_before_paid_work():
    from datetime import timedelta

    deps = _make_deps([])
    deps["report_builder"] = lambda *args: pytest.fail("report must not run")
    registry = SessionRegistry(deps)
    session = registry.create(str(uuid4()), "1")
    context = RecordContext(
        guild_id="1",
        text_channel_id="2",
        voice_channel_id="3",
        requester_discord_id="4",
        started_at=deps["now"]() + timedelta(hours=1),
    )
    with pytest.raises(ValueError):
        await session.stop(record=context)
    assert not session._stop_requested
