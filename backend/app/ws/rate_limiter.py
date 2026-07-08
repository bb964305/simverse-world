"""In-process sliding-window rate limiter for WebSocket chat.

Single-worker sufficiency: the ConnectionManager (`app/ws/manager.py`) is
in-process today, so a per-user window held in process memory is correct.
Once P0-3b lands the cross-process Redis bus, this migrates to Redis
INCR-with-expire so multiple API workers share the same counters.

Why not slowapi here: slowapi is built around HTTP Request/Response and is
awkward to drive from a raw WS receive loop. A 30-line sliding window is
the right shape for the WS path.
"""
import time
from collections import defaultdict

from app.config import settings


class SlidingWindowLimiter:
    """Per-key fixed-size sliding window over a 60s horizon.

    ``check`` is O(1) amortized: expired entries are dropped lazily from the
    front of the deque each call.
    """

    def __init__(self, max_per_minute: int):
        self._max = max_per_minute
        self._hits: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> bool:
        """Record a hit for ``key`` and return True if allowed, False if the
        window is already full (caller must reject without side effects)."""
        now = time.monotonic()
        window = self._hits[key]
        cutoff = now - 60.0
        # drop entries older than the 60s window (lazy expiry)
        while window and window[0] < cutoff:
            window.pop(0)
        if len(window) >= self._max:
            return False
        window.append(now)
        return True

    def reset(self, key: str | None = None) -> None:
        """Clear counters — used by tests to start from a clean window."""
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)


# Per-user chat limiter, tied to ConnectionManager's lifetime.
ws_limiter = SlidingWindowLimiter(settings.ws_rate_limit_per_minute)
