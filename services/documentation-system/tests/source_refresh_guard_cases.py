"""Shared guard contract: atomic admission and owner-specific release."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4


def assert_refresh_guard(adapter, peer):
    first, second = uuid4(), uuid4()
    ready = Barrier(2)

    def acquire(store, run_id):
        ready.wait(timeout=5)
        return store.acquire_source_refresh_run(run_id, actor="scheduler")

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(acquire, adapter, first)
        b = pool.submit(acquire, peer, second)
        results = [a.result(timeout=10), b.result(timeout=10)]
    assert sorted(results) == [False, True]
    owner, loser = (first, second) if results[0] else (second, first)
    # Replays are rejected too: a reused UUID must not admit a second process.
    assert peer.acquire_source_refresh_run(owner, actor="scheduler") is False
    assert peer.release_source_refresh_run(loser) is False
    assert peer.acquire_source_refresh_run(loser, actor="scheduler") is False
    assert peer.release_source_refresh_run(owner) is True
    assert adapter.release_source_refresh_run(owner) is False
    assert peer.acquire_source_refresh_run(loser, actor="scheduler") is True
    assert adapter.release_source_refresh_run(loser) is True
