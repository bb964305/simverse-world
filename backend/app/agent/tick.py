"""Resident tick: slim orchestrator calling plugin phases."""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.actions import ActionResult
from app.agent.registry import registry
from app.agent.schemas import TickContext, get_world_time
from app.config import settings
from app.models.resident import Resident
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

# Per-resident daily action cap, in Redis so it is shared across the API
# workers and the standalone agent-worker and survives process restarts
# (P0-3b). World time (agent-T): the date stamp is the WORLD date, so the cap
# is "per WORLD day" and "resets" every world midnight (every 6 real hours at
# k=4) — a spend guardrail scoped to the accelerated day the residents live in.
# The TTL stays in REAL seconds (Redis housekeeping, real-time semantics): a
# 2-real-day TTL comfortably outlives any world day and cleans up stale keys.
_DAILY_KEY_PREFIX = "sv:daily_actions:"
_DAILY_TTL_SECONDS = 2 * 86400


def _daily_key(resident_id: str) -> str:
    from app.world_clock import world_date_key
    return f"{_DAILY_KEY_PREFIX}{world_date_key()}:{resident_id}"


async def _over_daily_limit(resident_id: str) -> bool:
    val = await get_redis().get(_daily_key(resident_id))
    return int(val or 0) >= settings.agent_max_daily_actions


async def _incr_daily_count(resident_id: str) -> None:
    r = get_redis()
    key = _daily_key(resident_id)
    count = await r.incr(key)
    if count == 1:  # first action today — set the cleanup TTL once
        await r.expire(key, _DAILY_TTL_SECONDS)


async def resident_tick(
    db: AsyncSession,
    resident: Resident,
    *,
    force_plan_only: bool = False,
) -> ActionResult | None:
    """Execute one autonomous tick for a resident via plugin chain.

    ``force_plan_only`` (set by the budget breaker's 95%+ tier) makes decide
    hard-follow the plan with no LLM interrupt — the rule-based fallback.
    """
    # Realism P1-10: metabolize needs each processed tick (energy/satiety/social
    # drain). Not an action — runs before the daily-cap gate so needs keep moving
    # even after a resident spends its action budget, and never counts as an action.
    if settings.realism_enabled:
        try:
            from app.agent.needs import get_needs, metabolize, write_needs
            sbti = (resident.meta_json or {}).get("sbti")
            write_needs(resident, metabolize(get_needs(resident), status=resident.status, sbti=sbti))
            await db.commit()
        except Exception:
            logger.warning("needs metabolism failed for %s", resident.slug, exc_info=True)

    if await _over_daily_limit(resident.id):
        return None

    world_time, hour, schedule_phase = get_world_time()

    ctx = TickContext(
        db=db,
        resident=resident,
        world_time=world_time,
        hour=hour,
        schedule_phase=schedule_phase,
        force_plan_only=force_plan_only,
    )

    try:
        phases = registry.get_phases(resident)
    except RuntimeError as e:
        logger.error("Failed to load phases for %s: %s", resident.slug, e, exc_info=True)
        return None

    for phase in phases:
        try:
            ctx = await phase.execute(ctx)
        except Exception as e:
            logger.warning("Phase failed for %s: %s", resident.slug, e)
            break
        if ctx.skip_remaining:
            break

    if ctx.action_result:
        await _incr_daily_count(resident.id)
        logger.debug("Resident %s ticked: %s -> %s",
                      resident.slug, ctx.action_result.action.value, ctx.action_result.reason)

    return ctx.action_result
