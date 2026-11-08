"""
Rate limiting: in-memory token buckets keyed by client (DESIGN.md §2).

  - Query endpoints: RATE_LIMIT_PER_MINUTE sustained, RATE_LIMIT_BURST burst, per client.
  - Webhooks: correctly signed requests are never limited (the signature is the access
    control, and a 429 would make the provider retry or drop events). Only *failed*
    signature attempts draw from INVALID_SIGNATURE_LIMIT_PER_MINUTE, per client.

Client identity: the connection address, unless the connection comes from a configured
trusted proxy (TRUSTED_PROXIES), in which case the right-most X-Forwarded-For entry —
the address that proxy saw — is used. X-Forwarded-For is never trusted otherwise:
anyone can send it, and honouring it would let a client dodge the limit or drain
another client's bucket.

Single process only (state is in memory); multiple instances would need Redis or a gateway.
"""

import math
import time
from dataclasses import dataclass
from threading import Lock
from typing import Callable

from fastapi import HTTPException, Request, Response, status

from app.config import settings

MAX_TRACKED_CLIENTS = 10_000


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int  # whole seconds until one token is available (0 if allowed)


class RateLimiter:
    """Token bucket per key: `burst` capacity, refilled at `per_minute` tokens/minute."""

    def __init__(self, per_minute: float, burst: int, clock: Callable[[], float] = time.monotonic):
        self.per_minute = per_minute
        self.burst = max(1, int(burst))
        self._rate_per_sec = per_minute / 60.0
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_refill)
        self._lock = Lock()

    def hit(self, key: str) -> Decision:
        """Try to consume one token for `key`."""
        with self._lock:
            now = self._clock()
            tokens, last = self._buckets.get(key, (float(self.burst), now))
            tokens = min(float(self.burst), tokens + (now - last) * self._rate_per_sec)

            if tokens >= 1:
                tokens -= 1
                decision = Decision(True, self.burst, int(tokens), 0)
            else:
                wait = (1 - tokens) / self._rate_per_sec if self._rate_per_sec > 0 else math.inf
                decision = Decision(False, self.burst, 0, max(1, math.ceil(wait)))

            self._buckets[key] = (tokens, now)
            if len(self._buckets) > MAX_TRACKED_CLIENTS:
                self._evict(now)
            return decision

    def _evict(self, now: float) -> None:
        """Drop buckets that have refilled completely (idle clients); they'd start full anyway."""
        full_after = self.burst / self._rate_per_sec if self._rate_per_sec > 0 else math.inf
        self._buckets = {k: v for k, v in self._buckets.items() if now - v[1] < full_after}
        if len(self._buckets) > MAX_TRACKED_CLIENTS:  # still too many: drop oldest
            keep = sorted(self._buckets.items(), key=lambda kv: kv[1][1])[-MAX_TRACKED_CLIENTS:]
            self._buckets = dict(keep)


# Limiters are rebuilt when their settings change (also lets tests reconfigure them).
_limiters: dict[str, tuple[tuple, RateLimiter]] = {}
_limiters_lock = Lock()


def _limiter(name: str, per_minute: float, burst: int) -> RateLimiter | None:
    if per_minute <= 0:
        return None  # disabled
    config = (per_minute, burst)
    with _limiters_lock:
        cached = _limiters.get(name)
        if cached is None or cached[0] != config:
            cached = (config, RateLimiter(per_minute, burst))
            _limiters[name] = cached
        return cached[1]


def reset_limiters() -> None:
    """Forget all buckets (tests)."""
    with _limiters_lock:
        _limiters.clear()


def _trusted_proxies() -> set[str]:
    return {ip.strip() for ip in settings.trusted_proxies.split(",") if ip.strip()}


def client_key(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if peer in _trusted_proxies():
        forwarded = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        if forwarded:
            # The right-most entry was appended by our trusted proxy; anything to its
            # left was supplied by the client and can't be trusted.
            return forwarded[-1]
    return peer


def _too_many(decision: Decision, detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        detail,
        headers={
            "Retry-After": str(decision.retry_after),
            "X-RateLimit-Limit": str(decision.limit),
            "X-RateLimit-Remaining": "0",
        },
    )


def limit_queries(request: Request, response: Response) -> None:
    """FastAPI dependency for the query endpoints."""
    limiter = _limiter("queries", settings.rate_limit_per_minute, settings.rate_limit_burst)
    if limiter is None:
        return
    decision = limiter.hit(client_key(request))
    if not decision.allowed:
        raise _too_many(decision, "Rate limit exceeded")
    response.headers["X-RateLimit-Limit"] = str(decision.limit)
    response.headers["X-RateLimit-Remaining"] = str(decision.remaining)


def record_invalid_signature(request: Request) -> None:
    """
    Called only after a webhook signature check fails. Raises 429 once a client has
    made too many failed attempts; otherwise returns so the caller can send its 401.
    """
    per_minute = settings.invalid_signature_limit_per_minute
    limiter = _limiter("invalid_signatures", per_minute, int(per_minute))
    if limiter is None:
        return
    decision = limiter.hit(client_key(request))
    if not decision.allowed:
        raise _too_many(decision, "Too many requests with invalid signatures")
