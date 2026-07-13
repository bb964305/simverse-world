"""A5 village daily report: gather material (SQL, no LLM) → compose (1 LLM call).

Cold-start days with no material produce a canned fallback and skip the LLM.
Regeneration is idempotent via the (scope, date, user_id) uniqueness.
"""

import logging
from datetime import datetime, date as date_type, timedelta, UTC

from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm.client import get_client
from app.llm.metering import record_usage
from app.models.digest import Digest
from app.models.memory import Memory
from app.models.personality_history import PersonalityHistory
from app.models.resident import Resident
from app.models.world_event import WorldEvent
from app.ws.manager import manager

logger = logging.getLogger(__name__)

DIGEST_SYSTEM = (
    "你是 Simverse World 小镇的日报编辑。根据下面的今日素材，写一篇温暖、有趣的村落日报（小报体）。"
    "要求：以「# 」开头写一个标题，然后 3-5 段，总字数不超过 600 字，中文，突出居民故事与小镇氛围。"
)


def _extract_text(response) -> str:
    for block in response.content:
        if hasattr(block, "text"):
            return block.text
    return ""


async def gather_material(db: AsyncSession, day: date_type) -> dict:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    end = start + timedelta(days=1)

    chats = (await db.execute(
        select(Memory.content).where(
            Memory.source == "chat_resident",
            Memory.created_at >= start, Memory.created_at < end,
        ).order_by(Memory.importance.desc()).limit(10)
    )).scalars().all()

    shifts = (await db.execute(
        select(PersonalityHistory.old_type, PersonalityHistory.new_type).where(
            PersonalityHistory.created_at >= start, PersonalityHistory.created_at < end,
        ).limit(10)
    )).all()

    events = (await db.execute(
        select(WorldEvent.title, WorldEvent.description).where(WorldEvent.is_active.is_(True))
    )).all()

    heat_top = (await db.execute(
        select(Resident.name, Resident.heat).order_by(Resident.heat.desc()).limit(3)
    )).all()

    stats = {
        "chat_count": len(chats),
        "shift_count": len(shifts),
        "event_count": len(events),
        "heat_top": [{"name": n, "heat": h} for n, h in heat_top],
    }
    has_material = bool(chats or shifts or events)
    return {
        "chats": list(chats),
        "shifts": [f"{o}→{n}" for o, n in shifts],
        "events": [f"{t}：{d}" for t, d in events],
        "heat_top": [f"{n}(热度{h})" for n, h in heat_top],
        "stats": stats,
        "has_material": has_material,
    }


def _build_prompt(day: date_type, material: dict) -> str:
    parts = [f"日期：{day}"]
    if material["events"]:
        parts.append("今日世界事件：\n" + "\n".join(f"- {e}" for e in material["events"]))
    if material["chats"]:
        parts.append("今日居民对话摘录：\n" + "\n".join(f"- {c}" for c in material["chats"]))
    if material["shifts"]:
        parts.append("今日人格变化：\n" + "\n".join(f"- {s}" for s in material["shifts"]))
    if material["heat_top"]:
        parts.append("人气居民：" + "、".join(material["heat_top"]))
    return "\n\n".join(parts)


async def compose_digest(day: date_type, material: dict) -> tuple[str, str]:
    client = get_client("system")
    model = settings.effective_model
    resp = await client.messages.create(
        model=model, max_tokens=800, system=DIGEST_SYSTEM,
        messages=[{"role": "user", "content": _build_prompt(day, material)}],
    )
    text = _extract_text(resp).strip()
    await record_usage("digest", model=model, owner="system", response=resp)
    title = f"{day} 村落日报"
    if text.startswith("#"):
        first_line = text.splitlines()[0].lstrip("# ").strip()
        if first_line:
            title = first_line
    return title, text


async def _pin_digest_bulletin(db: AsyncSession, digest: Digest) -> None:
    """A5→A4: pin the fresh village digest on the bulletin board.

    Unpins any previous digest pin first so only the latest stays pinned.
    Author fields stay NULL (= system post; the board renders it as「系统」).
    Called only when a *new* digest row was inserted, so it is idempotent per
    day for free (regenerating the same day's digest returns early upstream).
    """
    from sqlalchemy import update
    from app.models.bulletin_post import BulletinPost
    from app.services.bulletin_service import create_post

    await db.execute(
        update(BulletinPost)
        .where(BulletinPost.kind == "digest", BulletinPost.pinned.is_(True))
        .values(pinned=False)
    )
    await create_post(db, "digest", digest.title, digest.content_md, pinned=True)


async def generate_village_digest(db: AsyncSession, day: date_type | None = None) -> Digest:
    day = day or datetime.now(UTC).date()

    existing = (await db.execute(
        select(Digest).where(Digest.scope == "village", Digest.date == day, Digest.user_id == "")
    )).scalar_one_or_none()
    if existing is not None:
        return existing  # idempotent

    material = await gather_material(db, day)
    if not material["has_material"]:
        title = f"{day} 村落日报"
        content = f"# {title}\n\n今天的小镇静悄悄，居民们各自忙碌，没有特别的大事发生。明天再来看看吧。"
    else:
        title, content = await compose_digest(day, material)

    digest = Digest(
        scope="village", date=day, user_id="", title=title,
        content_md=content, stats_json=material["stats"],
    )
    db.add(digest)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return (await db.execute(
            select(Digest).where(Digest.scope == "village", Digest.date == day, Digest.user_id == "")
        )).scalar_one()
    await db.refresh(digest)

    # A5→A4: pin the new digest on the bulletin board (best-effort; a bulletin
    # failure must never break digest generation).
    try:
        await _pin_digest_bulletin(db, digest)
    except Exception:
        logger.warning("digest bulletin pin failed", exc_info=True)

    try:
        await manager.broadcast({"type": "digest_ready", "date": str(day)})
    except Exception:
        logger.warning("digest_ready broadcast failed", exc_info=True)
    return digest


# ── E14 personal weekly recap ────────────────────────────────────────

WEEKLY_SYSTEM = (
    "你是私人回顾编辑。用第二人称给玩家写一段温暖的本周回顾，≤400 字，中文，"
    "结合下面的数据，突出与居民的连接。不要罗列数字，讲成故事。"
)

# 12 reproducible behavior tags (LLM only polishes copy, the tag itself is rule-based).
WEEKLY_TAGS = [
    "沉睡者", "城市漫游者", "社交名流", "健谈者", "深夜访客", "探索先锋",
    "长情之人", "新面孔收藏家", "安静的观察者", "热心肠", "梦想合伙人", "小镇常客",
]


def _personality_tag(chat_count: int, distinct: int, explore: int) -> str:
    if chat_count == 0 and explore == 0:
        return "沉睡者"
    if explore >= 5:
        return "城市漫游者"
    if distinct >= 5:
        return "社交名流"
    if chat_count >= 10:
        return "健谈者"
    if distinct >= 3:
        return "新面孔收藏家"
    if explore >= 2:
        return "探索先锋"
    if chat_count >= 3:
        return "小镇常客"
    return "安静的观察者"


def _week_sunday(today: date_type) -> date_type:
    return today - timedelta(days=(today.weekday() + 1) % 7)


async def generate_weekly_recap(db: AsyncSession, user_id: str) -> Digest:
    """Lazily generate this week's personal recap (idempotent per week)."""
    from app.models.conversation import Conversation
    from app.models.user import User
    from app.models.achievement import UserAchievement
    from app.models.location_visit import LocationVisit

    today = datetime.now(UTC).date()
    week_key = _week_sunday(today)
    existing = (await db.execute(
        select(Digest).where(Digest.scope == "personal", Digest.user_id == user_id, Digest.date == week_key)
    )).scalar_one_or_none()
    if existing is not None:
        return existing

    week_start = datetime(week_key.year, week_key.month, week_key.day, tzinfo=UTC)
    convs = (await db.execute(
        select(Conversation).where(Conversation.user_id == user_id, Conversation.started_at >= week_start)
    )).scalars().all()
    chat_count = len(convs)
    turns = sum((c.turns or 0) for c in convs)
    distinct = len({c.resident_id for c in convs})

    mem_rows = (await db.execute(
        select(Memory.content).where(Memory.related_user_id == user_id, Memory.created_at >= week_start)
        .order_by(Memory.importance.desc()).limit(3)
    )).scalars().all()
    ach_count = (await db.execute(
        select(func.count()).select_from(UserAchievement).where(
            UserAchievement.user_id == user_id, UserAchievement.unlocked_at >= week_start,
        )
    )).scalar() or 0
    explore = (await db.execute(
        select(func.count()).select_from(LocationVisit).where(LocationVisit.user_id == user_id)
    )).scalar() or 0

    tag = _personality_tag(chat_count, distinct, explore)
    stats = {"chats": chat_count, "turns": turns, "distinct_residents": distinct,
             "achievements": int(ach_count), "explored": int(explore), "tag": tag}

    if chat_count < 2:
        title = f"{week_key} 本周回顾"
        content = f"# {title}\n\n本周太安静了，几乎没有和居民互动。下周多出来走走，会有新的故事在等你。\n\n本周人格标签：**{tag}**"
    else:
        material = (f"对话 {chat_count} 次 / {turns} 轮，认识了 {distinct} 位居民；"
                    f"被写进 {len(mem_rows)} 条记忆；解锁成就 {ach_count} 个。\n"
                    + "记忆摘录：\n" + "\n".join(f"- {m}" for m in mem_rows))
        client = get_client("system")
        model = settings.effective_model
        resp = await client.messages.create(
            model=model, max_tokens=600, system=WEEKLY_SYSTEM,
            messages=[{"role": "user", "content": f"本周人格标签：{tag}\n{material}"}],
        )
        body = _extract_text(resp).strip()
        await record_usage("weekly_recap", model=model, owner="system", response=resp)
        title = f"{week_key} 本周回顾"
        content = f"# {title}\n\n{body}\n\n本周人格标签：**{tag}**"

    digest = Digest(scope="personal", date=week_key, user_id=user_id, title=title,
                    content_md=content, stats_json=stats)
    db.add(digest)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return (await db.execute(
            select(Digest).where(Digest.scope == "personal", Digest.user_id == user_id, Digest.date == week_key)
        )).scalar_one()
    await db.refresh(digest)
    return digest


def serialize(d: Digest) -> dict:
    return {
        "id": d.id,
        "scope": d.scope,
        "date": str(d.date),
        "title": d.title,
        "content_md": d.content_md,
        "stats": d.stats_json or {},
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }
