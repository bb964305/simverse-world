"""BasicDecidePlugin: decide next action, plan-aware with hybrid execution."""
from __future__ import annotations

import logging
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
