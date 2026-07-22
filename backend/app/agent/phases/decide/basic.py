"""BasicDecidePlugin: decide next action, plan-aware with hybrid execution."""
from __future__ import annotations

import logging
import random
from datetime import datetime
from typing import Any

from app.agent.actions import ActionType, ActionResult, get_available_actions
from app.agent.prompts import build_decision_prompt
from app.agent.schemas import TickContext, parse_action_result
from app.config import settings
from app.llm.client import chat as llm_chat
from app.llm.metering import Meter
from app.memory.service import MemoryService

logger = logging.getLogger(__name__)

# Movement actions whose target tile is resolved server-side (realism P0-1).
# GO_HOME is excluded — execute resolves the home entrance itself.
_MOVEMENT_ACTIONS = {ActionType.WANDER, ActionType.VISIT_DISTRICT}


def _weather_kind(world_events) -> str | None:
    for e in world_events or []:
        if e.get("type") == "weather":
            return (e.get("payload_json") or {}).get("kind")
    return None


def _needs_prompt_hint(resident) -> str:
    """Realism P1-10: a one-line need summary softly injected into the decide
    prompt so the LLM's non-forced choices lean toward the resident's state."""
    from app.agent.needs import get_needs
    needs = get_needs(resident)
    parts = []
    if needs["satiety"] < 0.4:
        parts.append("有点饿了")
    if needs["energy"] < 0.4:
        parts.append("有些疲惫")
    if needs["social"] < 0.4:
        parts.append("想找人说说话")
    return f"\n（你现在{'，'.join(parts)}）" if parts else ""


class BasicDecidePlugin:
    def __init__(self, params: dict[str, Any] | None = None):
        params = params or {}
        self.interrupt_threshold: int = params.get("interrupt_threshold", 6)
        self.plan_adherence_hint: bool = params.get("plan_adherence_hint", True)
        # E-09/E-10 (largest cost lever,全服省 29–37%): with a fresh plan, execute
        # it rule-based (no decide LLM) unless a rule-level interrupt fires. Off by
        # default at the plugin level; enabled in the shipped agent YAML configs.
        self.skip_decide_when_planned: bool = params.get("skip_decide_when_planned", False)
        # Newest event memory at/above this importance (0–1 scale) counts as a
        # fresh notable event -> interrupt and re-decide with the LLM.
        self.interrupt_memory_importance: float = params.get("interrupt_memory_importance", 0.8)

    async def execute(self, ctx: TickContext) -> TickContext:
        ctx.available_actions = get_available_actions(ctx.resident, ctx.nearby_residents)
        await self._load_memories(ctx)

        plan = ctx.current_plan

        # Case 1: High-importance plan -> force execute
        if plan and plan.importance >= self.interrupt_threshold:
            result = self._force_execute_plan(plan, ctx)
            if result:
                ctx.action_result = result
                ctx.plan_followed = True
                plan.status = "executing"
                return ctx

        # Realism P1-10: needs arbitration — a critical need (<0.25) forces the
        # matching behavior (zero LLM). Below a high-importance plan, above the
        # weather/plan-skip paths.
        needs_action = self._maybe_needs_action(ctx)
        if needs_action is not None:
            ctx.action_result = needs_action
            ctx.plan_followed = False
            if plan:
                plan.status = "interrupted"
            return ctx

        # Realism P1-8: rule-level weather interrupt — duck out of rain/storm to
        # the nearest indoor place (zero LLM). Below a high-importance plan,
        # above the plan-skip fast path.
        shelter = self._maybe_shelter(ctx)
        if shelter is not None:
            ctx.action_result = shelter
            ctx.plan_followed = False
            if plan:
                plan.status = "interrupted"
            return ctx

        # Case 2 (E-09/E-10): plan-priority skip. Follow the plan without an LLM
        # call when nothing warrants reconsidering. force_plan_only (budget 95%+)
        # hard-disables interrupts — the breaker's rule-based fallback.
        if plan and (self.skip_decide_when_planned or ctx.force_plan_only):
            if ctx.force_plan_only or not self._should_interrupt(ctx):
                result = self._force_execute_plan(plan, ctx)
                if result:
                    ctx.action_result = result
                    ctx.plan_followed = True
                    plan.status = "executing"
                    return ctx
                # Plan not executable now (e.g. target left). Under a budget
                # crunch, skip the tick rather than spend on an LLM decide.
                if ctx.force_plan_only:
                    ctx.skip_remaining = True
                    return ctx

        # Case 3: no plan, an interrupt fired, or the plan was unexecutable -> LLM
        try:
            action_result = await self._llm_decide(ctx)
        except Exception as e:
            logger.warning("Decide LLM failed for %s: %s", ctx.resident.slug, e)
            ctx.skip_remaining = True
            return ctx

        if action_result is None:
            ctx.skip_remaining = True
            return ctx

        if action_result.action not in ctx.available_actions:
            logger.debug("Resident %s chose unavailable action %s", ctx.resident.slug, action_result.action)
            ctx.skip_remaining = True
            return ctx

        ctx.action_result = action_result

        if plan:
            try:
                planned_action = ActionType(plan.action)
                if action_result.action == planned_action:
                    ctx.plan_followed = True
                    plan.status = "executing"
                else:
                    ctx.plan_followed = False
                    plan.status = "interrupted"
            except ValueError:
                ctx.plan_followed = False

        return ctx

    def _should_interrupt(self, ctx: TickContext) -> bool:
        """Rule-level interrupt detection (E-09/E-10): should the fresh plan be
        overridden by an LLM decision? Uses only TickContext data — zero LLM.

        Two signals:
        - a fresh notable event: the newest event memory is high-importance
          (memories are loaded newest-first, so this approximates "just happened");
        - a social opportunity: a partner is available nearby (CHAT_RESIDENT is in
          available_actions) and the plan isn't already social.
        """
        if ctx.memories:
            newest = ctx.memories[0]
            importance = getattr(newest, "importance", None)
            if importance is not None and importance >= self.interrupt_memory_importance:
                return True

        plan = ctx.current_plan
        if ActionType.CHAT_RESIDENT in ctx.available_actions:
            if plan is None or plan.action != ActionType.CHAT_RESIDENT.value:
                return True

        return False

    def _maybe_needs_action(self, ctx: TickContext) -> ActionResult | None:
        """Realism P1-10: force a behavior when a need is critical. energy→GO_HOME
        (execute sleeps once home); satiety→EAT here or head to nearest dining.
        social is soft (CHAT weight / prompt), not a hard force — returns None."""
        if not settings.realism_enabled:
            return None
        from app.agent.needs import get_needs, most_critical
        crit = most_critical(get_needs(ctx.resident))
        if crit is None or crit == "social":
            return None
        from app.agent.map_data import (
            get_location_id_at, location_category, nearest_dining_location,
            get_valid_target_tile,
        )
        if crit == "energy":
            if ActionType.GO_HOME in ctx.available_actions:
                return ActionResult(ActionType.GO_HOME, None, None, "精力耗尽，回家休息")
            return None
        # satiety
        here = get_location_id_at(ctx.resident.tile_x, ctx.resident.tile_y)
        if location_category(here) == "dining" and ActionType.EAT in ctx.available_actions:
            return ActionResult(ActionType.EAT, here, None, "饿了，吃点东西")
        target = nearest_dining_location((ctx.resident.tile_x, ctx.resident.tile_y))
        if target and ActionType.VISIT_DISTRICT in ctx.available_actions:
            return ActionResult(
                ActionType.VISIT_DISTRICT, target, get_valid_target_tile(target), "去找吃的")
        return None

    def _maybe_shelter(self, ctx: TickContext) -> ActionResult | None:
        """Realism P1-8: in rain/storm, an outdoor resident reroutes to the
        nearest indoor location with probability realism_shelter_prob."""
        if not settings.realism_enabled:
            return None
        if _weather_kind(getattr(ctx, "world_events", None)) not in ("rain", "storm"):
            return None
        if ActionType.VISIT_DISTRICT not in ctx.available_actions:
            return None
        from app.agent.map_data import (
            get_location_id_at, location_is_indoor, nearest_indoor_location,
            get_valid_target_tile,
        )
        here = get_location_id_at(ctx.resident.tile_x, ctx.resident.tile_y)
        if here and location_is_indoor(here):
            return None  # already sheltered
        if random.random() >= settings.realism_shelter_prob:
            return None
        target_id = nearest_indoor_location((ctx.resident.tile_x, ctx.resident.tile_y))
        if not target_id:
            return None
        return ActionResult(
            action=ActionType.VISIT_DISTRICT, target_slug=target_id,
            target_tile=get_valid_target_tile(target_id), reason="躲雨",
        )

    def _force_execute_plan(self, plan, ctx: TickContext) -> ActionResult | None:
        try:
            action = ActionType(plan.action)
        except ValueError:
            logger.warning("Invalid action in plan: %s", plan.action)
            return None
        if action not in ctx.available_actions:
            return None
        # Realism P0-1: resolve the target tile server-side from the plan's
        # location (id or display name); model-reported coords are ignored.
        target_tile = None
        if settings.realism_enabled and action in _MOVEMENT_ACTIONS:
            from app.agent.plan_target import resolve_target_tile
            target_tile = resolve_target_tile(plan.target, plan.location)
        return ActionResult(
            action=action,
            target_slug=plan.target,
            target_tile=target_tile,
            reason=plan.reason[:100],
        )

    async def _llm_decide(self, ctx: TickContext) -> ActionResult | None:
        today_key = datetime.now().strftime("%Y-%m-%d")
        today_actions = [
            m.content for m in ctx.memories
            if m.created_at and m.created_at.strftime("%Y-%m-%d") == today_key
        ]
        ctx.today_actions = today_actions

        system_prompt, user_prompt = build_decision_prompt(
            resident=ctx.resident,
            schedule_phase=ctx.schedule_phase,
            world_time=ctx.world_time,
            nearby_residents=ctx.nearby_residents,
            memories=ctx.memories,
            today_actions=today_actions,
            available_actions=ctx.available_actions,
            max_daily_actions=settings.agent_max_daily_actions,
            world_events=ctx.world_events,
        )

        if ctx.current_plan and self.plan_adherence_hint:
            plan = ctx.current_plan
            hint = f"\n\n你原本计划在这个时段 {plan.action}（{plan.reason}），但你可以根据当前情况改变主意。"
            user_prompt += hint

        if settings.realism_enabled:
            user_prompt += _needs_prompt_hint(ctx.resident)

        raw = await llm_chat(
            system_prompt, [{"role": "user", "content": user_prompt}], max_tokens=200,
            meter=Meter(scenario="decide", resident_id=ctx.resident.id), expects_json=True,
        )
        result = parse_action_result(raw)
        # Realism P0-1: ignore any model-reported target_tile for movement
        # actions; resolve it server-side from target_slug (tried as id and name).
        if (result is not None and settings.realism_enabled
                and result.action in _MOVEMENT_ACTIONS):
            from app.agent.plan_target import resolve_target_tile
            result.target_tile = resolve_target_tile(result.target_slug, result.target_slug)
        return result

    async def _load_memories(self, ctx: TickContext) -> None:
        try:
            memory_svc = MemoryService(ctx.db)
            ctx.memories = await memory_svc.get_memories(ctx.resident.id, type="event", limit=10)
        except Exception as e:
            logger.warning("Memory retrieval failed for %s: %s", ctx.resident.slug, e)
            ctx.memories = []
