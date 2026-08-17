"""Rate limiting for the public door.

Two layers, because they defend different things.

`enforce_key_rate_limit` is the real per-consumer quota. It runs as a FastAPI
dependency *after* platform_auth has resolved the key, and counts against
`AuthedKey.name` — a value that can only have come from a row in `api_keys`.
That matters as much for the bookkeeping as for the quota: the set of names is
bounded by the number of keys we have issued, so nobody on the internet can grow
the counter, and no plaintext key is held in process memory.

`ClientRateLimitMiddleware` sits in front of auth and counts per client IP.
Authentication is itself the expensive step — a well-formed `gw_` key forces an
argon2 verification, which is deliberately slow — so something has to bound how
fast an unauthenticated caller can demand one, and per-key limiting cannot: an
attacker simply varies the key. Its limit is loose on purpose. It is a
floodgate, not a quota.

Both sit on FixedWindowCounter, which is bounded by construction. The first
version of this module keyed an unbounded dict on the raw `X-API-Key` header, in
front of auth: anyone could grow it without limit inside a window, and every
request past 1024 entries then paid a full O(n) scan (twice).

In-memory and process-local, which is correct because the gateway runs a single
replica. A shared store (Redis) is needed only if it is ever scaled past one.
"""

import time
from collections import OrderedDict

from fastapi import Depends, HTTPException, status
from platform_auth import AuthedKey
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.api.auth import require_api_key

_TOO_MANY = "rate limit exceeded"

# Per issued key. The quota an external consumer actually gets.
KEY_LIMIT = 60
KEY_WINDOW_S = 60.0

# Per client IP, in front of auth. Set well above the per-key quota: this is not
# trying to meter anyone, only to cap how much argon2 work one address can
# demand. Note that with TRUST_PROXY_HEADERS unset behind a proxy, every caller
# shares one bucket — another reason to keep it loose.
IP_LIMIT = 120
IP_WINDOW_S = 60.0


class FixedWindowCounter:
    """Per-identity fixed-window counter with a hard capacity.

    The capacity is the point. `hit()` takes an identity supplied — directly or
    indirectly — by the caller, so an unbounded dict is a memory-growth vector
    for anyone who can vary it. Entries are held in insertion order and an entry
    is re-inserted whenever its window restarts, so the front of the dict is
    always the oldest window: evicting from the front drops the entry closest to
    expiring, which is what a scan for expired entries would have picked anyway,
    at O(1) instead of O(n).

    Eviction under pressure is deliberately permissive — a flood of fresh
    identities can push a legitimate one out and hand it a fresh window. That
    trade is on purpose: the alternative, refusing new identities once full,
    would let an attacker lock everyone else out, turning a rate limiter into a
    denial-of-service tool.
    """

    def __init__(self, *, limit: int, window_s: float, capacity: int = 8192) -> None:
        self._limit = limit
        self._window = window_s
        self._capacity = capacity
        self._hits: OrderedDict[str, tuple[int, float]] = OrderedDict()

    def hit(self, identity: str, now: float | None = None) -> bool:
        """Record a request for `identity`. False means it is over the limit."""
        now = time.monotonic() if now is None else now
        entry = self._hits.get(identity)
        if entry is not None:
            count, start = entry
            if now - start < self._window:
                self._hits[identity] = (count + 1, start)
                return count + 1 <= self._limit
            # Window elapsed. Drop it so the re-insert below puts it at the
            # back, keeping the dict ordered by window start.
            del self._hits[identity]
        while len(self._hits) >= self._capacity:
            self._hits.popitem(last=False)
        self._hits[identity] = (1, now)
        return 1 <= self._limit

    def clear(self) -> None:
        self._hits.clear()

    def __len__(self) -> int:
        return len(self._hits)


def client_identity(request: Request, *, trust_proxy: bool) -> str:
    """Best available identifier for the caller, for per-IP limiting.

    With `trust_proxy`, take the *rightmost* X-Forwarded-For entry. A proxy
    appends the address it actually saw, so the rightmost hop is the one value
    in that header the client could not have written itself. Taking the leftmost
    — the usual "original client" reading — would let anyone reset their own
    bucket by sending a fresh X-Forwarded-For on every request, which defeats
    the whole layer.

    Without `trust_proxy` the headers are ignored entirely and the socket peer
    is used. Behind an unacknowledged proxy that collapses every caller into one
    bucket, which is why IP_LIMIT is set loose enough not to bite a real
    consumer.
    """
    if trust_proxy:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
            if hops:
                return hops[-1]
        real_ip = request.headers.get("X-Real-IP")
        if real_ip and real_ip.strip():
            return real_ip.strip()
    return request.client.host if request.client else "unknown"


class ClientRateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP flood guard, mounted in front of auth. See the module docstring."""

    def __init__(
        self,
        app,
        *,
        limit: int = IP_LIMIT,
        window_s: float = IP_WINDOW_S,
        trust_proxy: bool = False,
        capacity: int = 8192,
    ) -> None:
        super().__init__(app)
        self._counter = FixedWindowCounter(limit=limit, window_s=window_s, capacity=capacity)
        self._trust_proxy = trust_proxy

    async def dispatch(self, request: Request, call_next):
        # /health is exempt: it is unauthenticated, costs nothing, and Railway's
        # liveness probe hits it on a fixed interval. Letting the flood guard
        # 429 the probe would restart a service that is answering fine.
        if request.url.path == "/health":
            return await call_next(request)
        if not self._counter.hit(client_identity(request, trust_proxy=self._trust_proxy)):
            return JSONResponse(status_code=429, content={"detail": _TOO_MANY})
        return await call_next(request)


def build_key_rate_limit(counter: FixedWindowCounter):
    """FastAPI dependency enforcing `counter` against the authenticated key."""

    def _dep(key: AuthedKey = Depends(require_api_key)) -> AuthedKey:
        if not counter.hit(key.name):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=_TOO_MANY
            )
        return key

    return _dep


# The process-wide per-key quota, mounted on the /v1 router (see routers/resolve.py).
key_rate_limit_counter = FixedWindowCounter(limit=KEY_LIMIT, window_s=KEY_WINDOW_S)
enforce_key_rate_limit = build_key_rate_limit(key_rate_limit_counter)
