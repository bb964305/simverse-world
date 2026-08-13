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
        if settings.realism_plan_continuity_enabled and ctx.continuation_trip:
            return await super().execute(ctx)

        distracted = False
        if settings.realism_plan_continuity_enabled and ctx.current_plan:
            if ctx.resident.status != "walking":
                from app.agent.plan_continuity import claim_slot_interrupt
                claimed = await claim_slot_interrupt(
                    ctx.resident.id, ctx.plan_date, ctx.current_plan.slot, "spontaneous")
                distracted = bool(claimed and random.random() < self.distraction_chance)
        elif ctx.current_plan:
            distracted = random.random() < self.distraction_chance

        if ctx.current_plan and distracted:
            logger.debug("Spontaneous %s ignoring plan (distraction)", ctx.resident.slug)
            ctx.plan_interrupt_reason = "spontaneous"
            ctx.current_plan = None

        social_ready = self.social_eagerness and ctx.nearby_residents
        if settings.realism_plan_continuity_enabled:
            from app.agent.needs import get_needs
            social_ready = bool(
                social_ready and ctx.resident.status != "walking"
                and get_needs(ctx.resident).get("social", 1.0) <= 0.5
            )
        if social_ready:
            idle_nearby = [r for r in ctx.nearby_residents
                          if r.status in ("idle", "walking")
                          and getattr(r, "is_autonomous", True)]
            eager = False
            if idle_nearby and settings.realism_plan_continuity_enabled:
                plan = ctx.scheduled_plan
                if plan is not None:
                    from app.agent.plan_continuity import claim_slot_interrupt
                    eager = bool(await claim_slot_interrupt(
                        ctx.resident.id, ctx.plan_date, plan.slot, "social_eager")
                        and random.random() < 0.4)
            elif idle_nearby:
                eager = random.random() < 0.4
            if idle_nearby and eager:
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
                ctx.plan_interrupt_reason = "social_eager"
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
