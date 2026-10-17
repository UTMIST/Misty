"""Dedicated Q&A capacity and shutdown, using blocking fakes only."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.pipeline.qna_runner import QnaBusy, QnaRunner, QnaRunnerClosed, default_worker_count


async def wait_for_event(event):
    async def wait():
        while not event.is_set():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), 2)


def test_qna_pool_is_half_the_shared_default_and_strictly_smaller():
    with ThreadPoolExecutor() as shared_default:
        assert default_worker_count() == shared_default._max_workers
    runner = QnaRunner(lambda: None)
    try:
        assert runner.max_workers == max(1, default_worker_count() // 2)
        assert 0 < runner.max_workers < default_worker_count()
    finally:
        runner.close()


def test_busy_is_immediate_and_capacity_recovers_after_success_and_failure():
    async def scenario():
        entered, release = threading.Event(), threading.Event()

        def builder(value):
            entered.set()
            assert release.wait(3)
            if value == "fail":
                raise ValueError("fake failure")
            return value

        runner = QnaRunner(builder, max_workers=1)
        pending = asyncio.create_task(runner.run("first"))
        try:
            await wait_for_event(entered)
            with pytest.raises(QnaBusy):
                await asyncio.wait_for(runner.run("extra"), 0.1)
            release.set()
            assert await pending == "first"
            with pytest.raises(ValueError, match="fake failure"):
                await runner.run("fail")
            assert await runner.run("next") == "next"
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            runner.close()

    asyncio.run(scenario())


def test_cancellation_holds_capacity_until_the_real_worker_finishes_without_leaking_errors():
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        unhandled = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda loop, context: unhandled.append(context))

        def builder(value):
            if value == "cancelled":
                entered.set()
                assert release.wait(3)
                raise ValueError("PRIVATE MODEL CONTENT")
            return value

        runner = QnaRunner(builder, max_workers=1)
        pending = asyncio.create_task(runner.run("cancelled"))
        try:
            await wait_for_event(entered)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            with pytest.raises(QnaBusy):
                await runner.run("too early")
            release.set()

            async def retry():
                while True:
                    try:
                        return await runner.run("after completion")
                    except QnaBusy:
                        await asyncio.sleep(0.001)

            assert await asyncio.wait_for(retry(), 2) == "after completion"
            await asyncio.sleep(0)
            assert unhandled == []
        finally:
            release.set()
            runner.close()

    asyncio.run(scenario())


def test_close_is_idempotent_and_rejects_new_jobs():
    runner = QnaRunner(lambda: "unused", max_workers=1)
    runner.close()
    runner.close()
    with pytest.raises(QnaRunnerClosed):
        asyncio.run(runner.run())


def test_close_does_not_block_the_event_loop_on_running_jobs():
    async def scenario():
        entered, release = threading.Event(), threading.Event()

        def builder():
            entered.set()
            assert release.wait(3)
            return "finished"

        runner = QnaRunner(builder, max_workers=1)
        pending = asyncio.create_task(runner.run())
        try:
            await wait_for_event(entered)
            runner.close()
            assert not release.is_set()
            with pytest.raises(QnaRunnerClosed):
                await runner.run()
        finally:
            release.set()
        assert await pending == "finished"

    asyncio.run(scenario())


def test_app_lifespan_owns_one_pool_and_clears_cached_wiring(monkeypatch):
    from fastapi.testclient import TestClient
    from src.api import wiring
    from src.api.app import create_app

    created = []

    class FakeRunner:
        max_workers = 2

        def __init__(self, builder):
            self.closed = False
            created.append(self)

        async def run(self, *args):
            return None

        def close(self):
            self.closed = True

    monkeypatch.setattr(wiring, "QnaRunner", FakeRunner)
    monkeypatch.setattr(wiring, "_build_qna_builder", lambda: lambda *args: None)
    for _ in range(2):
        with TestClient(create_app()) as client:
            assert client.get("/health").status_code == 200
            runner = wiring.get_qna_runner()
            registry = wiring.get_session_registry()
            assert registry._deps["qna_runner"].__self__ is runner
            assert wiring.get_qna_runner() is runner
            assert not runner.closed
        assert runner.closed
        assert wiring.get_qna_runner.cache_info().currsize == 0
        assert wiring.get_session_registry.cache_info().currsize == 0
    assert len(created) == 2
