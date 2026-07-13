"""World event cron (S1): flip is_active on schedule and broadcast transitions.

Mirrors heat_cron: runs only where run_background_tasks is true (single instance
— the agent-worker in split mode), and broadcasts via manager, which fans out to
every API worker's sockets over Redis pub/sub.
"""

import asyncio
import logging

from app.database import async_session
from app.services.world_event_service import flip_active_events, write_collective_memories
from app.ws.manager import manager

logger = logging.getLogger(__name__)
EVENT_CRON_INTERVAL_SECONDS = 60


async def event_cron_loop():
    """Background task: every 60s flip active world events and broadcast."""
    while True:
        try:
            async with async_session() as db:
                # E6: keep a weather segment scheduled. Created inactive with
                # starts_at=now, so the flip below activates + broadcasts it in
                # this same pass through the S1 pipeline (A2 template pattern).
                try:
                    from app.tasks.weather import ensure_weather_event
                    scheduled = await ensure_weather_event(db)
                    if scheduled is not None:
                        logger.info("Event cron: scheduled weather '%s'", scheduled.title)
                except Exception:
                    logger.warning("E6 weather step failed", exc_info=True)
                changes = await flip_active_events(db)
                # A2: on start, give active residents a shared collective memory.
                for event, phase in changes:
                    if phase == "start":
                        try:
                            await write_collective_memories(db, event)
                        except Exception:
                            logger.warning("collective memory write failed", exc_info=True)
                # C3: fire due script acts + settle finished seasons.
                try:
                    from app.services.script_service import fire_due_scripts, settle_due_seasons
                    fired = await fire_due_scripts(db)
                    settled = await settle_due_seasons(db)
                    if fired:
                        logger.info("Event cron: fired %d script act(s)", len(fired))
                    if settled:
                        logger.info("Event cron: settled %d season(s)", len(settled))
                except Exception:
                    logger.warning("C3 script/season cron step failed", exc_info=True)
            for event, phase in changes:
                await manager.broadcast({
                    "type": "world_event",
                    "event": event,
                    "phase": phase,
                })
            if changes:
                logger.info("Event cron: %d world-event transitions", len(changes))
        except Exception as e:
            logger.error(f"Event cron error: {e}", exc_info=True)
        await asyncio.sleep(EVENT_CRON_INTERVAL_SECONDS)
