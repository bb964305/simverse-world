"""Global + per-researcher run-concurrency semaphore (recovery plan Phase 4, gap #4).

Enforces ``lab_max_concurrent_runs`` and ``lab_max_concurrent_per_researcher``
across ALL Lab Runner processes via ATOMIC Redis counters — never a process-local
variable, a count-then-insert, or the UI ``busy`` flag. A Runner reserves a slot
before it executes a dequeued run (``try_reserve``) and releases it in a finally
(``release``); a slot leaked by a crashed Runner (or a negative counter from an
over-release) is healed by ``reconcile``, which re-syncs each counter to the DB's
true count of active runs. ``INCR`` is atomic, so N racing reservers get N
distinct values and only those at-or-below the cap proceed — the (cap+1)th
reserver sees an over-limit value, releases, and backs off.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select

from app.config import settings
from app.models.lab_run import LabRun
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

GLOBAL_KEY = "sv:lab:running"
# A run holds a Runner (and thus a slot) while it is being executed — that is the
# running or paused-for-approval window, NOT the queued backlog waiting in Redis.
_SLOT_STATES = ("running", "needs_approval")


def _rkey(slug: str) -> str:
    return f"sv:lab:running:{slug}"


def _global_limit() -> int:
    return int(getattr(settings, "lab_max_concurrent_runs", 0) or 0)


def _researcher_limit() -> int:
    return int(getattr(settings, "lab_max_concurrent_per_researcher", 0) or 0)


async def try_reserve(*, researcher_slug: str | None) -> bool:
    """Atomically reserve a global (and per-researcher) run slot. Returns False —
    releasing anything it took — when a cap would be exceeded; the caller must
    requeue and back off. A non-positive limit disables that cap."""
    r = get_redis()
    gl, rl = _global_limit(), _researcher_limit()
    took_global = False
    try:
        if gl > 0:
            g = int(await r.incr(GLOBAL_KEY))
            took_global = True
            if g > gl:
                await r.decr(GLOBAL_KEY)
                return False
        if researcher_slug and rl > 0:
            c = int(await r.incr(_rkey(researcher_slug)))
            if c > rl:
                await r.decr(_rkey(researcher_slug))
                if took_global:
                    await r.decr(GLOBAL_KEY)
                return False
        return True
    except Exception:
        # Fail OPEN on a Redis fault would break the cap; fail CLOSED (refuse) so
        # a broken semaphore can never over-admit. Roll back any partial take.
        logger.warning("lab concurrency reserve faulted; refusing admission", exc_info=True)
        try:
            if took_global:
                await r.decr(GLOBAL_KEY)
        except Exception:
            pass
        return False


async def release(*, researcher_slug: str | None) -> None:
    """Release a reserved slot. An occasional over/under-DECR is self-healed by
    ``reconcile`` re-syncing to the true active-run count."""
    r = get_redis()
    try:
        if _global_limit() > 0:
            await r.decr(GLOBAL_KEY)
        if researcher_slug and _researcher_limit() > 0:
            await r.decr(_rkey(researcher_slug))
    except Exception:
        logger.warning("lab concurrency release failed", exc_info=True)


async def reconcile(db) -> dict:
    """Re-sync the Redis counters to the DB's true count of active (running /
    needs_approval) runs — heals a slot leaked by a crashed Runner and a negative
    counter from a double-release. Runs at Runner startup and periodically."""
    r = get_redis()
    total = (await db.execute(
        select(func.count()).select_from(LabRun).where(LabRun.status.in_(_SLOT_STATES))
    )).scalar() or 0
    await r.set(GLOBAL_KEY, int(total))
    rows = (await db.execute(
        select(LabRun.researcher_slug, func.count())
        .where(LabRun.status.in_(_SLOT_STATES))
        .group_by(LabRun.researcher_slug)
    )).all()
    per: dict[str, int] = {}
    for slug, cnt in rows:
        if slug:
            await r.set(_rkey(slug), int(cnt))
            per[slug] = int(cnt)
    return {"global": int(total), "researchers": per}
