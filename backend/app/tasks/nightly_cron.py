"""Nightly cron (runs ~00:30 daily).

One cron, several isolated responsibilities. A5 owns the village digest; A1 weekly
eval / E2 dreams / E7 capsule delivery will hook in here later, each wrapped in
its own try/except so one failing job never blocks the others.
"""

import asyncio
import logging
from datetime import datetime, timedelta, UTC

from app.database import async_session
from app.services.digest_service import generate_village_digest

logger = logging.getLogger(__name__)

RUN_HOUR = 0
RUN_MINUTE = 30


def _seconds_until_next_run(now: datetime) -> float:
    target = now.replace(hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def run_nightly_jobs() -> None:
    """Run each nightly job in isolation."""
    try:
        async with async_session() as db:
            digest = await generate_village_digest(db)
        logger.info("Nightly digest ready: %s", digest.title)
    except Exception:
        logger.error("Nightly village digest failed", exc_info=True)

    # B1: expire stale commissions.
    try:
        from app.services.commission_service import expire_commissions
        async with async_session() as db:
            n = await expire_commissions(db)
        if n:
            logger.info("Expired %d commissions", n)
    except Exception:
        logger.error("Commission expiry failed", exc_info=True)

    # E7: deliver due time capsules.
    try:
        from app.services.capsule_service import deliver_due_capsules
        async with async_session() as db:
            n = await deliver_due_capsules(db)
        if n:
            logger.info("Delivered %d time capsules", n)
    except Exception:
        logger.error("Capsule delivery failed", exc_info=True)

    # A2: schedule upcoming holidays / random news (idempotent).
    try:
        from app.tasks.event_templates import ensure_scheduled_events
        async with async_session() as db:
            n = await ensure_scheduled_events(db)
        if n:
            logger.info("Scheduled %d world events", n)
    except Exception:
        logger.error("Event scheduling failed", exc_info=True)

    # A1: weekly life-goal evaluation (Sundays only).
    if datetime.now(UTC).weekday() == 6:
        try:
            await run_weekly_goal_eval()
        except Exception:
            logger.error("Weekly goal eval failed", exc_info=True)
    # Future: E2 dreams, E7 capsule delivery — each own try/except.


async def run_weekly_goal_eval() -> None:
    """Evaluate every resident that has an active life goal (A1)."""
    from sqlalchemy import select
    from app.models.resident_goal import ResidentGoal
    from app.services.goal_service import weekly_evaluate

    async with async_session() as db:
        resident_ids = (await db.execute(
            select(ResidentGoal.resident_id).where(
                ResidentGoal.kind == "life", ResidentGoal.status == "active",
            )
        )).scalars().all()
    for rid in resident_ids:
        try:
            async with async_session() as db:
                await weekly_evaluate(db, rid)
        except Exception:
            logger.warning("goal eval failed for %s", rid, exc_info=True)


async def nightly_cron_loop() -> None:
    """Sleep until the next 00:30 and run the nightly jobs, forever."""
    while True:
        await asyncio.sleep(_seconds_until_next_run(datetime.now(UTC)))
        await run_nightly_jobs()
