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

    # E2: generate dreams for active residents.
    try:
        from app.services.dream_service import run_nightly_dreams
        n = await run_nightly_dreams()
        if n:
            logger.info("Generated %d dreams", n)
    except Exception:
        logger.error("Dream generation failed", exc_info=True)

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

    # Lab: expire overdue tasks + auto-release reviewed ones (72h), and dispatch
    # any funded open-recruitment tasks that now have an idle researcher.
    try:
        from app.services.lab_task_service import expire_lab_tasks, dispatch_open_tasks
        async with async_session() as db:
            n = await expire_lab_tasks(db)
        async with async_session() as db:
            d = await dispatch_open_tasks(db)
        if n or d:
            logger.info("Lab: expired/released %d tasks, dispatched %d", n, d)
    except Exception:
        logger.error("Lab task expiry/dispatch failed", exc_info=True)

    # Lab: reap orphaned runs (crashed runner → stale heartbeat) and refund the
    # escrow so no run stays stuck 'running' with money frozen (spec §5.1).
    try:
        n = await sweep_orphan_lab_runs()
        if n:
            logger.info("Lab: reaped %d orphan runs", n)
    except Exception:
        logger.error("Lab orphan-run sweep failed", exc_info=True)

    # V12: pin still-referenced artifact evidence, then tombstone what's past
    # its retention window. apply_retention_holds is protective and always
    # runs; cleanup_expired is destructive and internally no-ops while
    # lab_agent_v1_enabled is off (P2-B review decision: don't destroy
    # evidence during a flag-off rollback window) — the gate lives in the
    # service, not here, so it holds for every caller.
    try:
        from app.config import settings
        from app.services import lab_artifact_service
        pipeline_client = None
        if settings.lab_artifact_pipeline_enabled:
            from app.lab.artifact_pipeline import ArtifactPipelineClient

            pipeline_client = ArtifactPipelineClient.from_settings()
        async with async_session() as db:
            held = await lab_artifact_service.apply_retention_holds(db)
        try:
            async with async_session() as db:
                stats = await lab_artifact_service.cleanup_expired(
                    db, pipeline_client=pipeline_client
                )
        finally:
            if pipeline_client is not None:
                await pipeline_client.aclose()
        if held or stats.get("deleted_count") or stats.get("quarantined_count"):
            logger.info(
                "Lab: retention held %d artifacts, cleanup deleted=%d quarantined=%d",
                held, stats.get("deleted_count", 0), stats.get("quarantined_count", 0),
            )
    except Exception:
        logger.error("Lab artifact retention sweep failed", exc_info=True)

    # A1: weekly life-goal evaluation (Sundays only).
    if datetime.now(UTC).weekday() == 6:
        try:
            await run_weekly_goal_eval()
        except Exception:
            logger.error("Weekly goal eval failed", exc_info=True)
    # Future: E2 dreams, E7 capsule delivery — each own try/except.


async def sweep_orphan_lab_runs() -> int:
    """Reap stale legacy runs and refund their task escrow.

    Protocol-v2 recovery is lease/session/claim driven. A v2 Runtime may have
    committed its final result while the Gateway process died before terminal
    projection, so heartbeat age alone must never fail or refund it.
    """
    from sqlalchemy import select
    from app.models.lab_run import LabRun
    from app.models.lab_task import LabTask
    from app.config import settings
    from app.services.lab_task_service import fail_task

    cutoff = datetime.now(UTC) - timedelta(seconds=settings.lab_run_heartbeat_ttl_s)
    n = 0
    async with async_session() as db:
        stale = (await db.execute(
            select(LabRun).where(
                LabRun.protocol_version == 1,
                LabRun.status.in_(["queued", "running", "needs_approval"]),
                LabRun.heartbeat_at.isnot(None),
                LabRun.heartbeat_at <= cutoff,
            )
        )).scalars().all()
        for run in stale:
            run.status = "failed"
            run.ended_at = datetime.now(UTC)
            run.error = "orphaned: heartbeat stale"
            await db.commit()
            from app.lab import telemetry
            telemetry.emit_alert(
                telemetry.LabAlert.ORPHAN_HEARTBEAT, run_id=run.id, reason="heartbeat_stale",
            )
            task = await db.get(LabTask, run.task_id)
            if task is not None and task.status not in ("completed", "cancelled", "expired", "failed"):
                try:
                    await fail_task(db, task, reason=f"orphan_run:{run.id}")
                except Exception:
                    logger.warning("orphan refund failed for task %s", run.task_id, exc_info=True)
            n += 1
    return n


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
