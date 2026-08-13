"""AgentLoop: centralized background task driving all resident autonomous behavior."""
# New WebSocket message types emitted by the AgentLoop:
#
# resident_move:
#   { "type": "resident_move", "resident_slug": str, "tile_x": int, "tile_y": int,
#     "target_tile": [x, y] | null, "status": "walking" }
#
# resident_chat:
#   { "type": "resident_chat", "initiator_slug": str, "target_slug": str, "summary": null }
#
# resident_chat_end:
#   { "type": "resident_chat_end", "initiator_slug": str, "target_slug": str,
#     "summary": str, "mood": "positive"|"neutral"|"negative" }
#
# resident_status:
#   { "type": "resident_status", "resident_slug": str, "status": str }
import asyncio
import json
import logging
import time
from datetime import datetime, UTC

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.actions import ActionType, ActionResult
from app.agent.chat import resident_chat
from app.agent.night_homing import night_homing_step
from app.agent.registry import registry
from app.agent.scheduler import build_schedule, get_activity_probability, should_tick
from app.agent.tick import resident_tick
from app.observability import observe_tick_round
from app.config import settings
from app.database import async_session
from app.llm.budget import BudgetTier, background_tier
from app.llm.budget_alerts import maybe_check_usage_stall
from app.models.resident import Resident
from app.tasks.loop_heartbeat import beat
from app.ws.manager import manager

logger = logging.getLogger(__name__)
# One JSON line per acted tick (see _handle_action) — dedicated name so ops can
# route/filter behavior replay separately from app logs.
events_logger = logging.getLogger("agent.events")

# Realism P1-10: energy that lets a sleeping resident wake.
_WAKE_ENERGY = 0.5


async def _metabolize_sleepers(current_hour: int, current_weekday: int) -> int:
    """Recover sleeping residents' energy and wake those rested + in-window.

    Sleeping residents are excluded from the main tick round, so their energy is
    metabolized here (own session). Returns the number woken."""
    from app.agent.needs import get_needs, metabolize, write_needs
    woke = 0
    async with async_session() as db:
        sleepers = (await db.execute(
            select(Resident).where(
                Resident.is_autonomous,
                Resident.status == "sleeping",
            )
        )).scalars().all()
        for r in sleepers:
            sbti = (r.meta_json or {}).get("sbti")
            needs = metabolize(get_needs(r), status="sleeping", sbti=sbti)
            write_needs(r, needs)
            sched = build_schedule(sbti, weekday=current_weekday)
            in_window = sched.wake_hour <= current_hour < sched.sleep_hour
            if needs["energy"] >= _WAKE_ENERGY and in_window:
                r.status = "idle"
                woke += 1
        if sleepers:
            await db.commit()
    return woke


class AgentLoop:
    """Centralized agent loop — runs as a FastAPI background task.

    Follows the same pattern as heat_cron_loop: while True, try, sleep.
    Differences:
    - Evaluates per-resident schedules (SBTI-derived) before ticking
    - Uses asyncio.Semaphore to bound concurrent ticks
    - Dispatches resident_chat() for CHAT_RESIDENT actions
    - Broadcasts movement and status changes to all connected clients
    """

    async def run(self) -> None:
        """Main loop — runs indefinitely."""
        registry.load_all()
        logger.info("AgentLoop started (interval=%ds, %d agent configs loaded)",
                     settings.agent_tick_interval, len(registry._configs))
        while True:
            if not settings.agent_enabled:
                await asyncio.sleep(settings.agent_tick_interval)
                continue
            tier = BudgetTier.NORMAL
            t0 = time.perf_counter()
            try:
                tier = await self._tick_round()
            except Exception as e:
                logger.error("AgentLoop tick_round error: %s", e, exc_info=True)
            observe_tick_round(time.perf_counter() - t0)
            # Roadmap #6: budget-breaker silent-failure watchdog. Cheap no-op
            # unless armed (env BUDGET_USAGE_STALL_MIN>0 + metering on);
            # alerts when llm_usage gets zero new rows for the whole window
            # while this loop keeps running. Never raises.
            await maybe_check_usage_stall()
            # P2: liveness signal + sibling-loop watchdog (Roadmap #5).
            await beat("agent")
            # Budget throttle (E-24, ≥80%/≥95%): halve background frequency.
            sleep_mult = 2 if tier in (BudgetTier.THROTTLE, BudgetTier.RULE_ONLY) else 1
            await asyncio.sleep(settings.agent_tick_interval * sleep_mult)

    async def _tick_round(self) -> BudgetTier:
        """One round: evaluate schedules, run concurrent resident ticks.

        AsyncSession is not concurrency-safe (P0-1): a short-lived session
        loads the resident list, then each guarded tick opens its own
        session inside the semaphore.

        Returns the current budget tier so the caller can throttle its sleep.
        Budget degradation (E-24): PLAYER_ONLY (≥100%) pauses the whole round;
        RULE_ONLY (≥95%) forces plan-only decides and suppresses inter-resident
        chat initiation.
        """
        # Load id + schedule data for active autonomous residents, then release
        # the session.
        async with async_session() as db:
            tier = await background_tier(db)
            if tier == BudgetTier.PLAYER_ONLY:
                # Budget exhausted: pause all background work; player-visible
                # calls (WS chat) keep running on their own path.
                return tier
            result = await db.execute(
                select(Resident.id, Resident.meta_json, Resident.mood_json).where(
                    Resident.is_autonomous,
                    Resident.status.not_in(["sleeping"])
                )
            )
            rows = result.all()
            # E6: current weather fetched once per round (60s-cached) and
            # threaded into every schedule build below.
            try:
                from app.tasks.weather import get_current_weather
                weather = await get_current_weather(db)
            except Exception:
                weather = None
            # Realism P1-9: is a festival world event active this round?
            festival_active = False
            try:
                from app.services.world_event_service import get_active_events_cached
                festival_active = any(
                    e.get("type") == "festival" for e in await get_active_events_cached(db)
                )
            except Exception:
                festival_active = False
        if not rows:
            return tier

        force_plan_only = tier == BudgetTier.RULE_ONLY
        suppress_chat = tier == BudgetTier.RULE_ONLY
        # World time (agent-T): resident 作息 runs on the accelerated world clock,
        # not real wall-clock — a full day/night every 6 real hours at k=4.
        from app.world_clock import world_hour, world_weekday
        current_hour = world_hour()
        current_weekday = world_weekday()
        # Realism P1-10: sleeping residents (excluded from the tick round) recover
        # energy and wake within their schedule window once rested.
        if settings.realism_enabled:
            try:
                await _metabolize_sleepers(current_hour, current_weekday)
            except Exception:
                logger.warning("sleeper metabolism failed", exc_info=True)
        active_trip_ids: set[str] = set()
        if getattr(settings, "realism_plan_continuity_enabled", False):
            try:
                from app.agent.plan_continuity import active_trip_resident_ids
                active_trip_ids = await active_trip_resident_ids(
                    [row.id for row in rows])
            except Exception:
                # Existence is only a scheduling hint. Redis trouble falls back
                # to the ordinary random gate; resident_tick still owns full
                # trip validation once a tick is admitted.
                logger.warning("active plan trip batch check failed", exc_info=True)
        semaphore = asyncio.Semaphore(settings.agent_max_concurrent)

        async def guarded_tick(
            resident_id: str, meta_json: dict | None, mood_json: dict | None = None
        ) -> ActionResult | None:
            """Run one resident's tick in its own session, bounded by semaphore."""
            # Evaluate schedule before acquiring semaphore (no DB needed)
            sbti_data = (meta_json or {}).get("sbti")
            schedule = build_schedule(sbti_data, weather=weather, weekday=current_weekday)

            if get_activity_probability(schedule, current_hour) <= 0.0:
                # 作息门关闭：夜间归巢（零 LLM，一 tick 一步），不计日行动数
                # （burn-in 发现：sleep_hour 后居民冻结在最后位置"就地入睡"）
                async with semaphore:
                    async with async_session() as db:
                        resident = await db.get(Resident, resident_id)
                        if (
                            resident is None
                            or not resident.is_autonomous
                            or resident.status in ("sleeping", "chatting", "socializing")
                        ):
                            return None
                        new_tile = await night_homing_step(db, resident)
                        if new_tile is not None:
                            await manager.broadcast({
                                "type": "resident_move",
                                "resident_slug": resident.slug,
                                "tile_x": resident.tile_x,
                                "tile_y": resident.tile_y,
                                "target_tile": None,
                                "status": "walking",
                            })
                return None

            weather_kind = (weather or {}).get("kind")
            valence = (mood_json or {}).get("valence") if settings.realism_enabled else None
            # A started route must get consecutive opportunities to advance.
            # This bypasses only the stochastic gate: the explicit awake-window
            # check above and the sleeping re-check below remain authoritative.
            if (resident_id not in active_trip_ids
                    and not should_tick(
                        schedule, current_hour, weather_kind, festival_active, valence)):
                return None

            async with semaphore:
                async with async_session() as db:
                    resident = await db.get(Resident, resident_id)
                    if (
                        resident is None
                        or not resident.is_autonomous
                        or resident.status == "sleeping"
                    ):
                        return None
                    if (
                        settings.chat_engaged_tick_skip_enabled
                        and resident.status in ("chatting", "socializing")
                    ):
                        if resident.status == "socializing":
                            return None
                        # chatting: only skip while the Redis chat lock is
                        # held. A missing lock means the chat ended without a
                        # status reset (stale) — self-heal to idle and tick
                        # normally so needs/plans/memories keep flowing.
                        if await manager.resident_lock_owner(resident_id) is not None:
                            return None
                        resident.status = "idle"
                        await db.commit()
                    try:
                        # Pass force_plan_only only when set, so patched ticks
                        # with a (db, resident) signature stay compatible.
                        tick_kwargs = {"force_plan_only": True} if force_plan_only else {}
                        action_result = await resident_tick(db, resident, **tick_kwargs)
                    except Exception as e:
                        logger.warning("Tick error for %s: %s", resident.slug, e)
                        return None

                    if action_result:
                        await self._handle_action(db, resident, action_result, suppress_chat=suppress_chat)

                    return action_result

        # Run all ticks concurrently, bounded by semaphore
        await asyncio.gather(
            *(guarded_tick(row.id, row.meta_json, row.mood_json) for row in rows),
            return_exceptions=True,
        )
        return tier

    async def _handle_action(
        self,
        db: AsyncSession,
        resident: Resident,
        action_result: ActionResult,
        *,
        suppress_chat: bool = False,
    ) -> None:
        """Post-tick: broadcast state changes and handle chat initiation.

        ``suppress_chat`` (budget RULE_ONLY tier) pauses inter-resident chat
        initiation — a planned CHAT would otherwise fire an 11–13 call wrap-up.
        """
        # Structured behavior log (PLAN_P3 批次 3, "agent_events" 最小落法):
        # one JSON line per acted tick at the single post-tick chokepoint.
        # Answers "为什么居民做了这件事" without a new table — llm_usage rows
        # and WS broadcasts already cover cost and visual replay; grep/ship
        # this logger ("agent.events") for behavior replay.
        try:
            events_logger.info(json.dumps({
                "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                "resident": resident.slug,
                "action": action_result.action.value,
                "target_slug": action_result.target_slug,
                "target_tile": list(action_result.target_tile) if action_result.target_tile else None,
                "reason": action_result.reason,
                "suppress_chat": suppress_chat,
            }, ensure_ascii=False))
        except Exception:  # never let telemetry break the tick
            pass

        movement_actions = {ActionType.WANDER, ActionType.GO_HOME, ActionType.VISIT_DISTRICT}

        if action_result.action in movement_actions:
            await manager.broadcast({
                "type": "resident_move",
                "resident_slug": resident.slug,
                "tile_x": resident.tile_x,
                "tile_y": resident.tile_y,
                "target_tile": list(action_result.target_tile) if action_result.target_tile else None,
                "status": "walking",
            })
            # A durable market assignment turns into a purchase only after the
            # resident has genuinely reached its unique slot.  The service owns
            # idempotency, wallet/stock CAS and rollback; this broadcast is only
            # the committed playback frame returned by that authority.
            try:
                from app.services.caravan_market_service import maybe_purchase_for_resident

                purchase = await maybe_purchase_for_resident(db, resident)
                if purchase is not None:
                    await manager.broadcast(purchase)
            except Exception:
                logger.warning(
                    "market purchase hook failed for %s", resident.slug,
                    exc_info=True,
                )

        elif action_result.action == ActionType.CHAT_RESIDENT:
            if suppress_chat:
                logger.debug("Budget RULE_ONLY: skipping chat initiation for %s", resident.slug)
            else:
                await self._initiate_chat(db, resident, action_result.target_slug)

        elif action_result.action in {ActionType.IDLE, ActionType.NAP}:
            await manager.broadcast({
                "type": "resident_status",
                "resident_slug": resident.slug,
                "status": resident.status,
                "mood_label": resident.mood_label,
            })

    async def _initiate_chat(
        self,
        db: AsyncSession,
        initiator: Resident,
        target_slug: str | None,
    ) -> None:
        """Fetch target resident and run inter-resident chat."""
        if not target_slug:
            return

        result = await db.execute(
            select(Resident).where(
                Resident.is_autonomous,
                Resident.slug == target_slug,
            )
        )
        target = result.scalar_one_or_none()
        if target is None:
            return

        # Broadcast chat start
        await manager.broadcast({
            "type": "resident_chat",
            "initiator_slug": initiator.slug,
            "target_slug": target.slug,
            "summary": None,  # Will be updated when chat ends
        })

        try:
            chat_result = await resident_chat(db, initiator, target)

            if chat_result and not chat_result.get("skipped"):
                await manager.broadcast({
                    "type": "resident_chat_end",
                    "initiator_slug": initiator.slug,
                    "target_slug": target.slug,
                    "summary": chat_result.get("summary", ""),
                    "mood": chat_result.get("mood", "neutral"),
                })
        except Exception as e:
            logger.warning("Chat initiation failed %s->%s: %s", initiator.slug, target_slug, e)
            # Ensure both get unlocked
            initiator.status = "idle"
            target.status = "idle"
            await db.commit()
            await manager.broadcast({
                "type": "resident_chat_end",
                "initiator_slug": initiator.slug,
                "target_slug": target.slug,
                "summary": "",
            })


# Module-level singleton
agent_loop = AgentLoop()
