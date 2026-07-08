"""Process-wide shared async Redis client (P0-3b).

The ConnectionManager online-state/locks/queues, the agent daily-action
counter, and the WS rate limiter all live in Redis so that multiple API
workers (and the standalone agent-worker) share one source of truth and can
fan out realtime events to each other over pub/sub.

Mirrors app/http.py: one lazily-created client for the whole process, closed
from the app/worker lifespan on shutdown. ``decode_responses=True`` means every
command returns ``str`` (never ``bytes``), which keeps the manager code and the
Lua-free lock/queue helpers simple.

Tests inject a ``fakeredis`` instance via ``set_redis`` (see tests/conftest.py)
so the suite never needs a running server.
"""
from __future__ import annotations

import redis.asyncio as aioredis

from app.config import settings

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Return the shared client, creating it from ``settings.redis_url`` if absent."""
    global _client
    if _client is None:
        _client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _client


def set_redis(client: aioredis.Redis | None) -> None:
    """Override the shared client (test hook). Passing None clears it."""
    global _client
    _client = client


async def close_redis() -> None:
    """Close the shared client. Called from the app/worker lifespan on shutdown."""
    global _client
    client, _client = _client, None
    if client is not None:
        try:
            await client.aclose()
        except Exception:  # pragma: no cover — best-effort shutdown
            pass
