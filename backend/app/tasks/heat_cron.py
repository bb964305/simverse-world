import asyncio
import logging
from app.database import async_session
from app.services.heat_service import recalculate_heat
from app.ws.manager import manager

logger = logging.getLogger(__name__)
HEAT_CRON_INTERVAL_SECONDS = 3600  # 1 hour


async def heat_cron_loop():
    """Background task: recalculate heat hourly, broadcast status changes."""
    while True:
        try:
            async with async_session() as db:
                changes = await recalculate_heat(db)
                # E1: regress moods toward neutral hourly (~48h back to calm).
                try:
                    from app.services.mood_service import decay_all
                    await decay_all(db)
                except Exception:
                    logger.warning("mood decay failed", exc_info=True)
            for change in changes:
                await manager.broadcast({
                    "type": "resident_status",
                    "resident_slug": change["slug"],
                    "status": change["new_status"],
                    "heat": change["heat"],
                    "mood_label": change.get("mood_label", "calm"),
                })
            if changes:
                logger.info(f"Heat cron: {len(changes)} status changes")
        except Exception as e:
            logger.error(f"Heat cron error: {e}", exc_info=True)
        await asyncio.sleep(HEAT_CRON_INTERVAL_SECONDS)
