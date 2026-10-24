"""Bounded in-memory recovery until documentation-system acknowledges durability."""

import asyncio
import logging
import time

import httpx

from src.contracts import RecordResult

logger = logging.getLogger("meeting.audit")


class HttpRecordHandoff:
    def __init__(
        self,
        base_url,
        api_key,
        hold_s,
        *,
        client=None,
        sleep=asyncio.sleep,
        monotonic=time.monotonic,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.hold_s = hold_s
        self.client = client
        self.sleep = sleep
        self.monotonic = monotonic
        self.pending = {}
        self.tasks = set()

    async def _put(self, meeting_id, payload):
        if not self.base_url or not self.api_key:
            return None

        async def send(client):
            try:
                response = await client.put(
                    f"{self.base_url}/meeting-records/{meeting_id}",
                    headers={"X-API-Key": self.api_key},
                    json=payload,
                )
                if response.status_code not in {200, 202}:
                    return None
                body = response.json()
                return RecordResult(
                    status=body["persistence_status"],
                    doc_url=body.get("doc_url"),
                    admin_only=not body.get("audience_team_ids"),
                )
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                return None

        if self.client is not None:
            return await send(self.client)
        async with httpx.AsyncClient(timeout=10) as client:
            return await send(client)

    async def submit(self, meeting_id, payload):
        # Keep a reference before the first await, including on cancellation.
        if meeting_id in self.pending:
            return RecordResult(status="handoff_pending")
        self.pending[meeting_id] = payload
        task = asyncio.create_task(self._deliver(meeting_id, payload))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return await asyncio.shield(task)

    async def _deliver(self, meeting_id, payload):
        result = await self._put(meeting_id, payload)
        if result is not None:
            self.pending.pop(meeting_id, None)
            return result
        task = asyncio.create_task(self._retry(meeting_id, payload))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        logger.warning("meeting.record_handoff_pending meeting_id=%s", meeting_id)
        return RecordResult(status="handoff_pending")

    async def _retry(self, meeting_id, payload):
        until = self.monotonic() + self.hold_s
        delay = 5
        try:
            while self.monotonic() < until:
                await self.sleep(min(delay, max(0, until - self.monotonic())))
                if self.monotonic() >= until:
                    break
                if await self._put(meeting_id, payload) is not None:
                    logger.info("meeting.record_handoff_saved meeting_id=%s", meeting_id)
                    return
                delay = min(300, delay * 2)
            logger.error("meeting.record_handoff_failed meeting_id=%s", meeting_id)
        finally:
            self.pending.pop(meeting_id, None)

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
