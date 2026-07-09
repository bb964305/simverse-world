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

# First 12 achievements (D1). In-code defs are the engine's authoritative source
# (reward/copy); the achievements table is seeded from them.
ACHIEVEMENT_DEFS: list[dict] = [
    {"code": "first_chat", "title": "初次相遇", "description": "第一次和居民对话", "icon": "💬", "points": 1, "reward_sc": 20, "hidden": False},
    {"code": "deep_talk", "title": "促膝长谈", "description": "单次对话达到 10 轮", "icon": "🗣️", "points": 2, "reward_sc": 30, "hidden": False},
    {"code": "remembered", "title": "被记住", "description": "第一次被居民写进记忆", "icon": "📝", "points": 2, "reward_sc": 20, "hidden": False},
    {"code": "memory_keeper_10", "title": "念念不忘", "description": "被 10 条居民记忆记住", "icon": "🧠", "points": 4, "reward_sc": 50, "hidden": False},
    {"code": "soul_shaper", "title": "灵魂塑造者", "description": "首次触发居民人格跳变", "icon": "✨", "points": 8, "reward_sc": 100, "hidden": False},
    {"code": "week_streak", "title": "七日之约", "description": "连续登录 7 天", "icon": "📅", "points": 4, "reward_sc": 50, "hidden": False},
    {"code": "explorer_5", "title": "城市漫游者", "description": "到访 5 个不同的地点", "icon": "🧭", "points": 3, "reward_sc": 30, "hidden": False},
    {"code": "explorer_all", "title": "踏遍全城", "description": "到访全部地点", "icon": "🗺️", "points": 8, "reward_sc": 100, "hidden": False},
    {"code": "errand_runner", "title": "跑腿达人", "description": "完成第一个委托", "icon": "📜", "points": 3, "reward_sc": 30, "hidden": False},
    {"code": "patron", "title": "赞助人", "description": "首次打赏创作", "icon": "💝", "points": 1, "reward_sc": 10, "hidden": False},
    {"code": "socialite", "title": "社交名流", "description": "与 10 位不同居民聊过", "icon": "🎭", "points": 4, "reward_sc": 50, "hidden": False},
    {"code": "dreamt_of", "title": "入梦之人", "description": "首次被居民梦到", "icon": "🌙", "points": 5, "reward_sc": 66, "hidden": True},
]

_DEF_BY_CODE: dict[str, dict] = {d["code"]: d for d in ACHIEVEMENT_DEFS}

# Target for "visit all locations" — every distinct named, bounded map location.
try:
    from app.agent.map_data import LOCATIONS as _LOCATIONS
    ALL_LOCATIONS_TARGET = len([lid for lid, l in _LOCATIONS.items() if l.get("bounds")])
except Exception:  # pragma: no cover
    ALL_LOCATIONS_TARGET = 20


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


async def increment_distinct(user_id: str, code: str, target: int, member: str) -> str | None:
    """Bump a distinct-set achievement; unlock when the set reaches target."""
    now = datetime.now(UTC)
    async with async_session() as db:
        existing = (await db.execute(
            select(UserAchievement).where(
                UserAchievement.user_id == user_id, UserAchievement.code == code,
            )
        )).scalar_one_or_none()
        if existing and existing.unlocked_at is not None:
            return None
        seen = list((existing.progress_json or {}).get("seen", [])) if existing else []
        if member not in seen:
            seen.append(member)
        count = len(seen)
        reached = count >= target
        progress = {"seen": seen, "count": count, "target": target}
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


# ── Checkers (registered on domain events, D1) ───────────────────────

@on("chat_completed")
async def _ach_first_chat(db, user_id: str = "", **kw) -> None:
    if user_id:
        await unlock(user_id, "first_chat")


@on("chat_completed")
async def _ach_deep_talk(db, user_id: str = "", turns: int = 0, **kw) -> None:
    if user_id and turns >= 10:
        await unlock(user_id, "deep_talk")


@on("chat_completed")
async def _ach_socialite(db, user_id: str = "", resident_id: str | None = None, **kw) -> None:
    if user_id and resident_id:
        await increment_distinct(user_id, "socialite", 10, resident_id)


@on("memory_written_about_user")
async def _ach_remembered(db, user_id: str = "", **kw) -> None:
    if user_id:
        await unlock(user_id, "remembered")


@on("memory_written_about_user")
async def _ach_memory_keeper(db, user_id: str = "", **kw) -> None:
    if user_id:
        await increment(user_id, "memory_keeper_10", 10)


@on("personality_shifted")
async def _ach_soul_shaper(db, user_id: str = "", **kw) -> None:
    if user_id:
        await unlock(user_id, "soul_shaper")


@on("login_streak")
async def _ach_week_streak(db, user_id: str = "", streak: int = 0, **kw) -> None:
    if user_id and streak >= 7:
        await unlock(user_id, "week_streak")


@on("location_first_visit")
async def _ach_explorer_5(db, user_id: str = "", **kw) -> None:
    if user_id:
        await increment(user_id, "explorer_5", 5)


@on("location_first_visit")
async def _ach_explorer_all(db, user_id: str = "", **kw) -> None:
    if user_id:
        await increment(user_id, "explorer_all", ALL_LOCATIONS_TARGET)


@on("commission_completed")
async def _ach_errand_runner(db, user_id: str = "", **kw) -> None:
    if user_id:
        await unlock(user_id, "errand_runner")


@on("purchase_tip")
async def _ach_patron(db, user_id: str = "", **kw) -> None:
    if user_id:
        await unlock(user_id, "patron")


@on("dream_generated")
async def _ach_dreamt_of(db, user_id: str = "", **kw) -> None:
    if user_id:
        await unlock(user_id, "dreamt_of")


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
