"""Achievement engine (S2).

In-code ACHIEVEMENT_DEFS are the engine's source of truth (rewards/copy); the
achievements table is seeded from them so ops can override copy without a deploy.
Each achievement is unlocked by a checker registered on a domain event. Unlock is
idempotent (UniqueConstraint on user_id+code) and writes in its own short session
so it never couples the emitting code path's transaction.
"""

import logging
from datetime import datetime, UTC

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import async_session
from app.events.bus import on
from app.models.achievement import Achievement, UserAchievement
from app.services.coin_service import reward
from app.services.notification_service import notify
from app.ws.manager import manager

logger = logging.getLogger(__name__)

# Starter set proving the engine (D1 expands to the full 12). Each dict is the
# authoritative definition used at unlock time.
ACHIEVEMENT_DEFS: list[dict] = [
    {"code": "first_chat", "title": "初次相遇", "description": "第一次和居民对话", "icon": "💬", "points": 1, "reward_sc": 10, "hidden": False},
    {"code": "conversationalist_10", "title": "健谈者", "description": "累计完成 10 次对话", "icon": "🗣️", "points": 3, "reward_sc": 30, "hidden": False},
    {"code": "explorer_5", "title": "城市漫游者", "description": "探索 5 个不同的地点", "icon": "🧭", "points": 3, "reward_sc": 30, "hidden": False},
]

_DEF_BY_CODE: dict[str, dict] = {d["code"]: d for d in ACHIEVEMENT_DEFS}


async def _grant_rewards(db, user_id: str, code: str) -> None:
    """Reward + S4 notify + WS toast for a freshly-unlocked achievement."""
    d = _DEF_BY_CODE.get(code)
    if not d:
        return
    if d["reward_sc"]:
        await reward(db, user_id, d["reward_sc"], f"achievement:{code}")
    await notify(
        db, user_id, "achievement", d["title"], d["description"],
        {"code": code, "reward_sc": d["reward_sc"]},
    )
    try:
        await manager.send(user_id, {
            "type": "achievement_unlocked",
            "code": code, "title": d["title"], "reward_sc": d["reward_sc"],
        })
    except Exception:
        logger.warning("achievement_unlocked WS push failed for %s", user_id, exc_info=True)


async def unlock(user_id: str, code: str) -> str | None:
    """Idempotently unlock a one-shot achievement. Returns code if newly unlocked."""
    now = datetime.now(UTC)
    async with async_session() as db:
        existing = (await db.execute(
            select(UserAchievement).where(
                UserAchievement.user_id == user_id, UserAchievement.code == code,
            )
        )).scalar_one_or_none()
        if existing and existing.unlocked_at is not None:
            return None
        if existing:
            existing.unlocked_at = now
        else:
            db.add(UserAchievement(user_id=user_id, code=code, unlocked_at=now))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return None
        await _grant_rewards(db, user_id, code)
        return code


async def increment(user_id: str, code: str, target: int) -> str | None:
    """Bump a counting achievement; unlock when count reaches target."""
    now = datetime.now(UTC)
    async with async_session() as db:
        existing = (await db.execute(
            select(UserAchievement).where(
                UserAchievement.user_id == user_id, UserAchievement.code == code,
            )
        )).scalar_one_or_none()
        if existing and existing.unlocked_at is not None:
            return None
        count = ((existing.progress_json or {}).get("count", 0) if existing else 0) + 1
        reached = count >= target
        progress = {"count": count, "target": target}
        if existing:
            existing.progress_json = progress
            if reached:
                existing.unlocked_at = now
        else:
            db.add(UserAchievement(
                user_id=user_id, code=code, progress_json=progress,
                unlocked_at=now if reached else None,
            ))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return None
        if reached:
            await _grant_rewards(db, user_id, code)
            return code
        return None


# ── Checkers (registered on domain events) ───────────────────────────

@on("chat_completed")
async def _ach_first_chat(db, user_id: str = "", **kw) -> None:
    if user_id:
        await unlock(user_id, "first_chat")


@on("chat_completed")
async def _ach_conversationalist(db, user_id: str = "", **kw) -> None:
    if user_id:
        await increment(user_id, "conversationalist_10", 10)


@on("location_first_visit")
async def _ach_explorer(db, user_id: str = "", **kw) -> None:
    # Fires once S5 LocationTracker emits location_first_visit.
    if user_id:
        await increment(user_id, "explorer_5", 5)


# ── Seed + query ─────────────────────────────────────────────────────

async def seed_achievements(db) -> int:
    """Upsert ACHIEVEMENT_DEFS into the achievements table. Returns count."""
    for d in ACHIEVEMENT_DEFS:
        row = await db.get(Achievement, d["code"])
        if row is None:
            db.add(Achievement(
                code=d["code"], title=d["title"], description=d["description"],
                icon=d["icon"], points=d["points"], reward_sc=d["reward_sc"], hidden=d["hidden"],
            ))
    await db.commit()
    return len(ACHIEVEMENT_DEFS)


async def get_user_achievements(db, user_id: str) -> list[dict]:
    """Merge definitions with a user's progress for GET /achievements."""
    rows = {
        ua.code: ua for ua in (await db.execute(
            select(UserAchievement).where(UserAchievement.user_id == user_id)
        )).scalars().all()
    }
    out: list[dict] = []
    for d in ACHIEVEMENT_DEFS:
        ua = rows.get(d["code"])
        unlocked = ua is not None and ua.unlocked_at is not None
        hide = d["hidden"] and not unlocked
        out.append({
            "code": d["code"],
            "title": "???" if hide else d["title"],
            "description": "隐藏成就" if hide else d["description"],
            "icon": "❓" if hide else d["icon"],
            "points": d["points"],
            "reward_sc": d["reward_sc"],
            "hidden": d["hidden"],
            "unlocked": unlocked,
            "unlocked_at": ua.unlocked_at.isoformat() if (ua and ua.unlocked_at) else None,
            "progress": (ua.progress_json if ua else None),
        })
    return out
