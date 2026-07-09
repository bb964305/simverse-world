"""BasicPerceivePlugin: find nearby residents within radius."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.agent.schemas import TickContext
from app.models.resident import Resident

logger = logging.getLogger(__name__)


class BasicPerceivePlugin:
    def __init__(self, params: dict[str, Any] | None = None):
        params = params or {}
        self.radius: int = params.get("radius", 10)

    async def execute(self, ctx: TickContext) -> TickContext:
        try:
            result = await ctx.db.execute(
                select(Resident).where(Resident.id != ctx.resident.id)
            )
            all_residents = result.scalars().all()

            nearby = []
            for r in all_residents:
                dist = abs(r.tile_x - ctx.resident.tile_x) + abs(r.tile_y - ctx.resident.tile_y)
                if dist <= self.radius:
                    nearby.append(r)

            ctx.nearby_residents = nearby
        except Exception as e:
            logger.warning("Perceive failed for %s: %s", ctx.resident.slug, e)
            ctx.skip_remaining = True

        # S1: active world events (60s-cached, fail-open) for the decision prompt.
        try:
            from app.services.world_event_service import get_active_events_cached
            ctx.world_events = await get_active_events_cached(ctx.db)
        except Exception:
            ctx.world_events = []

        return ctx
