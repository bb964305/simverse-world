"""Nightly cron (runs at the Beijing morning anchor, once per real day).

One cron, several isolated responsibilities. A5 owns the village digest; A1 weekly
eval / E2 dreams / E7 capsule delivery will hook in here later, each wrapped in
its own try/except so one failing job never blocks the others.

World time (agent-T): the cron keeps its true 24-REAL-hour cadence — k does not
change how often it fires — but its anchor is a Beijing morning hour so the
digest is ready to read when players wake up. Jobs that are "weekly" in world
terms (A1 goal eval, relation decay) can no longer key off ``weekday()==k``:
a world week is only 1.75 real days, so equality would misfire. They gate on the
world-week ordinal stored in Redis and fire when it advances (§5).
"""

import asyncio
import logging
from datetime import datetime, timedelta, UTC

from app.database import async_session
from app.redis_client import get_redis
from app.services.digest_service import generate_village_digest
from app.tasks.loop_heartbeat import beat
from app.world_clock import now_real, world_week_index

logger = logging.getLogger(__name__)

# Beijing-morning anchor (real time): the digest lands in the early Beijing
# morning, readable at breakfast. Real 24h cadence is unchanged by k.
RUN_HOUR = 7
RUN_MINUTE = 0

# Redis keys holding the last world-week ordinal each weekly job ran for. A job
# fires when the current world week is greater than the stored one, then writes
# the new ordinal back — so it runs exactly once per world week regardless of how
# many real days (~1.75) that spans.
_GOAL_WEEK_KEY = "sv:nightly:last_goal_week"
_DECAY_WEEK_KEY = "sv:nightly:last_decay_week"

# R3 (eng-health A): ledger of the anchor DATE this cron last ran for. Without
# it a crash / container restart / deploy window straddling the RUN_HOUR anchor
# silently dropped that whole day's nightly batch — no log, no alert. Same
# Redis idempotency shape as _world_week_gate above, keyed by date instead of
# world-week ordinal.
_LAST_RUN_DATE_KEY = "sv:nightly:last_run_date"


def _seconds_until_next_run(now: datetime) -> float:
    target = now.replace(hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _world_week_gate(redis_key: str) -> bool:
    """Return True at most once per world week for ``redis_key``.

    Reads the last world-week ordinal this gate ran for from Redis; if the
    current world week has advanced past it (or nothing is stored yet), records
    the new ordinal and returns True. Two real runs inside the same world week
    → only the first passes; a run that crosses into a new world week → passes.
    """
    current = world_week_index()
    raw = await get_redis().get(redis_key)
    last = int(raw) if raw is not None else None
    if last is not None and current <= last:
        return False
    await get_redis().set(redis_key, str(current))
    return True


# --------------------------------------------------------------------------- #
# R3 — missed-window catch-up (eng-health A)                                   #
# --------------------------------------------------------------------------- #

def _anchor_passed(now: datetime) -> bool:
    """True once today's RUN_HOUR:RUN_MINUTE anchor is in the past."""
    return now >= now.replace(hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)


def _anchor_date(now: datetime) -> str:
    """ISO date of the anchor the given instant belongs to.

    Before today's anchor the current "nightly day" is still yesterday's, so a
    03:00 restart must not claim today's slot and suppress the 07:00 run.
    """
    day = now.date() if _anchor_passed(now) else (now - timedelta(days=1)).date()
    return day.isoformat()


async def _claim_run_date(date_str: str) -> bool:
    """Claim ``date_str`` in the ledger. True = this caller should run.

    SET NX + follow-up GET (the fakeredis-safe shape used by ws.manager locks):
    the first claimant of an anchor date wins, a same-date re-entry loses, a new
    date overwrites. **Fail-open**: a broken ledger must never silence the whole
    nightly batch, so a Redis error returns True.
    """
    try:
        r = get_redis()
        if await r.set(_LAST_RUN_DATE_KEY, date_str, nx=True):
            return True
        if (await r.get(_LAST_RUN_DATE_KEY)) == date_str:
            return False
        await r.set(_LAST_RUN_DATE_KEY, date_str)
        return True
    except Exception:
        logger.warning(
            "nightly run-date ledger unavailable — running unguarded", exc_info=True
        )
        return True


async def _needs_catch_up(now: datetime) -> bool:
    """True when today's anchor already passed with no run recorded for it.

    Fail-closed on Redis errors (unlike the claim): with an unknown ledger the
    conservative move at boot is to stay quiet and let the scheduled run fire.
    """
    if not _anchor_passed(now):
        return False
    try:
        last = await get_redis().get(_LAST_RUN_DATE_KEY)
    except Exception:
        logger.warning("nightly catch-up check: ledger unreadable", exc_info=True)
        return False
    return last != _anchor_date(now)


async def run_nightly_jobs(*, once_per_day: bool = False) -> None:
    """Run each nightly job in isolation.

    ``once_per_day`` (R3): claim today's anchor date in the Redis ledger first
    and skip the batch entirely if it was already claimed — this is what makes
    a restart-triggered catch-up safe. Default False keeps direct/manual calls
    (ops scripts, tests) behaving exactly as before.
    """
    if once_per_day:
        _date = _anchor_date(now_real())
        if not await _claim_run_date(_date):
            logger.info("nightly jobs already ran for anchor %s — skipping", _date)
            return
    # S1-3 opinion drift — MUST run before digest: the same night's digest
    # opinion_line has to reflect the post-drift variance (KICKOFF S1-3 §7
    # ordering hard requirement). Keep this block immediately above the digest
    # block; do not append it to the end of the job list.
    try:
        from app.config import settings as _opinion_settings
        if _opinion_settings.polis_opinion_enabled:
            from app.services.opinion_service import OpinionService
            async with async_session() as db:
                n = await OpinionService(db).drift()
            if n:
                logger.info("S1-3: opinion drift moved %d stances", n)
    except Exception:
        logger.error("S1-3 opinion drift failed", exc_info=True)

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

    # M2: advance story arcs (rule-based milestone engine).
    try:
        from app.services.arc_service import evaluate_arcs
        async with async_session() as db:
            n = await evaluate_arcs(db)
        if n:
            logger.info("Advanced %d story-arc milestones", n)
    except Exception:
        logger.error("Arc engine failed", exc_info=True)

    # M3: close due civic polls and execute the winning outcome.
    try:
        from app.services.civic_service import close_due_polls
        async with async_session() as db:
            n = await close_due_polls(db)
        if n:
            logger.info("Closed %d civic polls", n)
    except Exception:
        logger.error("Civic poll close failed", exc_info=True)

    # F2 civic promotion — MUST sit between the poll close above and the NPC
    # vote below, and above the two poll-OPENING blocks that follow. Two
    # separate ordering constraints, neither is style:
    #   1. after close_due_polls — tonight's promotions must not move the
    #      denominator of a poll that is being tallied this very night;
    #   2. before the opening blocks + run_npc_voting — a resident promoted
    #      tonight is inside `_eligible_at_open` for the polls opened tonight
    #      and votes on them tonight, so numerator and denominator come from
    #      the same electorate. Appending this to the end of the job list does
    #      not "slightly reorder" things: it enters the promoted resident into
    #      night N+1's quorum denominator with zero ballots cast, silently
    #      raising the bar every poll has to clear that night.
    # Keep this block immediately below the poll-close block; do not move it.
    # `run_promotion_pass` gates on CIVIC_PROMOTION_MODE internally and returns
    # immediately (zero reads, zero writes) in the default `off` state, so
    # wiring it is a byte-for-byte no-op until the flag is flipped separately.
    try:
        from app.tasks.civic_promotion import run_promotion_pass
        async with async_session() as db:
            summary = await run_promotion_pass(db)
        if summary.get("mode") != "off":
            logger.info("F2 civic promotion pass: %s", summary)
    except Exception:
        logger.error("civic promotion pass failed", exc_info=True)

    # M5: open the standing building proposals (idempotent one-shot per topic —
    # an existing world picks them up here without a re-seed).
    try:
        from app.services.civic_service import seed_civic_agenda
        async with async_session() as db:
            n = await seed_civic_agenda(db)
        if n:
            logger.info("Opened %d civic building proposals", n)
    except Exception:
        logger.error("Civic agenda seeding failed", exc_info=True)

    # M6: seasonal mayor election — once per active season, else every
    # election_interval_days; never while an election poll is already open.
    try:
        from app.services.election_service import maybe_open_seasonal_election
        async with async_session() as db:
            poll = await maybe_open_seasonal_election(db)
        if poll is not None:
            logger.info("Opened mayor election poll %s", poll.id)
    except Exception:
        logger.error("Mayor election opening failed", exc_info=True)

    # M3: NPC residents cast their (rule-based) votes on open civic polls.
    try:
        from app.services.civic_service import run_npc_voting
        async with async_session() as db:
            n = await run_npc_voting(db)
        if n:
            logger.info("%d NPC civic votes cast", n)
    except Exception:
        logger.error("NPC civic voting failed", exc_info=True)

    # S2-1: office term expiry — vacate offices whose term_ends_at has passed
    # (realism-family pattern: gate INSIDE the cron, own try/except, fail-open;
    # skipped entirely while polis_office_enabled is False).
    try:
        from app.config import settings as _office_settings
        if _office_settings.polis_office_enabled:
            from app.services.office_service import OfficeService
            async with async_session() as db:
                n = await OfficeService(db).term_check()
            if n:
                logger.info("office term_check vacated %d", n)
    except Exception:
        logger.error("office term_check failed", exc_info=True)

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

    # A1: weekly life-goal evaluation — once per WORLD week (agent-T §5). Uses
    # the world-week gate so it fires when the world week advances, not on a
    # real weekday (a world week is only ~1.75 real days).
    if await _world_week_gate(_GOAL_WEEK_KEY):
        try:
            await run_weekly_goal_eval()
        except Exception:
            logger.error("Weekly goal eval failed", exc_info=True)

    # Realism P0-2: soft-archive stale, low-importance event memories so the
    # world forgets old small talk (score-floor eviction). Gated on realism.
    from app.config import settings
    if settings.realism_enabled:
        try:
            await run_memory_eviction()
        except Exception:
            logger.error("Realism memory eviction failed", exc_info=True)

        # Realism P0-5b: reclaim proposals stuck in `approved` (crash between the
        # approve-commit and the applied-commit) and orphaned Lab budget
        # reservations left by runs that reached a terminal state.
        try:
            from app.services.proposal_service import reclaim_stuck_proposals
            async with async_session() as db:
                n = await reclaim_stuck_proposals(db)
            if n:
                logger.info("Realism: reclaimed %d stuck approved proposals", n)
        except Exception:
            logger.error("Realism proposal reclaim failed", exc_info=True)
        try:
            n = await sweep_orphan_lab_reservations()
            if n:
                logger.info("Realism: released %d orphan lab reservations", n)
        except Exception:
            logger.error("Realism lab reservation sweep failed", exc_info=True)

    # Realism P2-1: weekly relationship decay (INDEPENDENT gate). Ties idle for
    # 30 days lose familiarity (×0.95/week) and drift affinity toward 0
    # (×0.98/week). Run once per WORLD week (agent-T §5) via the world-week gate
    # so the daily real cron doesn't over-decay — the rate is per-week and a
    # world week is only ~1.75 real days.
    from app.config import settings as _rel_settings
    if _rel_settings.realism_relations_enabled and await _world_week_gate(_DECAY_WEEK_KEY):
        try:
            from app.services import relation_service
            async with async_session() as db:
                n = await relation_service.decay(db)
            if n:
                logger.info("Realism P2: decayed %d idle relations", n)
        except Exception:
            logger.error("Realism relation decay failed", exc_info=True)

    # Realism P2-4: nightly circle detection (independent gate, runs daily).
    # Connected components over strong ties → meta_json.circle_id + snapshot.
    if _rel_settings.realism_relations_enabled:
        try:
            from app.services import circle_service
            async with async_session() as db:
                snap = await circle_service.refresh_circles(db)
            if snap.get("count"):
                logger.info("Realism P2: detected %d social circles", snap["count"])
        except Exception:
            logger.error("Realism circle detection failed", exc_info=True)

    # S1-1: aggregate public reputation from gossip evidence and mood.
    # Independent default-off gate; one fail-open block like the other social
    # foundation jobs, with no LLM calls and no schema writes.
    try:
        from app.config import settings as _rep_settings
        if _rep_settings.rep_enabled:
            from app.services.reputation_service import recompute
            async with async_session() as db:
                n = await recompute(db)
            if n:
                logger.info("S1-1: reputation recomputed for %d residents", n)
    except Exception:
        logger.error("S1-1 reputation recompute failed", exc_info=True)

    # S1-5: town public spending / treasury reconciliation (realism-family
    # pattern: gate INSIDE the cron, own try/except, fail-open; skipped whole
    # while town_treasury_enabled is False, so a disabled world touches no DB).
    # Appended after the existing governance blocks — none of them are moved.
    try:
        from app.config import settings as _town_settings
        if _town_settings.town_treasury_enabled:
            from app.services.treasury_service import run_public_spending
            async with async_session() as db:
                spent = await run_public_spending(db)
            if spent:
                logger.info("S1-5: town public spending disbursed %d SC", spent)
    except Exception:
        logger.error("S1-5 town treasury nightly failed", exc_info=True)

    # M-A: NPC↔NPC trade night (same realism-family shape: gate INSIDE the cron,
    # own try/except, fail-open). Order is a hard requirement, not style:
    # settle → accept → consume. Settling first keeps a commission accepted
    # tonight from being completed the same night; consuming last lets a reward
    # that just landed be spent without waiting a whole night. Both gates are
    # read before the session opens — a disabled world touches no DB.
    try:
        from app.config import settings as _trade_settings
        if _trade_settings.npc_economy_enabled and _trade_settings.npc_trade_enabled:
            from app.services.npc_trade_service import (
                run_commission_accept_pass, run_commission_settle_pass,
                run_consumption_pass,
            )
            async with async_session() as db:
                settled = await run_commission_settle_pass(db)
                accepted = await run_commission_accept_pass(db)
                bought = await run_consumption_pass(db)
            if settled["settled"] or accepted["accepted"] or bought["bought"]:
                logger.info(
                    "M-A: %d commissions settled (%d SC paid, %d reopened), "
                    "%d accepted, %d purchases for %d SC (tax %d)",
                    settled["settled"], settled["paid"], settled["reopened"],
                    accepted["accepted"], bought["bought"], bought["spent"],
                    bought["tax"])
    except Exception:
        logger.error("M-A npc trade nightly failed", exc_info=True)
    # Future: E2 dreams, E7 capsule delivery — each own try/except.


async def run_memory_eviction() -> int:
    """Realism P0-2: per-resident soft-archive of stale low-importance events."""
    from sqlalchemy import select
    from app.memory.service import MemoryService
    from app.models.resident import Resident
    total = 0
    async with async_session() as db:
        rids = (await db.execute(select(Resident.id))).scalars().all()
        svc = MemoryService(db)
        for rid in rids:
            total += await svc.evict_memories(rid)
    if total:
        logger.info("Realism: archived %d stale event memories", total)
    return total


_RESERVED_BUDGET_COLS = (
    "reserved_model_tokens", "reserved_tool_calls", "reserved_wall_clock_ms",
    "reserved_egress_requests", "reserved_egress_bytes", "reserved_artifact_count",
    "reserved_artifact_bytes", "reserved_active_workers",
)


async def sweep_orphan_lab_reservations() -> int:
    """Realism P0-5b: release budget reservations left non-zero on runs that
    already reached a terminal state (the docstring-acknowledged orphan in
    app/lab/budgets.py). Read of the Lab models from the tasks layer — no
    app/lab code touched."""
    from sqlalchemy import select
    from app.models.lab_run import LabRun
    from app.models.lab_budget import LabRunBudget
    _TERMINAL = ("succeeded", "failed", "cancelled")
    released = 0
    async with async_session() as db:
        rows = (await db.execute(
            select(LabRunBudget)
            .join(LabRun, LabRun.id == LabRunBudget.run_id)
            .where(LabRun.status.in_(_TERMINAL))
        )).scalars().all()
        for b in rows:
            changed = False
            for col in _RESERVED_BUDGET_COLS:
                if (getattr(b, col, 0) or 0) > 0:
                    setattr(b, col, 0)
                    changed = True
            if changed:
                released += 1
        if released:
            await db.commit()
    return released


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
    from app.services.lab_task_service import (
        LabTaskError,
        _sync_codex_terminal_cost,
        fail_task,
    )

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
            run_id = run.id
            task_id = run.task_id
            task = await db.get(LabTask, task_id)
            if run.adapter == "codex":
                if task is None:
                    usage_error: Exception | None = RuntimeError("orphan task is missing")
                else:
                    try:
                        await _sync_codex_terminal_cost(db, task)
                    except LabTaskError as exc:
                        usage_error = exc
                    else:
                        usage_error = None
                if usage_error is not None:
                    await db.rollback()
                    run = await db.get(LabRun, run_id)
                    task = await db.get(LabTask, task_id)
                    if run is not None:
                        run.status = "failed"
                        run.ended_at = datetime.now(UTC)
                        run.error = "cost_unknown: orphaned run usage unavailable"
                        await db.commit()
                    from app.lab import telemetry
                    telemetry.emit_alert(
                        telemetry.LabAlert.ORPHAN_HEARTBEAT,
                        run_id=run_id,
                        reason="cost_unknown",
                    )
                    logger.error(
                        "orphan Codex usage unavailable for run %s; refund blocked",
                        run_id,
                        exc_info=(
                            type(usage_error),
                            usage_error,
                            usage_error.__traceback__,
                        ),
                    )
                    n += 1
                    continue
                run = await db.get(LabRun, run_id)
                task = await db.get(LabTask, task_id)
                if run is None:
                    continue
            run.status = "failed"
            run.ended_at = datetime.now(UTC)
            run.error = "orphaned: heartbeat stale"
            await db.commit()
            from app.lab import telemetry
            telemetry.emit_alert(
                telemetry.LabAlert.ORPHAN_HEARTBEAT, run_id=run.id, reason="heartbeat_stale",
            )
            task = task or await db.get(LabTask, task_id)
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
    """Sleep until the next Beijing-morning anchor and run the nightly jobs, forever.

    World time (agent-T §5): the cadence stays a true real 24h, but the anchor is
    ``RUN_HOUR``:00 Beijing time (via ``now_real`` in Asia/Shanghai) instead of
    UTC 00:30, so the digest is ready in the Beijing morning.

    R3 (eng-health A): before entering the wait, check the Redis run-date ledger
    — if today's anchor already passed and nothing ran for it (crash, container
    restart, deploy window over 07:00), catch up immediately instead of losing
    the whole day's batch silently. Every run goes through the ``once_per_day``
    guard, so a same-day restart never double-runs.
    """
    try:
        _boot = now_real()
        if await _needs_catch_up(_boot):
            logger.warning(
                "nightly: anchor %02d:%02d for %s already passed with no recorded "
                "run (restart/downtime) — catching up now",
                RUN_HOUR, RUN_MINUTE, _anchor_date(_boot),
            )
            await run_nightly_jobs(once_per_day=True)
    except Exception:
        logger.error("nightly catch-up check failed", exc_info=True)
    while True:
        await beat("nightly")  # P2: liveness signal + sibling-loop watchdog
        await asyncio.sleep(_seconds_until_next_run(now_real()))
        await run_nightly_jobs(once_per_day=True)
