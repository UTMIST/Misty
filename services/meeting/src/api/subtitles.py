"""Per-connection subtitle FIFO. Only this task writes application events.

Sequence allocation and insertion happen together on the owning event loop,
without an await. This orders delivery, not speech across independent speakers.
The queue is ephemeral: no replay, acknowledgement, or audio backpressure.
"""

import asyncio
import contextlib
import json
import logging

_logger = logging.getLogger("meeting.audit")


class SubtitleSender:
    def __init__(self, websocket, session_id, max_events=256, max_bytes=256 * 1024, timeout_s=5):
        self._socket = websocket
        self._session_id = session_id
        self._queue = asyncio.Queue(maxsize=max_events)
        self._max_bytes = max_bytes
        self._bytes = 0
        self._sequence = 0
        self._timeout_s = timeout_s
        self._disabled = False
        self._closed = False
        self._task = None

    def start(self):
        self._task = asyncio.create_task(self._send())

    def enqueue(self, event):
        """Loop-thread only. Never block a final-result or audio producer."""
        if self._disabled or self._closed:
            return
        event = {**event, "session_id": self._session_id}
        if event["type"] == "subtitles.chunk":
            self._sequence += 1
            event["sequence"] = self._sequence
        elif event["type"] == "subtitles.complete":
            event["last_sequence"] = self._sequence
        size = len(json.dumps(event).encode("utf-8"))
        if self._queue.full() or self._bytes + size > self._max_bytes:
            self._disable("queue_overflow")
            return
        self._bytes += size
        self._queue.put_nowait((event, size))

    def _clear(self):
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
        self._bytes = 0

    def _disable(self, code):
        if self._disabled:
            return
        self._disabled = True
        self._clear()
        _logger.warning("session %s: subtitles disabled (%s)", self._session_id, code)
        # One best-effort, content-free error; never grow an error queue.
        self._queue.put_nowait(
            ({"type": "subtitles.error", "session_id": self._session_id, "code": code}, 0)
        )

    async def _send(self):
        while True:
            event, size = await self._queue.get()
            self._bytes -= size
            try:
                await asyncio.wait_for(self._socket.send_json(event), self._timeout_s)
            except Exception:  # noqa: BLE001 -- outgoing failures don't own audio ingest
                if not self._disabled:
                    self._disable("send_failed")
            finally:
                self._queue.task_done()

    async def close(self):
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._clear()
