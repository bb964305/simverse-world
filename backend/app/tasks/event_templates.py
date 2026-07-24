"""A2: built-in world-event templates (holidays + news pool). Zero LLM.

`ensure_scheduled_events` runs daily (nightly_cron): it schedules upcoming
holidays (by month/day) and occasionally drops a random news event. All events
are created inactive; the S1 event_cron flips + broadcasts them on their window.
"""

import random
import logging
from datetime import datetime, date as date_type, timedelta, UTC
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import settings
from app.models.world_event import WorldEvent
from app.world_clock import now_world, world_to_real

logger = logging.getLogger(__name__)

# (month, day) -> (title, description, payload)
HOLIDAYS: dict[tuple[int, int], tuple[str, str, dict]] = {
    (1, 1): ("元旦", "新的一年开始了，小镇张灯结彩，居民们互道新年好。", {"ambience": "festive"}),
    (2, 14): ("情人节", "空气里弥漫着甜蜜，居民们更愿意谈论爱与陪伴。", {"ambience": "warm"}),
    (6, 1): ("儿童节", "小镇回到了童真时刻，大家玩起了小时候的游戏。", {"ambience": "playful"}),
    (9, 15): ("丰收节", "田里的作物成熟了，居民们分享着丰收的喜悦与感恩。", {"ambience": "harvest"}),
    (10, 31): ("万圣节", "南瓜灯亮起，居民们戴上面具，互相吓唬又互相请客。", {"ambience": "spooky"}),
    (12, 25): ("冬日庆典", "初雪落下，小镇点起暖光，居民们围炉夜话。", {"ambience": "cozy"}),
}

NEWS_POOL: list[tuple[str, str]] = [
    ("神秘旅人", "一位神秘的旅人经过小镇，带来了远方的传闻。"),
    ("流星雨", "昨夜有流星划过，居民们都在讨论许了什么愿。"),
    ("集市日", "广场上办起了临时集市，热闹非凡。"),
    ("旧物展", "图书馆展出了一批小镇的旧物，勾起许多回忆。"),
]

HOLIDAY_WINDOW_DAYS = 2  # holiday event lasts this many REAL days (player-visible continuity)
SCHEDULE_LOOKAHEAD_DAYS = 3
NEWS_PROBABILITY = 0.15  # ~1/week when run daily


def _world_day_real_start(world_day: date_type) -> datetime:
    """Real (UTC) anchor for a world calendar day's 00:00.

    World time (agent-T) decides *which day* an event is held on, but the active
    window is anchored to REAL time — the S1 event_cron flips ``is_active`` by
    comparing ``starts_at/ends_at`` to real-UTC now, and players should see a
    continuous activity window. So we take world-midnight, map it back to the
    real instant it occurs at, and store that (as UTC) as ``starts_at``."""
    zone = ZoneInfo(settings.timezone)
    world_midnight = datetime(world_day.year, world_day.month, world_day.day, tzinfo=zone)
    return world_to_real(world_midnight).astimezone(UTC)


async def _exists(db, title: str, start_real: datetime) -> bool:
    # Idempotency: a same-title event already anchored within the same real-hour
    # window (re-running for the same world day recomputes the identical anchor).
    lo = start_real - timedelta(hours=1)
    hi = start_real + timedelta(hours=1)
    row = (await db.execute(
        select(WorldEvent.id).where(
            WorldEvent.title == title,
            WorldEvent.starts_at >= lo,
            WorldEvent.starts_at < hi,
        ).limit(1)
    )).scalar_one_or_none()
    return row is not None


async def ensure_scheduled_events(db, today: date_type | None = None) -> int:
    """Schedule upcoming holidays (idempotent) + maybe a random news event.

    World time (agent-T): ``today`` and the lookahead iterate the WORLD calendar
    (which day to hold an event), while each event's active window is anchored to
    REAL time via ``_world_day_real_start`` (§5 seam). Returns created count."""
    today = today or now_world().date()
    created = 0
    announcements: list[tuple[str, str, date_type]] = []

    for offset in range(SCHEDULE_LOOKAHEAD_DAYS + 1):
        day = today + timedelta(days=offset)
        info = HOLIDAYS.get((day.month, day.day))
        if not info:
            continue
        title, desc, payload = info
        start = _world_day_real_start(day)
        if await _exists(db, title, start):
            continue
        db.add(WorldEvent(
            type="festival", title=title, description=desc, payload_json=payload,
            starts_at=start, ends_at=start + timedelta(days=HOLIDAY_WINDOW_DAYS), is_active=False,
        ))
        created += 1
        announcements.append((title, desc, day))

    # M1 F1.5: 集市日 — a weekly all-day festival at the plaza.摊贩 duties get a
    # halved WORK cooldown and the shop runs a discount that day. Weekday is read
    # on the WORLD calendar; the active window stays real-time.
    for offset in range(SCHEDULE_LOOKAHEAD_DAYS + 1):
        day = today + timedelta(days=offset)
        if day.weekday() != settings.market_day_weekday:
            continue
        title = "集市日"
        start = _world_day_real_start(day)
        if await _exists(db, title, start):
            continue
        db.add(WorldEvent(
            type="festival", title=title,
            description="广场上支起了摊子,居民们摆摊、赶集、讨价还价,热闹了一整天。",
            payload_json={"market_day": True, "location_id": "central_plaza", "ambience": "market"},
            starts_at=start, ends_at=start + timedelta(days=1), is_active=False,
        ))
        created += 1
        announcements.append((title, "本周集市日,欢迎各位摊主到中央广场出摊。", day))

    if random.random() < NEWS_PROBABILITY:
        title, desc = random.choice(NEWS_POOL)
        start = _world_day_real_start(today)
        if not await _exists(db, title, start):
            db.add(WorldEvent(
                type="news", title=title, description=desc, payload_json={},
                starts_at=start, ends_at=start + timedelta(days=1), is_active=False,
            ))
            created += 1

    if created:
        await db.commit()
        await _announce_events(db, announcements)
    return created


async def _announce_events(db, announcements: list[tuple[str, str, date_type]]) -> None:
    """Duty system: the town clerk (市政厅文书) posts an official bulletin for
    each newly scheduled festival. Best-effort — a bulletin failure must never
    break event scheduling; without a clerk resident, no post is made."""
    if not announcements:
        return
    try:
        from app.services.bulletin_service import create_post
        from app.services.duty_service import find_duty_resident

        clerk = await find_duty_resident(db, "town_clerk")
        if clerk is None:
            return
        for title, desc, day in announcements:
            await create_post(
                db, "notice",
                f"市政厅公告:{title}({day.month}月{day.day}日)",
                f"{desc}\n\n届时相关活动照章有序进行,请各位居民相互转告。——{clerk.name} 谨启",
                author_resident_id=clerk.id,
            )
    except Exception:
        logger.warning("clerk event announcement failed", exc_info=True)
