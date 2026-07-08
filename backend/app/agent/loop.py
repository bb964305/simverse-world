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
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.actions import ActionType, ActionResult
from app.agent.chat import resident_chat
from app.agent.registry import registry
from app.agent.scheduler import build_schedule, should_tick
from app.agent.tick import resident_tick
from app.config import settings
from app.database import async_session
from app.llm.budget import BudgetTier, background_tier
from app.models.resident import Resident
from app.ws.manager import manager

logger = logging.getLogger(__name__)


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
            try:
                tier = await self._tick_round()
            except Exception as e:
                logger.error("AgentLoop tick_round error: %s", e, exc_info=True)
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
        # Load id + schedule data for active residents, then release the session
        async with async_session() as db:
            tier = await background_tier(db)
            if tier == BudgetTier.PLAYER_ONLY:
                # Budget exhausted: pause all background work; player-visible
                # calls (WS chat) keep running on their own path.
                return tier
            result = await db.execute(
                select(Resident.id, Resident.meta_json).where(
                    Resident.status.not_in(["sleeping"])
                )
            )
            rows = result.all()
        if not rows:
            return tier

        force_plan_only = tier == BudgetTier.RULE_ONLY
        suppress_chat = tier == BudgetTier.RULE_ONLY
        current_hour = datetime.now().hour
        semaphore = asyncio.Semaphore(settings.agent_max_concurrent)

        async def guarded_tick(
            resident_id: str, meta_json: dict | None
        ) -> ActionResult | None:
            """Run one resident's tick in its own session, bounded by semaphore."""
            # Evaluate schedule before acquiring semaphore (no DB needed)
            sbti_data = (meta_json or {}).get("sbti")
            schedule = build_schedule(sbti_data)

            if not should_tick(schedule, current_hour):
                return None

            async with semaphore:
                async with async_session() as db:
                    resident = await db.get(Resident, resident_id)
                    if resident is None or resident.status == "sleeping":
                        return None
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
            *(guarded_tick(row.id, row.meta_json) for row in rows),
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
            select(Resident).where(Resident.slug == target_slug)
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
