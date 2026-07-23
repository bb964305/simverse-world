"""SpontaneousDecidePlugin: extravert variant — easily distracted, social-eager."""
from __future__ import annotations

import logging
import random
from typing import Any

from app.agent.actions import ActionType, ActionResult, get_available_actions
from app.agent.phases.decide.basic import BasicDecidePlugin
from app.agent.schemas import TickContext
from app.config import settings

logger = logging.getLogger(__name__)


class SpontaneousDecidePlugin(BasicDecidePlugin):
    def __init__(self, params: dict[str, Any] | None = None):
        super().__init__(params)
        params = params or {}
        self.social_eagerness: bool = params.get("social_eagerness", True)
        self.distraction_chance: float = params.get("distraction_chance", 0.3)

    async def execute(self, ctx: TickContext) -> TickContext:
        if ctx.current_plan and random.random() < self.distraction_chance:
            logger.debug("Spontaneous %s ignoring plan (distraction)", ctx.resident.slug)
            ctx.current_plan = None

        if self.social_eagerness and ctx.nearby_residents:
            idle_nearby = [r for r in ctx.nearby_residents
                          if r.status in ("idle", "walking")]
            if idle_nearby and random.random() < 0.4:
                target = await self._pick_chat_target(ctx, idle_nearby)
                ctx.available_actions = get_available_actions(ctx.resident, ctx.nearby_residents)
                await self._load_memories(ctx)
                ctx.action_result = ActionResult(
                    action=ActionType.CHAT_RESIDENT,
                    target_slug=target.slug,
                    target_tile=None,
                    reason="想聊天",
                )
                ctx.plan_followed = False
                if ctx.current_plan:
                    ctx.current_plan.status = "interrupted"
                return ctx

        return await super().execute(ctx)

    async def _pick_chat_target(self, ctx: TickContext, idle_nearby, rng=random):
        """P2-3: weight the CHAT target by ``0.5 + familiarity + max(0, affinity)``
        (old friends and liked residents are likelier partners) with an ε uniform
        mix so strangers stay reachable (circles must not ossify — hard req).
        Uniform random when the relations gate is off (pre-P2 behavior).

        Relations are batched into ``ctx.relations`` once per tick — a single
        query, never one per candidate (perf red line)."""
        if not settings.realism_relations_enabled:
            return rng.choice(idle_nearby)
        from app.services import relation_service
        if ctx.relations is None:
            ctx.relations = await relation_service.relations_for(ctx.db, ctx.resident.id)
        rels = ctx.relations

        def weight(r):
            v = rels.get(r.id)
            if v is None:
                return 0.5
            return 0.5 + v.familiarity + max(0.0, v.affinity)

        return relation_service.weighted_pick(
            idle_nearby, weight, rng, epsilon=settings.realism_rel_chat_epsilon
        )
