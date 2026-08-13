"""Redis-backed continuity for planned travel and edge-triggered interrupts.

All keys are operational state with real-time TTLs.  The feature is separately
gated by ``REALISM_PLAN_CONTINUITY_ENABLED``; disabling it immediately restores
the old per-tick behavior without touching resident data.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from app.redis_client import get_redis

_DEDUPE_TTL_SECONDS = 2 * 86400
_ACTIVE_TTL_SECONDS = 2 * 3600

logger = logging.getLogger(__name__)


def _active_key(resident_id: str) -> str:
    return f"sv:active_plan_trip:{resident_id}"


def _interrupt_key(resident_id: str, plan_date: str, slot: int, reason: str) -> str:
    return f"sv:plan_interrupt:{plan_date}:{resident_id}:{slot}:{reason}"


async def active_trip_resident_ids(resident_ids: list[str]) -> set[str]:
    """Return residents that currently have an active-trip key, in one Redis read.

    This is intentionally an existence hint only.  AgentLoop uses it to bypass
    the awake-window random sampling gate; :func:`get_active_trip` inside
    ``resident_tick`` remains the single full validator for date, slot, action,
    target, age and step count.
    """
    ids = list(dict.fromkeys(resident_ids))
    if not ids:
        return set()
    values = await get_redis().mget([_active_key(resident_id) for resident_id in ids])
    return {resident_id for resident_id, value in zip(ids, values) if value is not None}


async def get_active_trip(resident_id: str) -> dict | None:
    raw = await get_redis().get(_active_key(resident_id))
    if not raw:
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("not an object")
    except (TypeError, ValueError):
        await clear_active_trip(resident_id, reason="malformed")
        return None
    from app.config import settings
    from app.world_clock import now_world, world_date_key
    if value.get("plan_date") != world_date_key():
        await clear_active_trip(resident_id, reason="stale_plan_date")
        return None
    if type(value.get("plan_slot")) is not int or value["plan_slot"] < 0:
        await clear_active_trip(resident_id, reason="invalid_plan_slot")
        return None
    hour_range = value.get("plan_hour_range")
    if not (
        isinstance(hour_range, list)
        and len(hour_range) == 2
        and all(type(hour) is int for hour in hour_range)
        and 0 <= hour_range[0] < hour_range[1] <= 24
        and hour_range[0] <= now_world().hour < hour_range[1]
    ):
        await clear_active_trip(resident_id, reason="stale_plan_slot")
        return None
    try:
        started_at = datetime.fromisoformat(value["started_at"])
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - started_at.astimezone(timezone.utc) > timedelta(
            seconds=_ACTIVE_TTL_SECONDS
        ):
            await clear_active_trip(resident_id, reason="expired")
            return None
    except (KeyError, TypeError, ValueError):
        await clear_active_trip(resident_id, reason="invalid_started_at")
        return None
    step_count = value.get("step_count")
    if type(step_count) is not int or step_count < 0:
        await clear_active_trip(resident_id, reason="invalid_step_count")
        return None
    if step_count >= settings.realism_plan_continuation_max_steps:
        await clear_active_trip(resident_id, reason="max_steps")
        return None
    from app.agent.actions import ActionType
    try:
        action = ActionType(value.get("action"))
    except (TypeError, ValueError):
        await clear_active_trip(resident_id, reason="invalid_action")
        return None
    movement_actions = {
        ActionType.WANDER, ActionType.GO_HOME, ActionType.VISIT_DISTRICT,
    }
    if action not in movement_actions:
        await clear_active_trip(resident_id, reason="invalid_action")
        return None
    if action in {ActionType.WANDER, ActionType.VISIT_DISTRICT}:
        from app.agent.plan_target import resolve_location_id
        canonical_target = resolve_location_id(
            value.get("target"), value.get("location"))
        if canonical_target is None:
            await clear_active_trip(resident_id, reason="invalid_target")
            return None
        value["target"] = canonical_target
    tile = value.get("target_tile")
    if not (isinstance(tile, list) and len(tile) == 2):
        await clear_active_trip(resident_id, reason="invalid_target")
        return None
    try:
        from app.agent.pathfinder import get_walkable_tiles
        if (int(tile[0]), int(tile[1])) not in get_walkable_tiles():
            await clear_active_trip(resident_id, reason="invalid_target")
            return None
    except (TypeError, ValueError):
        await clear_active_trip(resident_id, reason="invalid_target")
        return None
    return value


async def set_active_trip(resident_id: str, trip: dict) -> None:
    await get_redis().set(
        _active_key(resident_id), json.dumps(trip, ensure_ascii=False),
        ex=_ACTIVE_TTL_SECONDS)


async def clear_active_trip(resident_id: str, reason: str | None = None) -> None:
    await get_redis().delete(_active_key(resident_id))
    if reason:
        logger.info("Active plan trip cleared resident=%s reason=%s", resident_id, reason)


async def claim_slot_interrupt(
    resident_id: str, plan_date: str | None, slot: int, reason: str,
) -> bool:
    """Return True once per resident+slot+reason.

    Each independent interrupt signal gets one probability trial per plan slot;
    a failed distraction roll must not consume the social/notable-event trial.
    """
    if not plan_date:
        return False
    try:
        claimed = await get_redis().set(
            _interrupt_key(resident_id, plan_date, slot, reason), "1",
            ex=_DEDUPE_TTL_SECONDS, nx=True,
        )
        return bool(claimed)
    except Exception:
        # Redis is an optimization boundary here. On an outage, preserve the
        # plan instead of crashing the decide phase or retrying noisy signals.
        logger.warning(
            "Plan interrupt debounce unavailable resident=%s reason=%s",
            resident_id, reason,
        )
        return False
