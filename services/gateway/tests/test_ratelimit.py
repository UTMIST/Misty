import json

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from src.api.auth import require_scope
from src.api.deps import get_storage
from src.api.hashing import generate_key
from src.api.middleware import AuditLogMiddleware
from src.api.ratelimit import (
    ClientRateLimitMiddleware,
    FixedWindowCounter,
    build_key_rate_limit,
    client_identity,
)
from src.storage.in_memory import InMemoryStorageAdapter


# --- FixedWindowCounter -------------------------------------------------------


def test_counts_within_a_window_then_refuses():
    c = FixedWindowCounter(limit=2, window_s=60)
    assert c.hit("a", now=100.0) is True
    assert c.hit("a", now=100.5) is True
    assert c.hit("a", now=100.9) is False


def test_identities_are_independent():
    c = FixedWindowCounter(limit=1, window_s=60)
    assert c.hit("a", now=100.0) is True
    assert c.hit("a", now=100.1) is False
    assert c.hit("b", now=100.1) is True


def test_window_rolls_over():
    c = FixedWindowCounter(limit=1, window_s=10)
    assert c.hit("a", now=100.0) is True
    assert c.hit("a", now=105.0) is False
    assert c.hit("a", now=110.0) is True  # new window


def test_capacity_is_hard():
    """The whole point: an attacker varying the identity cannot grow this.

    The previous implementation kept an unbounded dict keyed on the raw
    X-API-Key header, in front of auth, so anyone could add entries without
    limit inside a window — and past 1024 entries each request paid a full scan.
    """
    c = FixedWindowCounter(limit=10, window_s=60, capacity=32)
    for i in range(10_000):
        c.hit(f"attacker-{i}", now=100.0)
    assert len(c) == 32


def test_eviction_drops_the_oldest_window_first():
    c = FixedWindowCounter(limit=10, window_s=60, capacity=3)
    c.hit("oldest", now=100.0)
    c.hit("middle", now=101.0)
    c.hit("newest", now=102.0)
    c.hit("arrival", now=103.0)
    assert "oldest" not in c._hits
    assert set(c._hits) == {"middle", "newest", "arrival"}


def test_restarting_a_window_moves_an_entry_to_the_back():
    # Ordering is what makes O(1) eviction correct: an entry whose window
    # restarts is no longer the oldest and must not be the next one evicted.
    c = FixedWindowCounter(limit=10, window_s=10, capacity=2)
    c.hit("a", now=100.0)
    c.hit("b", now=101.0)
    c.hit("a", now=120.0)  # a's window restarts; a is now the newest
    c.hit("c", now=121.0)  # evicts one entry
    assert "b" not in c._hits
    assert set(c._hits) == {"a", "c"}


# --- client_identity ----------------------------------------------------------


def _request(headers: dict, peer: str = "10.0.0.1") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw, "client": (peer, 1234)})


def test_untrusted_proxy_headers_are_ignored():
    r = _request({"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "5.6.7.8"})
    assert client_identity(r, trust_proxy=False) == "10.0.0.1"


def test_trusted_proxy_takes_the_rightmost_hop():
    """The rightmost entry is the only one the client could not have written.

    A client that sends `X-Forwarded-For: 1.2.3.4` gets the proxy's observed
    address appended after it. Reading the leftmost value — the usual "original
    client" convention — would let anyone rotate their own bucket per request
    and walk straight through this layer.
    """
    r = _request({"X-Forwarded-For": "1.2.3.4, 9.9.9.9"})
    assert client_identity(r, trust_proxy=True) == "9.9.9.9"


def test_trusted_proxy_falls_back_to_real_ip_then_peer():
    assert client_identity(_request({"X-Real-IP": "5.6.7.8"}), trust_proxy=True) == "5.6.7.8"
    assert client_identity(_request({}), trust_proxy=True) == "10.0.0.1"


# --- per-IP middleware --------------------------------------------------------


def _ip_app(**kwargs) -> TestClient:
    app = FastAPI()

    @app.get("/x")
    def x():
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    app.add_middleware(ClientRateLimitMiddleware, **kwargs)
    return TestClient(app)


def test_ip_limit_applies_without_any_key():
    # The point of this layer: it bounds unauthenticated callers, who by
    # definition have no key to meter.
    c = _ip_app(limit=2, window_s=60)
    assert c.get("/x").status_code == 200
    assert c.get("/x").status_code == 200
    assert c.get("/x").status_code == 429


def test_rotating_the_api_key_does_not_escape_the_ip_limit():
    c = _ip_app(limit=2, window_s=60)
    assert c.get("/x", headers={"X-API-Key": "gw_one"}).status_code == 200
    assert c.get("/x", headers={"X-API-Key": "gw_two"}).status_code == 200
    assert c.get("/x", headers={"X-API-Key": "gw_three"}).status_code == 429


def test_health_is_exempt():
    # Railway's liveness probe hits /health on a fixed interval; 429ing it would
    # restart a service that is answering perfectly well.
    c = _ip_app(limit=1, window_s=60)
    for _ in range(5):
        assert c.get("/health").status_code == 200


# --- per-key quota ------------------------------------------------------------


def _keyed_app(limit: int):
    store = InMemoryStorageAdapter()
    plaintext, prefix, key_hash = generate_key()
    store.create_api_key(
        name="consumer", prefix=prefix, key_hash=key_hash, scopes=["resolve:discord"], actor="t"
    )
    other_plain, other_prefix, other_hash = generate_key()
    store.create_api_key(
        name="other",
        prefix=other_prefix,
        key_hash=other_hash,
        scopes=["resolve:discord"],
        actor="t",
    )

    counter = FixedWindowCounter(limit=limit, window_s=60)
    app = FastAPI(dependencies=[Depends(build_key_rate_limit(counter))])

    @app.get("/probe")
    def probe(_=Depends(require_scope("resolve:discord"))):
        return {"ok": True}

    app.dependency_overrides[get_storage] = lambda: store
    return TestClient(app), plaintext, other_plain


def test_key_quota_is_per_issued_key():
    c, key, other = _keyed_app(limit=2)
    assert c.get("/probe", headers={"X-API-Key": key}).status_code == 200
    assert c.get("/probe", headers={"X-API-Key": key}).status_code == 200
    assert c.get("/probe", headers={"X-API-Key": key}).status_code == 429
    # A different issued key has its own bucket.
    assert c.get("/probe", headers={"X-API-Key": other}).status_code == 200


def test_unauthenticated_requests_never_reach_the_key_counter():
    """401 must win over 429, and an anonymous caller must not create a bucket.

    This is the structural fix: the quota runs behind require_api_key, so the
    only strings it can ever be keyed on are names of keys we issued.
    """
    c, key, _ = _keyed_app(limit=1)
    for _ in range(5):
        assert c.get("/probe").status_code == 401
        assert c.get("/probe", headers={"X-API-Key": "gw_bogus"}).status_code == 401
    # The real key still has its full quota — the noise above consumed none.
    assert c.get("/probe", headers={"X-API-Key": key}).status_code == 200


def test_429_is_still_audited(capsys):
    # Audit is added last, so it is outermost and observes the flood guard's
    # short-circuit. Mirrors the order in src.api.app.create_app().
    app = FastAPI()

    @app.get("/x")
    def x():
        return {"ok": True}

    app.add_middleware(ClientRateLimitMiddleware, limit=1, window_s=60)
    app.add_middleware(AuditLogMiddleware, logger_name="gateway.audit")

    c = TestClient(app)
    assert c.get("/x").status_code == 200
    assert c.get("/x").status_code == 429

    entries = [
        json.loads(line)
        for line in capsys.readouterr().out.strip().splitlines()
        if line.startswith("{")
    ]
    statuses = [e.get("status") for e in entries]
    assert 429 in statuses
    assert 200 in statuses
