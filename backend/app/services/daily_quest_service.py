"""D3 daily topic quest: generate (rule-based, no LLM) + complete on chat.

The quest nudges the player toward a resident (biased to low-heat residents for
long-tail exposure) with a suggested topic. Completing a chat with that resident
for >= min_turns marks it done and pays out.
"""

import logging
import random

from datetime import datetime, UTC

from sqlalchemy import select

from app.database import async_session
from app.events.bus import on
from app.models.daily_quest import DailyQuest
from app.models.resident import Resident

logger = logging.getLogger(__name__)

MIN_TURNS = 3
QUEST_REWARD = 15
TOPIC_TEMPLATES = [
    "聊聊他/她最近在忙什么",
    "问问他/她的爱好",
    "了解一下他/她的过去",
    "听听他/她对小镇的看法",
    "问问他/她最近的心情",
]


async def _pick_resident(db) -> Resident | None:
    residents = (await db.execute(
        select(Resident).where(Resident.status != "sleeping").order_by(Resident.heat.asc()).limit(8)
    )).scalars().all()
    if not residents:
        return None
    # Duty system: a quest-magnet resident (爱打听的学生) attracts daily quests
    # with her perk's probability — she is the town's natural "帮我找…" target.
    try:
        from app.services.duty_service import perk as _duty_perk
        magnets = [r for r in residents if _duty_perk(r, "quest_magnet", 0.0) > 0]
        if magnets:
            magnet = magnets[0]
            if random.random() < _duty_perk(magnet, "quest_magnet", 0.0):
                return magnet
    except Exception:
        pass
    # Bias toward the low-heat end (already ordered asc) for discovery.
    return random.choice(residents[:5]) if len(residents) >= 5 else random.choice(residents)


async def generate_daily_quest(db, user_id: str) -> DailyQuest | None:
    """Create today's quest if the user doesn't have one yet (idempotent)."""
    today = datetime.now(UTC).date()
    existing = (await db.execute(
        select(DailyQuest).where(DailyQuest.user_id == user_id, DailyQuest.date == today)
    )).scalar_one_or_none()
    if existing is not None:
        return existing

    resident = await _pick_resident(db)
    if resident is None:
        return None

    quest = DailyQuest(
        user_id=user_id, date=today, status="pending", reward_sc=QUEST_REWARD,
        quest_json={
            "resident_slug": resident.slug,
            "resident_name": resident.name,
            "topic": random.choice(TOPIC_TEMPLATES),
            "min_turns": MIN_TURNS,
        },
    )
    db.add(quest)
    try:
        await db.commit()
        await db.refresh(quest)
    except Exception:
        await db.rollback()
        return (await db.execute(
            select(DailyQuest).where(DailyQuest.user_id == user_id, DailyQuest.date == today)
        )).scalar_one_or_none()
    return quest


async def get_today_quest(db, user_id: str) -> DailyQuest | None:
    today = datetime.now(UTC).date()
    return (await db.execute(
        select(DailyQuest).where(DailyQuest.user_id == user_id, DailyQuest.date == today)
    )).scalar_one_or_none()


def serialize(q: DailyQuest) -> dict:
    return {
        "id": q.id,
        "date": str(q.date),
        "quest": q.quest_json,
        "status": q.status,
        "reward_sc": q.reward_sc,
    }


@on("chat_completed")
async def _complete_daily_quest(db, user_id: str = "", resident_id: str | None = None, turns: int = 0, **kw) -> None:
    """Mark today's quest done if this chat matches it. Own session; rewards + notifies."""
    if not user_id or not resident_id:
        return
    async with async_session() as s:
        quest = await get_today_quest(s, user_id)
        if quest is None or quest.status == "done":
            return
        q = quest.quest_json or {}
        if turns < int(q.get("min_turns", MIN_TURNS)):
            return
        resident = await s.get(Resident, resident_id)
        if resident is None or resident.slug != q.get("resident_slug"):
            return
        quest.status = "done"
        await s.commit()
        try:
            from app.services.coin_service import reward
            from app.services.notification_service import notify
            await reward(s, user_id, quest.reward_sc, "daily_quest")
            await notify(s, user_id, "system", "每日任务完成", f"你完成了与 {q.get('resident_name', '')} 的对话任务", {"reward_sc": quest.reward_sc})
        except Exception:
            logger.warning("daily quest payout failed", exc_info=True)
