"""A2: built-in world-event templates (holidays + news pool). Zero LLM.

`ensure_scheduled_events` runs daily (nightly_cron): it schedules upcoming
holidays (by month/day) and occasionally drops a random news event. All events
are created inactive; the S1 event_cron flips + broadcasts them on their window.
"""

import random
import logging
from datetime import datetime, date as date_type, timedelta, UTC

from sqlalchemy import select

from app.models.world_event import WorldEvent

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

HOLIDAY_WINDOW_DAYS = 2  # holiday event lasts this many days
SCHEDULE_LOOKAHEAD_DAYS = 3
NEWS_PROBABILITY = 0.15  # ~1/week when run daily


async def _exists(db, title: str, starts_on: date_type) -> bool:
    day_start = datetime(starts_on.year, starts_on.month, starts_on.day, tzinfo=UTC)
    row = (await db.execute(
        select(WorldEvent.id).where(
            WorldEvent.title == title,
            WorldEvent.starts_at >= day_start,
            WorldEvent.starts_at < day_start + timedelta(days=1),
        ).limit(1)
    )).scalar_one_or_none()
    return row is not None


async def ensure_scheduled_events(db, today: date_type | None = None) -> int:
    """Schedule upcoming holidays (idempotent) + maybe a random news event. Returns created count."""
    today = today or datetime.now(UTC).date()
    created = 0

    for offset in range(SCHEDULE_LOOKAHEAD_DAYS + 1):
        day = today + timedelta(days=offset)
        info = HOLIDAYS.get((day.month, day.day))
        if not info:
            continue
        title, desc, payload = info
        if await _exists(db, title, day):
            continue
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        db.add(WorldEvent(
            type="festival", title=title, description=desc, payload_json=payload,
            starts_at=start, ends_at=start + timedelta(days=HOLIDAY_WINDOW_DAYS), is_active=False,
        ))
        created += 1

    if random.random() < NEWS_PROBABILITY:
        title, desc = random.choice(NEWS_POOL)
        if not await _exists(db, title, today):
            start = datetime(today.year, today.month, today.day, tzinfo=UTC)
            db.add(WorldEvent(
                type="news", title=title, description=desc, payload_json={},
                starts_at=start, ends_at=start + timedelta(days=1), is_active=False,
            ))
            created += 1

    if created:
        await db.commit()
    return created
