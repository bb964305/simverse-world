"""Redis task queue for Lab runs (spec §5.1).

At-least-once: BRPOPLPUSH moves a run_id from the pending list into a processing
list; the runner explicitly ACKs (LREM) only after the run terminates. A runner
crash mid-run therefore leaves the id in ``processing``, where the watchdog
(nightly_cron) can reap the orphaned run + refund escrow. Naked LPUSH/BRPOP
would silently drop the task on crash.
"""
import logging

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

QUEUE_KEY = "sv:lab:queue"           # pending run ids (LPUSH producer / BRPOPLPUSH consumer)
PROCESSING_KEY = "sv:lab:processing"  # in-flight run ids awaiting ACK


async def enqueue_run(run_id: str) -> None:
    await get_redis().lpush(QUEUE_KEY, run_id)


async def dequeue_run(timeout: int = 5) -> str | None:
    """Block up to ``timeout`` s for a run id, atomically moving it to the
    processing list. Returns None on timeout."""
    return await get_redis().brpoplpush(QUEUE_KEY, PROCESSING_KEY, timeout=timeout)


async def ack_run(run_id: str) -> None:
    """Remove a finished run id from the processing list (explicit ack)."""
    await get_redis().lrem(PROCESSING_KEY, 0, run_id)


async def list_processing() -> list[str]:
    return await get_redis().lrange(PROCESSING_KEY, 0, -1)


async def requeue_run(run_id: str) -> None:
    """Move an id from processing back to pending (orphan recovery)."""
    r = get_redis()
    await r.lrem(PROCESSING_KEY, 0, run_id)
    await r.lpush(QUEUE_KEY, run_id)
