"""Bounded Q&A workers isolated from asyncio's audio/report executor."""

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor


class QnaBusy(Exception): ...


class QnaRunnerClosed(Exception): ...


def default_worker_count() -> int:
    # Match Python 3.11/3.12's default; 3.13+ uses process_cpu_count instead.
    cpu_count = getattr(os, "process_cpu_count", os.cpu_count)
    return min(32, (cpu_count() or 1) + 4)


def _observe_result(future: asyncio.Future) -> None:
    # A disconnected/cancelled caller no longer awaits this result. Retrieve
    # exceptions so asyncio cannot log private model/provider content later.
    if not future.cancelled():
        future.exception()


class QnaRunner:
    def __init__(self, builder, *, max_workers: int | None = None):
        # Keep Q&A concurrency below the shared audio/report executor capacity.
        self.max_workers = (
            max(1, default_worker_count() // 4) if max_workers is None else max_workers
        )
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers, thread_name_prefix="meeting-qna"
        )
        self._slots = threading.BoundedSemaphore(self.max_workers)
        self._builder = builder
        self._closed = False

    async def run(self, *args):
        if self._closed:
            raise QnaRunnerClosed
        if not self._slots.acquire(blocking=False):
            raise QnaBusy
        try:
            job = self._executor.submit(self._builder, *args)
        except Exception:
            self._slots.release()
            raise
        # Release on completion of the actual concurrent future, not cancellation
        # of its asyncio waiter. There is no unbounded queue of Q&A snapshots.
        job.add_done_callback(lambda _: self._slots.release())
        future = asyncio.wrap_future(job)
        future.add_done_callback(_observe_result)
        return await asyncio.shield(future)

    def close(self) -> None:
        self._closed = True
        # Running calls finish under the existing HTTP timeout. Shutdown itself
        # must not block the event loop; pending jobs are cancelled safely.
        self._executor.shutdown(wait=False, cancel_futures=True)
