"""Atomic UTC-day quotas shared by every player-authored resident path.

``claim_creation_slot`` is used by forge, JSON card import, and multipart skill
import. ``claim_forge_reward`` is separate because a session may cross midnight;
both counters use one conditional UPDATE as their cross-worker serialization
point. Callers own commit/rollback so a claim is atomic with the resident or
durable forge session it protects.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_, select, update

from app.config import settings
from app.models.user import User


class DailyCreationLimitExceeded(RuntimeError):
    def __init__(self, kind: str, limit: int):
        self.kind = kind
        self.limit = limit
        super().__init__(f"Daily {kind} limit reached ({limit})")


def error_detail(exc: DailyCreationLimitExceeded) -> dict[str, int | str]:
    return {"code": "daily_creation_limit", "kind": exc.kind, "limit": exc.limit}


async def _claim(
    db,
    *,
    user_id: str,
    date_column,
    count_column,
    limit: int,
    kind: str,
) -> int:
    if limit <= 0:
        return 0
    today = datetime.now(UTC).date()
    reset = await db.execute(
        update(User)
        .where(User.id == user_id, or_(date_column.is_(None), date_column != today))
        .values({date_column: today, count_column: 1})
        .execution_options(synchronize_session=False)
    )
    if (reset.rowcount or 0) == 1:
        return 1

    increment = await db.execute(
        update(User)
        .where(User.id == user_id, date_column == today, count_column < limit)
        .values({count_column: count_column + 1})
        .execution_options(synchronize_session=False)
    )
    if (increment.rowcount or 0) != 1:
        raise DailyCreationLimitExceeded(kind, limit)
    value = await db.scalar(select(count_column).where(User.id == user_id))
    return int(value or 0)


async def claim_creation_slot(db, user_id: str) -> int:
    """Reserve one UGC resident creation attempt in the caller transaction."""
    return await _claim(
        db,
        user_id=user_id,
        date_column=User.ugc_creation_date,
        count_column=User.ugc_creation_count,
        limit=settings.ugc_daily_creation_limit,
        kind="ugc_creation",
    )


async def claim_forge_reward(db, user_id: str) -> int:
    """Reserve one successful Forge reward in the completion transaction."""
    return await _claim(
        db,
        user_id=user_id,
        date_column=User.forge_reward_date,
        count_column=User.forge_reward_count,
        limit=settings.forge_daily_reward_limit,
        kind="forge_reward",
    )


async def try_claim_forge_reward(db, user_id: str) -> bool:
    """Atomically claim today's optional 50-SC reward.

    Reward exhaustion must not invalidate a resident whose Forge session was
    legitimately admitted earlier (especially when sessions cross midnight).
    Callers still own the surrounding Resident/session transaction.
    """
    try:
        await claim_forge_reward(db, user_id)
    except DailyCreationLimitExceeded:
        return False
    return True
