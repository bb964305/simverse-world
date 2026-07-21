"""Protocol-isolated Redis task queues for Lab runs (spec §5.1).

At-least-once: BRPOPLPUSH moves a run_id from the pending list into a processing
list; the runner explicitly ACKs (LREM) only after the run terminates. A runner
crash mid-run therefore leaves the id in ``processing``, where the watchdog
(nightly_cron) can reap the orphaned run + refund escrow. Naked LPUSH/BRPOP
would silently drop the task on crash.

The protocol version is required on every operation. There is deliberately no
shared-key alias or implicit-v1 fallback: an old or malformed caller must fail
closed instead of making a v1 worker eligible to claim a v2 run.
"""
import logging

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

V1_QUEUE_KEY = "sv:lab:v1:queue"
V1_PROCESSING_KEY = "sv:lab:v1:processing"
V2_QUEUE_KEY = "sv:lab:v2:queue"
V2_PROCESSING_KEY = "sv:lab:v2:processing"

_QUEUE_KEYS = {
    1: (V1_QUEUE_KEY, V1_PROCESSING_KEY),
    2: (V2_QUEUE_KEY, V2_PROCESSING_KEY),
}
LEGACY_QUEUE_KEYS = ("sv:lab:queue", "sv:lab:processing")


class LegacyQueueNotDrained(RuntimeError):
    """The pre-split queue still owns work and cutover must stop."""


def queue_keys(protocol_version: int) -> tuple[str, str]:
    """Return ``(pending, processing)`` keys for one canonical protocol.

    ``bool`` and string lookalikes are rejected even though Python/Redis could
    coerce them. The database contract stores protocol versions as integers.
    """
    if type(protocol_version) is not int or protocol_version not in _QUEUE_KEYS:
        raise ValueError(f"unsupported Lab protocol_version: {protocol_version!r}")
    return _QUEUE_KEYS[protocol_version]


async def require_legacy_queues_drained() -> None:
    """Fail before cutover rather than aliasing or silently stranding old work."""
    redis = get_redis()
    depths = {
        key: int(await redis.llen(key))
        for key in LEGACY_QUEUE_KEYS
    }
    remaining = {key: depth for key, depth in depths.items() if depth}
    if remaining:
        detail = ", ".join(f"{key}={depth}" for key, depth in remaining.items())
        raise LegacyQueueNotDrained(
            f"legacy Lab queues must be drained before protocol split: {detail}"
        )


async def enqueue_run(run_id: str, *, protocol_version: int) -> None:
    pending_key, _ = queue_keys(protocol_version)
    await get_redis().lpush(pending_key, run_id)


async def dequeue_run(*, protocol_version: int, timeout: float = 5) -> str | None:
    """Block up to ``timeout`` s for a run id, atomically moving it to the
    processing list. Returns None on timeout."""
    pending_key, processing_key = queue_keys(protocol_version)
    return await get_redis().brpoplpush(
        pending_key, processing_key, timeout=timeout
    )


async def ack_run(run_id: str, *, protocol_version: int) -> None:
    """Remove a finished run id from the processing list (explicit ack)."""
    _, processing_key = queue_keys(protocol_version)
    await get_redis().lrem(processing_key, 0, run_id)


async def list_processing(*, protocol_version: int) -> list[str]:
    _, processing_key = queue_keys(protocol_version)
    return await get_redis().lrange(processing_key, 0, -1)


async def requeue_run(run_id: str, *, protocol_version: int) -> None:
    """Move an id from processing back to pending (orphan recovery)."""
    pending_key, processing_key = queue_keys(protocol_version)
    r = get_redis()
    await r.lrem(processing_key, 0, run_id)
    await r.lpush(pending_key, run_id)
