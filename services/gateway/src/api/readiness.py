"""Bound database readiness work independently of public request volume."""

from threading import Lock
from time import monotonic

from contracts.storage import StorageAdapter

READINESS_CACHE_SECONDS = 5.0


class ReadinessProbe:
    """Cache success and failure per app, allowing only one probe at a time.

    Never wait on another probe: a readiness flood must not fill the request
    thread pool with lock waiters. While an expired result is being refreshed,
    concurrent callers receive an unready result instead of stale success.
    """

    def __init__(self) -> None:
        self._result = (False, 0.0)
        self._lock = Lock()

    def is_ready(self, storage: StorageAdapter) -> bool:
        if not self._lock.acquire(blocking=False):
            ready, expires_at = self._result
            return ready and monotonic() < expires_at
        try:
            ready, expires_at = self._result
            if monotonic() >= expires_at:
                ready = storage.is_ready()
                # Start the TTL after completion, including for slow/failed checks.
                self._result = (ready, monotonic() + READINESS_CACHE_SECONDS)
            return ready
        finally:
            self._lock.release()
