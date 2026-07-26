"""Runtime wiring between S2-5 fiscal policies and the S1-5 town treasury.

Policy storage and treasury accounting keep their existing independent gates.
When either relevant gate is off, this module preserves the pre-wiring result:
legacy tax/wage defaults remain in use and new public disbursements are no-ops.

The disbursement helpers are deliberately all-or-nothing. They delegate the
balance guard to TreasuryService.disburse, so callers never observe a partially
funded subsidy or housing batch and no read-modify-write balance path is added.
"""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.policy_service import (
    PolicyService,
    PolicyValueError,
    validate_fiscal_policy_value,
)

logger = logging.getLogger(__name__)


async def _value(db: AsyncSession, key: str, fallback: float | int) -> float | int:
    """Read a typed fiscal value, preserving the legacy fallback on any miss.

    The explicit storage-gate check matters: PolicyService intentionally falls
    back to system_config while its gate is off, but wiring must remain exactly
    inactive until POLIS_POLICY_ENABLED is enabled.
    """
    if not settings.polis_policy_enabled:
        return fallback
    try:
        value = await PolicyService(db).get(key, default=fallback)
        return validate_fiscal_policy_value(key, value)
    except (PolicyValueError, TypeError, ValueError):
        logger.warning("invalid fiscal policy %s; using fallback %s", key, fallback)
        return fallback


async def tax_rate(db: AsyncSession, *, fallback: float = 0.0) -> float:
    """Effective town tax ratio in the inclusive range 0..1."""
    value = await _value(db, "tax_rate", fallback)
    try:
        return float(validate_fiscal_policy_value("tax_rate", value))
    except PolicyValueError:
        return max(0.0, min(1.0, float(fallback)))


async def default_wage_sc(db: AsyncSession, *, fallback: int) -> int:
    """Policy-backed default public-duty wage (resident perks still override)."""
    value = await _value(db, "npc_default_wage_sc", fallback)
    try:
        return int(validate_fiscal_policy_value("npc_default_wage_sc", value))
    except PolicyValueError:
        return max(0, int(fallback))


async def medical_subsidy_sc(db: AsyncSession) -> int:
    value = await _value(db, "medical_subsidy_sc", 0)
    return int(value)


async def housing_development_scale(db: AsyncSession) -> int:
    value = await _value(db, "housing_development_scale", 0)
    return int(value)


async def pay_medical_subsidy(
    db: AsyncSession,
    *,
    cost_sc: int,
    reason: str = "medical_subsidy",
) -> int:
    """Pay up to the policy subsidy for one treatment; return SC actually paid.

    A short treasury rejects the whole subsidy, matching TreasuryService's
    frozen insufficient-funds contract. The future health service can charge
    the remaining treatment cost only after this call returns.
    """
    if not settings.polis_policy_enabled or not settings.town_treasury_enabled:
        return 0
    cost = max(0, int(cost_sc))
    amount = min(cost, await medical_subsidy_sc(db))
    if amount <= 0:
        return 0
    from app.services import treasury_service
    return amount if await treasury_service.disburse(db, amount, reason=reason) else 0


async def fund_housing_development(
    db: AsyncSession,
    *,
    unit_cost_sc: int,
    reason: str = "housing_development",
) -> int:
    """Fund the approved housing batch; return units funded, or zero.

    Capacity creation remains the caller's responsibility and must happen only
    when this function returns the full policy scale. Passing unit cost from the
    capacity/building subsystem avoids inventing a second price source here.
    """
    if not settings.polis_policy_enabled or not settings.town_treasury_enabled:
        return 0
    unit_cost = max(0, int(unit_cost_sc))
    units = await housing_development_scale(db)
    amount = unit_cost * units
    if units <= 0 or amount <= 0:
        return 0
    from app.services import treasury_service
    return units if await treasury_service.disburse(db, amount, reason=reason) else 0
