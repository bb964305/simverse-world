"""B2 location encounters: entering a location may surface a nearby resident.

Hooked into the LocationTracker consumer (on location enter). Pure rule + a
probability roll (no LLM). Cooldown is in-memory (1h per user+location, ≤5/day).
"""

import time
import random
import logging
from datetime import datetime, UTC

from sqlalchemy import select

from app.agent.map_data import get_location_by_id
from app.models.resident import Resident
from app.ws.manager import manager

logger = logging.getLogger(__name__)

ENCOUNTER_COOLDOWN_SECONDS = 3600
ENCOUNTER_DAILY_CAP = 5
ENCOUNTER_BASE_PROB = 0.3

_cooldown: dict[tuple[str, str], float] = {}
_daily: dict[tuple[str, str], int] = {}

# Situational openers by location id; fallback used otherwise.
OPENERS: dict[str, str] = {
    "library": "{name}正埋头翻着一本旧书",
    "academy": "{name}正在专注地做着笔记",
    "tavern": "{name}端着一杯酒，看起来心情不错",
    "cafe": "{name}正悠闲地喝着咖啡",
    "workshop": "{name}正摆弄着手里的工具",
    "shop": "{name}正在整理货架",
    "town_hall": "{name}正低头处理着一些文件",
    "central_plaza": "{name}正好也在广场上",
}
DEFAULT_OPENER = "{name}正好也在这里"


def _reset_for_tests() -> None:  # pragma: no cover
    _cooldown.clear()
    _daily.clear()


async def maybe_encounter(db, user_id: str, location_id: str) -> dict | None:
    """Maybe surface an encounter with a nearby idle resident. Returns the payload if sent."""
    now = time.monotonic()
    today = datetime.now(UTC).date().isoformat()

    # Membership check, not a 0.0 default: time.monotonic() is seconds since
    # boot, so on a machine up < 1h the 0.0 default made the first encounter
    # look "on cooldown" and killed it (same bug as witness dedup, 45be03f).
    last = _cooldown.get((user_id, location_id))
    if last is not None and now - last < ENCOUNTER_COOLDOWN_SECONDS:
        return None
    if _daily.get((user_id, today), 0) >= ENCOUNTER_DAILY_CAP:
        return None

    loc = get_location_by_id(location_id)
    if not loc or not loc.get("bounds"):
        return None
    x1, y1, x2, y2 = loc["bounds"]

    residents = (await db.execute(
        select(Resident).where(
            Resident.status.in_(["idle", "walking"]),
            Resident.tile_x >= x1, Resident.tile_x <= x2,
            Resident.tile_y >= y1, Resident.tile_y <= y2,
        )
    )).scalars().all()
    if not residents:
        return None

    if random.random() >= ENCOUNTER_BASE_PROB:
        return None

    resident = random.choice(residents)
    opener = OPENERS.get(location_id, DEFAULT_OPENER).format(name=resident.name)
    payload = {
        "type": "encounter_prompt",
        "resident_slug": resident.slug,
        "resident_name": resident.name,
        "location_id": location_id,
        "opener": opener,
    }
    try:
        await manager.send(user_id, payload)
    except Exception:
        logger.warning("encounter_prompt send failed", exc_info=True)
        return None

    _cooldown[(user_id, location_id)] = now
    _daily[(user_id, today)] = _daily.get((user_id, today), 0) + 1
    return payload
