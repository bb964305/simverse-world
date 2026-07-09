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

    try:
        await manager.broadcast({"type": "digest_ready", "date": str(day)})
    except Exception:
        logger.warning("digest_ready broadcast failed", exc_info=True)
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
