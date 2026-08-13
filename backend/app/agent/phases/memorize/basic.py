"""BasicMemorizePlugin: create event memory from action result."""
from __future__ import annotations

import logging
from typing import Any

from app.agent.actions import ActionType
from app.agent.map_data import get_location_at, get_location_by_id, get_valid_target_tile
from app.agent.schemas import TickContext
from app.config import settings
from app.memory.service import MemoryService

logger = logging.getLogger(__name__)

# Movement actions get realism-aware, displacement-truthful memory text.
_MOVEMENT_ACTIONS = {ActionType.WANDER, ActionType.VISIT_DISTRICT, ActionType.GO_HOME}


def _flashbulb_boost(mood_json: dict | None) -> float:
    """Realism P0-3 flashbulb effect: strong emotion burns memories in harder.
    importance += coef × |valence| × arousal."""
    m = mood_json or {}
    return settings.realism_flashbulb_coef * abs(float(m.get("valence", 0.0))) * float(m.get("arousal", 0.0))


def _target_name(slug) -> str | None:
    if not isinstance(slug, str):
        return None
    loc = get_location_by_id(slug)
    return loc.get("name") if loc else None


def _movement_memory(ctx) -> tuple[str, dict]:
    """Realism P0-1: memory text reflecting the *actual* displacement + a move
    breadcrumb for the burn-in probes (Task 6).

    - arrived at the resolved target  -> "到达了X"
    - moved this tick but not arrived  -> "正在前往X"
    - no movement happened             -> "在X停留" (never claims a move)
    """
    from app.agent.plan_target import resolve_location_id, resolve_target_tile
    ar = ctx.action_result
    res = ctx.resident
    action = ar.action

    if action == ActionType.GO_HOME:
        home_id = getattr(res, "home_location_id", None)
        if home_id:
            target_tile = get_valid_target_tile(home_id)
        elif res.home_tile_x is not None:
            target_tile = (res.home_tile_x, res.home_tile_y)
        else:
            target_tile = None
        dest = "家"
    else:
        scheduled = getattr(ctx, "scheduled_plan", None)
        follows_scheduled = bool(
            scheduled is not None
            and ctx.plan_followed
            and scheduled.action == action.value
        )
        location_name = scheduled.location if follows_scheduled else ar.target_slug
        target_id = resolve_location_id(ar.target_slug, location_name)
        target_tile = ar.target_tile or resolve_target_tile(target_id, location_name)
        dest = _target_name(target_id) or "某处"

    cur = (res.tile_x, res.tile_y)
    arrived = target_tile is not None and cur == tuple(target_tile)
    moved = res.status == "walking"

    if arrived:
        text = f"到达了{dest}"
    elif moved:
        text = f"正在前往{dest}"
    else:
        loc = get_location_at(res.tile_x, res.tile_y)
        text = f"在{loc['name'] if loc else '户外'}停留"

    scheduled = getattr(ctx, "scheduled_plan", None)
    planned = bool(
        scheduled is not None
        and ctx.plan_followed
        and scheduled.action == action.value
    )
    if action == ActionType.GO_HOME:
        target_id = getattr(res, "home_location_id", None)
    return text, {
        "intent": action.value,
        "target": target_id if isinstance(target_id, str) else None,
        "moved": bool(moved),
        "arrived": bool(arrived),
        "planned": planned,
        "plan_date": getattr(ctx, "plan_date", None) if planned else None,
        "plan_slot": scheduled.slot if planned else None,
    }


def _plan_memory(ctx) -> dict | None:
    """Structured attribution for every action taken inside a scheduled slot."""
    plan = getattr(ctx, "scheduled_plan", None)
    if plan is None:
        return None
    target = plan.target
    try:
        action = ActionType(plan.action)
    except ValueError:
        action = None
    if action in (ActionType.WANDER, ActionType.VISIT_DISTRICT):
        from app.agent.plan_target import resolve_location_id
        target = resolve_location_id(plan.target, plan.location)
    return {
        "date": getattr(ctx, "plan_date", None),
        "slot": plan.slot,
        "scheduled_action": plan.action,
        "scheduled_target": target if isinstance(target, str) else None,
        "followed": bool(ctx.plan_followed and ctx.action_result.action.value == plan.action),
        "interrupt_reason": getattr(ctx, "plan_interrupt_reason", None),
    }


def format_action_memory(action_result, resident) -> str:
    """Format an action into a human-readable memory string with location."""
    loc = get_location_at(resident.tile_x, resident.tile_y)
    loc_name = loc["name"] if loc else "户外"

    action = action_result.action
    if action == ActionType.WANDER:
        return f"在{loc_name}附近四处游荡"
    elif action == ActionType.GO_HOME:
        return "回到了自己的家"
    elif action == ActionType.VISIT_DISTRICT:
        return f"前往了{loc_name}"
    elif action == ActionType.CHAT_RESIDENT:
        return f"在{loc_name}和 {action_result.target_slug or '某位居民'} 聊天"
    elif action == ActionType.OBSERVE:
        return f"在{loc_name}静静地观察着周围的情况"
    elif action == ActionType.EAVESDROP:
        return f"在{loc_name}偷偷听了附近居民的对话"
    elif action == ActionType.REFLECT:
        return f"在{loc_name}进行了一段时间的自我反思"
    elif action == ActionType.JOURNAL:
        return f"在{loc_name}记录了今天的见闻"
    elif action == ActionType.WORK:
        return f"在{loc_name}专注于工作"
    elif action == ActionType.STUDY:
        return f"在{loc_name}学习了一些新知识"
    elif action == ActionType.GOSSIP:
        return f"在{loc_name}和 {action_result.target_slug or '某位居民'} 闲聊八卦"
    elif action == ActionType.NAP:
        return f"在{loc_name}小憩了一会儿"
    elif action == ActionType.IDLE:
        return f"在{loc_name}发了会儿呆"
    elif action == ActionType.RESEARCH:
        return f"在{loc_name}接入沙箱，投入了一段真实世界的研究工作"
    else:
        return f"在{loc_name}执行了 {action.value}"


class BasicMemorizePlugin:
    def __init__(self, params: dict[str, Any] | None = None):
        params = params or {}
        self.base_importance: float = params.get("base_importance", 0.3)
        self.plan_deviation_boost: float = params.get("plan_deviation_boost", 0.2)

    async def execute(self, ctx: TickContext) -> TickContext:
        if ctx.action_result is None:
            return ctx

        importance = self.base_importance
        if not ctx.plan_followed:
            importance += self.plan_deviation_boost

        action = ctx.action_result.action
        move_meta = None
        if settings.realism_enabled and action in _MOVEMENT_ACTIONS:
            memory_content, move_meta = _movement_memory(ctx)
        else:
            memory_content = format_action_memory(ctx.action_result, ctx.resident)

        if settings.realism_enabled:
            importance += _flashbulb_boost(ctx.resident.mood_json)

        importance = min(importance, 1.0)

        try:
            memory_svc = MemoryService(ctx.db)
            metadata = {}
            if move_meta:
                metadata["move"] = move_meta
            plan_meta = _plan_memory(ctx)
            if plan_meta:
                metadata["plan"] = plan_meta
            await memory_svc.add_memory(
                resident_id=ctx.resident.id,
                type="event",
                content=memory_content,
                importance=importance,
                source="agent_action",
                metadata_json=(metadata or None),
            )
            ctx.memory_created = True
        except Exception as e:
            logger.warning("Memorize failed for %s: %s", ctx.resident.slug, e)

        return ctx
