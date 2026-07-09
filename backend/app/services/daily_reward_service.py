"""Daily login reward with a consecutive-login streak (D3)."""

import logging
from datetime import datetime, timedelta, UTC

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.transaction import Transaction

logger = logging.getLogger(__name__)

# Reward ladder by streak day; caps at day 7 (index 6) and stays there.
STREAK_LADDER = [10, 15, 20, 25, 30, 40, 50]


def reward_for_streak(streak: int) -> int:
    return STREAK_LADDER[min(max(streak, 1) - 1, len(STREAK_LADDER) - 1)]


async def claim_daily_reward(db: AsyncSession, user_id: str) -> dict:
    """Claim the daily reward, advancing/resetting the login streak.

    Returns {"claimed", ...}. Idempotent per calendar day (UTC) via last_login_date.
    """
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        return {"claimed": False, "reason": "user_not_found"}

    today = datetime.now(UTC).date()
    last = user.last_login_date
    if last is not None and last == today:
        return {"claimed": False, "reason": "already_claimed_today", "streak": user.login_streak}

    if last is not None and last == today - timedelta(days=1):
        streak = (user.login_streak or 0) + 1
    else:
        streak = 1

    amount = reward_for_streak(streak)
    user.login_streak = streak
    user.last_login_date = today
    user.last_daily_reward_at = datetime.now(UTC)  # kept for compatibility
    user.soul_coin_balance += amount
    db.add(Transaction(user_id=user_id, amount=amount, reason="daily_login_reward"))
    await db.commit()
    await db.refresh(user)

    try:
        from app.events.bus import emit
        await emit(db, "login_streak", user_id=user_id, streak=streak)
    except Exception:
        logger.warning("login_streak emit failed", exc_info=True)

    return {
        "claimed": True,
        "amount": amount,
        "streak": streak,
        "new_balance": user.soul_coin_balance,
        "next_reward": reward_for_streak(streak + 1),
    }
