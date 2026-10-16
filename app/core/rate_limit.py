# ============================================================
# WAYTERO — RATE LIMITING
# File: app/core/rate_limit.py
#
# Fixed-window per-client limiter for the public (no-auth) capture
# endpoints: enquiries, callback requests, feedback lookup and feedback
# submission. Those forms are open to the internet, so without a limit one
# script can fill the admin queues or walk booking numbers.
#
# Redis when it is up (shared across workers, survives a restart), otherwise
# an in-process window. There is no third option today.
#
# FAIL-OPEN BY DESIGN. A Redis blip or a serialization hiccup must not return
# 429 to a real customer trying to send an enquiry — the same trade
# app/core/lifespan.py makes for OTP and caching ("Phase 0 continues without
# it"). The limiter is a spam deterrent, not a security control; anything that
# must be enforced belongs in the endpoint's own validation.
# ============================================================

import time
from collections import deque
from typing import Deque, Dict, List

from fastapi import Request

from app.core.exceptions import RateLimitExceededException
from app.core.logging import get_logger

logger = get_logger(__name__)

_KEY_PREFIX = "wt:rl"

# In-process windows, one deque of attempt timestamps per key. Only reached
# when Redis is unavailable, so it is a per-worker view of the traffic.
_MEMORY: Dict[str, Deque[float]] = {}

# A key is `wt:rl:{scope}:{client}`, so growth is bounded by distinct
# (form, visitor) pairs. This is the backstop: if it is ever hit, expired
# windows are dropped before the new one is recorded.
_MEMORY_KEY_LIMIT = 50_000


def _client_ip(request: Request) -> str:
    """Best available client address.

    On the VPS the app sits behind nginx, which sets X-Forwarded-For, so the
    first entry is trustworthy. Reachable without that proxy this header is
    client-supplied and a spammer could rotate through fake values — which is
    another reason the limiter stays advisory rather than load-bearing.
    """
    forwarded = request.headers.get("x-forwarded-for") or ""
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def _prune(key: str, now: float, window: int) -> Deque[float]:
    hits = _MEMORY.get(key)
    if hits is None:
        if len(_MEMORY) > _MEMORY_KEY_LIMIT:
            cutoff = now - window
            for stale in [k for k, dq in _MEMORY.items() if not dq or dq[-1] < cutoff]:
                _MEMORY.pop(stale, None)
        hits = _MEMORY[key] = deque()
    cutoff = now - window
    while hits and hits[0] < cutoff:
        hits.popleft()
    return hits


class RateLimit:
    """FastAPI dependency: ``Depends(RateLimit("enquiry", 5, 600))``.

    Raises RateLimitExceededException (HTTP 429) once ``limit`` attempts are
    recorded for one client inside ``window_seconds``; returns None otherwise.
    """

    def __init__(self, scope: str, limit: int, window_seconds: int) -> None:
        self.scope = scope
        self.limit = limit
        self.window_seconds = window_seconds

    async def __call__(self, request: Request) -> None:
        key = f"{_KEY_PREFIX}:{self.scope}:{_client_ip(request)}"
        redis = getattr(request.app.state, "redis", None)

        if redis is not None:
            try:
                attempts = await redis.incr(key)
                if int(attempts) == 1:
                    # First hit in this window — set its expiry. Losing it
                    # would leave the key counting forever.
                    await redis.expire(key, self.window_seconds)
                if int(attempts) > self.limit:
                    ttl = max(int(await redis.ttl(key) or 0), 0)
                    minutes = max(1, (ttl + 59) // 60)
                    raise RateLimitExceededException(
                        "Too many requests from this device. Please try again "
                        f"in about {minutes} "
                        f"{'minute' if minutes == 1 else 'minutes'}.",
                        retry_after=max(ttl, 1),
                    )
                return
            except RateLimitExceededException:
                raise
            except Exception as exc:  # pragma: no cover - Redis is down
                logger.warning(
                    "rate_limit_redis_unavailable",
                    scope=self.scope,
                    error=str(exc),
                    impact="Falling back to the in-process window for this request.",
                )

        now = time.monotonic()
        hits = _prune(key, now, self.window_seconds)
        if len(hits) >= self.limit:
            wait = int(self.window_seconds - (now - hits[0])) + 1
            raise RateLimitExceededException(
                "Too many requests from this device. Please wait a few minutes "
                "and try again.",
                retry_after=max(wait, 1),
            )
        hits.append(now)


__all__: List[str] = ["RateLimit"]
