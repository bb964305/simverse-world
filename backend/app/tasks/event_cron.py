"""World event cron (S1): flip is_active on schedule and broadcast transitions.

Mirrors heat_cron: runs only where run_background_tasks is true (single instance
— the agent-worker in split mode), and broadcasts via manager, which fans out to
every API worker's sockets over Redis pub/sub.
"""

import asyncio
import logging

from app.database import async_session
from app.services.world_event_service import flip_active_events
from app.ws.manager import manager

logger = logging.getLogger(__name__)
EVENT_CRON_INTERVAL_SECONDS = 60


async def event_cron_loop():
    """Background task: every 60s flip active world events and broadcast."""
    while True:
        try:
            async with async_session() as db:
                changes = await flip_active_events(db)
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
