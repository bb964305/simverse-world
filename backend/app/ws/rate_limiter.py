"""Redis sliding-window rate limiter for WebSocket chat (P0-3b).

Before P0-3b this window lived in process memory, which was only correct for a
single API worker. It now lives in Redis (a per-key sorted set of hit
timestamps) so every worker shares one counter — necessary once the API scales
horizontally.

Semantics are unchanged from the in-process version: ``check`` records a hit
and returns True if allowed, or False *without recording* when the 60s window
is already full (the caller rejects with no side effects).

Why not slowapi here: slowapi is built around HTTP Request/Response and is
awkward to drive from a raw WS receive loop.
"""
import time
import uuid

from app.config import settings
from app.redis_client import get_redis

WINDOW_SECONDS = 60.0
_KEY_PREFIX = "sv:rl:"


class SlidingWindowLimiter:
    """Per-key sliding window over a 60s horizon, backed by a Redis ZSET.

    ``max_per_minute`` is read from ``settings`` lazily on each ``check`` so
    tests (and ops) can raise the limit via env without re-importing. Pass a
    fixed value only for standalone/test instances that must not depend on
    settings. ``namespace`` isolates one limiter's keys from another's.
    """

    def __init__(self, max_per_minute: int | None = None, namespace: str = "ws"):
        self._max = max_per_minute  # None => read settings.ws_rate_limit_per_minute
        self._ns = namespace

    def _limit(self) -> int:
        return self._max if self._max is not None else settings.ws_rate_limit_per_minute

    def _key(self, key: str) -> str:
        return f"{_KEY_PREFIX}{self._ns}:{key}"

    async def check(self, key: str) -> bool:
        """Record a hit for ``key`` and return True if allowed, False if the
        window is already full (caller must reject without side effects)."""
        r = get_redis()
        rkey = self._key(key)
        now = time.time()
        # drop entries older than the 60s window, then count what remains
        await r.zremrangebyscore(rkey, 0, now - WINDOW_SECONDS)
        if await r.zcard(rkey) >= self._limit():
            return False
        # unique member (score collisions at identical timestamps must not
        # overwrite an existing hit)
        await r.zadd(rkey, {f"{now:.6f}-{uuid.uuid4().hex}": now})
        await r.expire(rkey, int(WINDOW_SECONDS) + 1)
        return True

    async def reset(self, key: str | None = None) -> None:
        """Clear counters — used by tests to start from a clean window."""
        r = get_redis()
        if key is None:
            async for k in r.scan_iter(match=f"{_KEY_PREFIX}{self._ns}:*"):
                await r.delete(k)
        else:
            await r.delete(self._key(key))


# Per-user chat limiter. Reads the limit from settings so it honours
# WS_RATE_LIMIT_PER_MINUTE overrides; state is shared across workers via Redis.
ws_limiter = SlidingWindowLimiter()
