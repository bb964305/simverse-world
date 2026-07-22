"""E2 dreams: active residents dream at night, weaving today's + an old memory.

Runs in the nightly cron (after the digest). LLM-generated; only active residents
(≥3 new memories today) are eligible, with a per-resident coin-flip and a global
nightly cap — so cost stays bounded. A dream that involves a player fires
dream_generated (→ D1 dreamt_of) + an S4 notice.
"""

import random
import logging
from datetime import datetime, timedelta, UTC

from sqlalchemy import select, func

from app.config import settings
from app.database import async_session
from app.events.bus import emit
from app.llm.client import get_client
from app.llm.metering import record_usage
from app.memory.service import MemoryService
from app.models.memory import Memory
from app.models.resident import Resident

logger = logging.getLogger(__name__)

DREAM_PROBABILITY = 0.5
GLOBAL_NIGHTLY_CAP = 10
MIN_MEMORIES = 3

DREAM_SYSTEM = (
    "你是梦境编织者。把下面的记忆素材揉成一段梦，允许荒诞的混搭与错位，第一人称，"
    "80 字以内，符合角色人格。只输出梦的内容。"
)
# Realism P1-11: piggyback a mood `tone` on the SAME dream call (the sole
# sanctioned LLM-output extension) — no new call.
DREAM_SYSTEM_JSON = (
    "你是梦境编织者。把下面的记忆素材揉成一段梦，允许荒诞的混搭与错位，第一人称，"
    "80 字以内，符合角色人格。只输出 JSON："
    '{"dream": "梦的内容", "tone": "positive|neutral|negative"}。'
)


def _extract_text(resp) -> str:
    for block in resp.content:
        if hasattr(block, "text"):
            return block.text
    return ""


async def generate_dream(db, resident: Resident) -> Memory | None:
    """Generate one dream for a resident if eligible + the coin flip passes."""
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    today = (await db.execute(
        select(Memory).where(Memory.resident_id == resident.id, Memory.created_at >= today_start)
        .order_by(Memory.importance.desc()).limit(3)
    )).scalars().all()
    if len(today) < MIN_MEMORIES:
        return None
    if random.random() >= DREAM_PROBABILITY:
        return None

    old = (await db.execute(
        select(Memory).where(
            Memory.resident_id == resident.id, Memory.created_at < today_start, Memory.importance >= 0.6,
        ).order_by(func.random()).limit(1)
    )).scalar_one_or_none()

    material = list(today) + ([old] if old else [])
    involves = next((m.related_user_id for m in material if m.related_user_id), None)
    sbti = (resident.meta_json or {}).get("sbti", {}).get("type", "")
    prompt = f"人格：{sbti}\n素材：\n" + "\n".join(f"- {m.content}" for m in material)

    client = get_client("system")
    model = settings.effective_model
    tone = None
    if settings.realism_enabled:
        resp = await client.messages.create(
            model=model, max_tokens=250, system=DREAM_SYSTEM_JSON,
            messages=[{"role": "user", "content": prompt}],
        )
        await record_usage("dream", model=model, owner="system", response=resp)
        from app.llm.json_extract import extract_json_object
        data = extract_json_object(_extract_text(resp).strip()) or {}
        content = str(data.get("dream") or "").strip()[:120]
        t = data.get("tone")
        tone = t if t in ("positive", "neutral", "negative") else "neutral"
    else:
        resp = await client.messages.create(
            model=model, max_tokens=200, system=DREAM_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        content = _extract_text(resp).strip()[:120]
        await record_usage("dream", model=model, owner="system", response=resp)
    if not content:
        return None

    dream = await MemoryService(db).add_memory(
        resident.id, "dream", content, importance=0.4, source="reflection",
        metadata_json={"date": today_start.date().isoformat(), "involves_user_id": involves},
    )

    # Realism P1-11: a dream's tone nudges mood ±0.1 (emotion-loop input).
    if tone in ("positive", "negative"):
        delta = settings.realism_dream_tone_delta if tone == "positive" else -settings.realism_dream_tone_delta
        try:
            from app.services.mood_service import apply_mood_event
            await apply_mood_event(db, resident, delta, 0.0)
        except Exception:
            logger.warning("dream tone mood write-back failed", exc_info=True)

    if involves:
        try:
            await emit(db, "dream_generated", user_id=involves, resident_id=resident.id)
            from app.services.notification_service import notify
            await notify(db, involves, "system", "有人梦到了你", f"{resident.name} 昨晚梦到了你。", {"resident_slug": resident.slug})
        except Exception:
            logger.warning("dream_generated side effects failed", exc_info=True)
    return dream


async def run_nightly_dreams() -> int:
    """Generate dreams for eligible residents, up to the global cap. Own session."""
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    made = 0
    async with async_session() as db:
        eligible_ids = (await db.execute(
            select(Memory.resident_id).where(Memory.created_at >= today_start)
            .group_by(Memory.resident_id).having(func.count() >= MIN_MEMORIES)
        )).scalars().all()
        random.shuffle(list(eligible_ids))
        for rid in eligible_ids:
            if made >= GLOBAL_NIGHTLY_CAP:
                break
            resident = await db.get(Resident, rid)
            if resident is None:
                continue
            if await generate_dream(db, resident) is not None:
                made += 1
    return made


async def get_recent_dream(db, resident_id: str) -> str | None:
    """The resident's most recent dream within the last 24h (for dialogue)."""
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    return (await db.execute(
        select(Memory.content).where(
            Memory.resident_id == resident_id, Memory.type == "dream", Memory.created_at >= cutoff,
        ).order_by(Memory.created_at.desc()).limit(1)
    )).scalar_one_or_none()
