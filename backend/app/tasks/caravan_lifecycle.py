"""Independent durable caravan driver; disabled gate still emits liveness."""
from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.database import async_session
from app.services.caravan_lifecycle_service import (
    drive_due_visits,
    reconcile_market_events,
    worker_owner,
)
from app.tasks.loop_heartbeat import beat
from app.ws.manager import manager

logger = logging.getLogger(__name__)


async def caravan_lifecycle_loop() -> None:
    owner = worker_owner()
    while True:
        try:
            if settings.caravan_lifecycle_enabled:
                async with async_session() as db:
                    await reconcile_market_events(db)
                    snapshots = await drive_due_visits(db, owner=owner)
                for snapshot in snapshots:
                    await manager.broadcast(snapshot)
                if snapshots:
                    logger.info("caravan lifecycle advanced %d step(s)", len(snapshots))
        except Exception:
            logger.exception("caravan lifecycle round failed")
        await beat("caravan")
        await asyncio.sleep(settings.caravan_lifecycle_interval_seconds)
