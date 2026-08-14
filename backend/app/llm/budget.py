"""Budget circuit breaker (P1-1, E-24/E-18).

Reads materialised spend from ``llm_usage`` (``SUM(cost_usd)``) over the current
UTC day and maps the global fraction to a degradation tier. Background LLM work
degrades in three tiers as the daily budget fills — each with a rule-based
fallback so the world never blank-screens — while a separate per-user daily cap
gates player-visible calls. The forge per-request ceiling is a start-time gate.

Everything here is read-only and defensive: a query failure or disabled metering
resolves to ``NORMAL`` (fail-open) so a breaker hiccup never freezes the world.
"""
from __future__ import annotations

import logging
from datetime import datetime, UTC
from enum import Enum

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.llm_usage import LLMUsage

logger = logging.getLogger(__name__)


class BudgetTier(str, Enum):
    NORMAL = "normal"
    THROTTLE = "throttle"        # >=80%: background slows (tick interval ×2)
    RULE_ONLY = "rule_only"      # >=95%: background rule-based (force plan, no inter-resident chat)
    PLAYER_ONLY = "player_only"  # >=100%: background paused, only player-visible calls run


# Tier boundaries as fractions of the global daily budget.
_THROTTLE_AT = 0.80
_RULE_ONLY_AT = 0.95
_PLAYER_ONLY_AT = 1.0


def _day_start() -> datetime:
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


async def _spend_since(
    session: AsyncSession, since: datetime, *, user_id: str | None = None
) -> float:
    stmt = select(func.coalesce(func.sum(LLMUsage.cost_usd), 0.0)).where(LLMUsage.ts >= since)
    if user_id is not None:
        stmt = stmt.where(LLMUsage.user_id == user_id)
    result = await session.execute(stmt)
    return float(result.scalar_one() or 0.0)


async def global_spend_today(session: AsyncSession) -> float:
    return await _spend_since(session, _day_start())


async def user_spend_today(session: AsyncSession, user_id: str) -> float:
    return await _spend_since(session, _day_start(), user_id=user_id)


def tier_for_fraction(fraction: float) -> BudgetTier:
    if fraction >= _PLAYER_ONLY_AT:
        return BudgetTier.PLAYER_ONLY
    if fraction >= _RULE_ONLY_AT:
        return BudgetTier.RULE_ONLY
    if fraction >= _THROTTLE_AT:
        return BudgetTier.THROTTLE
    return BudgetTier.NORMAL


async def background_tier(session: AsyncSession) -> BudgetTier:
    """Current global degradation tier. Fails open to NORMAL."""
    budget = settings.budget_global_daily_usd
    if not settings.llm_metering_enabled or budget <= 0:
        return BudgetTier.NORMAL
    try:
        spent = await global_spend_today(session)
    except Exception as e:  # never let a breaker query freeze the loop
        logger.debug("budget tier check failed, assuming NORMAL: %s", e)
        # Roadmap #6: failing open silently is how cost control dies unnoticed.
        from app.llm.budget_alerts import alert_meter_read_failure
        alert_meter_read_failure("background_tier", e)
        return BudgetTier.NORMAL
    return tier_for_fraction(spent / budget)


async def user_over_budget(session: AsyncSession, user_id: str | None) -> bool:
    """Whether a user has exhausted their per-day player-visible spend cap."""
    budget = settings.budget_user_daily_usd
    if not settings.llm_metering_enabled or budget <= 0 or not user_id:
        return False
    try:
        return await user_spend_today(session, user_id) >= budget
    except Exception as e:
        logger.debug("user budget check failed, allowing: %s", e)
        from app.llm.budget_alerts import alert_meter_read_failure
        alert_meter_read_failure("user_over_budget", e)
        return False


async def forge_blocked(session: AsyncSession, user_id: str | None = None) -> bool:
    """Start-time gate for a forge generation: block when the global budget is
    fully spent (PLAYER_ONLY) or the user is over their own daily cap. Forge
    pipelines additionally enforce their conversation-tagged per-request ceiling
    between paid calls in ``app.forge.budget_guard``."""
    if not settings.llm_metering_enabled:
        return False
    if await background_tier(session) == BudgetTier.PLAYER_ONLY:
        return True
    return await user_over_budget(session, user_id)
