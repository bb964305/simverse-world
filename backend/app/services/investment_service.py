"""E13 goal investment: invest in a life goal; settle on the A1 verdict."""

import logging
from datetime import datetime, UTC

from sqlalchemy import select, func

from app.models.goal_investment import GoalInvestment
from app.models.resident import Resident
from app.models.resident_goal import ResidentGoal

logger = logging.getLogger(__name__)

MIN_AMOUNT = 50
MAX_AMOUNT = 500
GOAL_POOL_CAP = 2000


class InvestmentError(Exception):
    """Raised for invalid invest requests (router maps to 400)."""


async def invest(db, user_id: str, goal_id: str, amount: int) -> GoalInvestment:
    if not (MIN_AMOUNT <= amount <= MAX_AMOUNT):
        raise InvestmentError(f"amount must be {MIN_AMOUNT}-{MAX_AMOUNT}")
    goal = await db.get(ResidentGoal, goal_id)
    if goal is None or goal.kind != "life" or goal.status != "active":
        raise InvestmentError("goal is not an active life goal")

    pooled = (await db.execute(
        select(func.coalesce(func.sum(GoalInvestment.amount), 0)).where(
            GoalInvestment.goal_id == goal_id, GoalInvestment.status == "active",
        )
    )).scalar() or 0
    if pooled + amount > GOAL_POOL_CAP:
        raise InvestmentError(f"goal investment pool cap reached ({GOAL_POOL_CAP})")

    from app.services.coin_service import charge
    if not await charge(db, user_id, amount, f"goal_invest:{goal_id}"):
        raise InvestmentError("Insufficient Soul Coins")

    inv = GoalInvestment(goal_id=goal_id, user_id=user_id, amount=amount, status="active")
    db.add(inv)
    await db.commit()
    await db.refresh(inv)

    try:
        from app.memory.service import MemoryService
        from app.services.notification_service import notify
        await MemoryService(db).add_memory(
            goal.resident_id, "event", "有人资助了我的梦想，这份信任我记在心里。",
            importance=0.85, source="investment", related_user_id=user_id,
        )
        resident = await db.get(Resident, goal.resident_id)
        if resident and resident.creator_id and resident.creator_id != "system":
            await notify(db, resident.creator_id, "system", "有人投资了你的居民的目标",
                         f"{resident.name} 的目标「{goal.title}」收到一笔 {amount} 🪙 投资。", {"goal_id": goal_id})
    except Exception:
        logger.warning("investment side effects failed", exc_info=True)

    # Realism P2-2: backing a resident's dream raises affinity (player→resident,
    # +0.1). Reuses the invest event; no-op when the relations gate is off.
    from app.config import settings
    if settings.realism_relations_enabled:
        try:
            from app.services import relation_service
            await relation_service.bump(
                db, goal.resident_id, user_id,
                d_affinity=settings.realism_rel_affinity_invest,
                type1="resident", type2="player",
            )
        except Exception:
            logger.warning("investment relation bump failed", exc_info=True)
    return inv


async def settle_goal_investments(db, goal_id: str, verdict: str) -> int:
    """Settle all active investments for a resolved goal. verdict: achieved|failed|abandoned."""
    from app.services.coin_service import reward

    invs = (await db.execute(
        select(GoalInvestment).where(GoalInvestment.goal_id == goal_id, GoalInvestment.status == "active")
    )).scalars().all()
    if not invs:
        return 0

    for inv in invs:
        if verdict == "achieved":
            payout = int(inv.amount * 1.5)
            reason = "goal_dividend"
            inv.status = "paid"
        elif verdict == "failed":
            payout = int(inv.amount * 0.5)
            reason = "goal_refund"
            inv.status = "refunded"
        else:  # abandoned → full refund
            payout = inv.amount
            reason = "goal_refund"
            inv.status = "refunded"
        inv.payout = payout
        inv.settled_at = datetime.now(UTC)
        await db.commit()
        if payout > 0:
            await reward(db, inv.user_id, payout, f"{reason}:{goal_id}")

    if verdict == "achieved":
        try:
            goal = await db.get(ResidentGoal, goal_id)
            if goal:
                from app.memory.service import MemoryService
                await MemoryService(db).add_memory(
                    goal.resident_id, "reflection", "没有大家的资助，就没有今天的我。这份恩情我会记一辈子。",
                    importance=0.9, source="reflection",
                )
        except Exception:
            logger.warning("investment memorial memory failed", exc_info=True)
    return len(invs)
