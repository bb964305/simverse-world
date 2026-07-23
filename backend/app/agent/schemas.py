"""Data models and utility functions for the agent plugin system."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from app.agent.actions import ActionType, ActionResult
from app.llm.json_extract import extract_json_object

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.models.memory import Memory
    from app.models.resident import Resident

logger = logging.getLogger(__name__)


@dataclass
class DailyGoal:
    goal: str
    motivation: str
    created_at: str
    status: str = "active"


@dataclass
class HourlyPlan:
    slot: int
    hour_range: tuple[int, int]
    action: str
    target: str | None
    location: str | None
    importance: int
    reason: str
    status: str = "pending"


@dataclass
class DailySchedulePlan:
    goal: DailyGoal
    plans: list[HourlyPlan]
    generated_date: str


@dataclass
class TickContext:
    db: AsyncSession
    resident: Resident
    world_time: str
    hour: int
    schedule_phase: str
    nearby_residents: list[Resident] = field(default_factory=list)
    nearby_known: dict[str, str] = field(default_factory=dict)  # resident_id -> relationship text
    # P2-3: batched numeric relations for read-path weighted sampling
    # (other_id -> RelationView), loaded once per tick (perf: +1 query, never
    # per-candidate). None until first loaded; {} means "loaded, no relations".
    relations: dict | None = None
    current_plan: HourlyPlan | None = None
    daily_goal: DailyGoal | None = None
    # A1: the resident's active life goal {"title", "progress"}, fetched
    # lazily (fail-open) where needed — currently by the plan phase on
    # plan-generation days so daily plans align with the long-term goal.
    life_goal: dict | None = None
    action_result: ActionResult | None = None
    plan_followed: bool = True
    new_tile: tuple[int, int] | None = None
    memory_created: bool = False
    memories: list[Memory] = field(default_factory=list)
    today_actions: list[str] = field(default_factory=list)
    available_actions: list[ActionType] = field(default_factory=list)
    world_events: list[dict] = field(default_factory=list)  # S1: active world events (60s-cached)
    skip_remaining: bool = False
    # Budget circuit breaker (P1-1): when the breaker is at its 95%+ tier the
    # loop sets this so decide hard-follows the plan (no LLM interrupt) — the
    # rule-based fallback that keeps the world running without spending (E-24).
    force_plan_only: bool = False


def get_world_time() -> tuple[str, int, str]:
    now = datetime.now()
    hour = now.hour
    formatted = now.strftime("%H:%M")
    if 5 <= hour < 9:
        phase = "清晨"
    elif 9 <= hour < 12:
        phase = "上午"
    elif 12 <= hour < 14:
        phase = "午后"
    elif 14 <= hour < 18:
        phase = "下午"
    elif 18 <= hour < 21:
        phase = "傍晚"
    elif 21 <= hour < 24:
        phase = "夜晚"
    else:
        phase = "深夜"
    return formatted, hour, phase


def parse_action_result(raw: str) -> ActionResult | None:
    data = extract_json_object(raw)
    if data is None:
        logger.debug("No JSON found in decision response: %s", raw[:200])
        return None
    try:
        action_str = data.get("action", "")
        try:
            action = ActionType(action_str)
        except ValueError:
            logger.debug("Unknown action type: %s", action_str)
            return None
        target_tile = data.get("target_tile")
        if target_tile and isinstance(target_tile, list) and len(target_tile) == 2:
            target_tile = (int(target_tile[0]), int(target_tile[1]))
        else:
            target_tile = None
        return ActionResult(
            action=action,
            target_slug=data.get("target_slug"),
            target_tile=target_tile,
            reason=str(data.get("reason", ""))[:100],
        )
    except (TypeError, KeyError, ValueError) as e:
        logger.debug("Failed to parse action result: %s | raw: %s", e, raw[:200])
        return None
