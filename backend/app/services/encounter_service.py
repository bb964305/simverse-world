"""B2 location encounters: entering a location may surface a nearby resident.

Hooked into the LocationTracker consumer (on location enter). Pure rule + a
probability roll (no LLM). Cooldown is in-memory (1h per user+location, ≤5/day).
"""

import random
import logging
from datetime import datetime, UTC

from sqlalchemy import select

from app.agent.map_data import get_location_by_id
from app.config import settings
from app.models.resident import Resident
from app.redis_client import get_redis
from app.ws.manager import manager

logger = logging.getLogger(__name__)

ENCOUNTER_COOLDOWN_SECONDS = 3600
ENCOUNTER_DAILY_CAP = 5
ENCOUNTER_BASE_PROB = 0.3
_DAILY_TTL_SECONDS = 2 * 86400


# Realism P0-5c: cooldown/daily count live in Redis (cross-worker, survives
# restart) instead of process-local dicts.
def _cooldown_key(user_id: str, location_id: str) -> str:
    return f"sv:enc_cd:{user_id}:{location_id}"


def _daily_key(user_id: str, today: str) -> str:
    return f"sv:enc_daily:{user_id}:{today}"

# Situational openers by location id; fallback used otherwise.
OPENERS: dict[str, str] = {
    "library": "{name}正埋头翻着一本旧书",
    "academy": "{name}正在专注地做着笔记",
    "tavern": "{name}端着一杯酒，看起来心情不错",
    "cafe": "{name}正悠闲地喝着咖啡",
    "workshop": "{name}正摆弄着手里的工具",
    "shop": "{name}正在整理货架",
    "town_hall": "{name}正低头处理着一些文件",
    "market_hall": "{name}正在集市大厅的摊位间挑选货物",
    "central_plaza": "{name}正好也在广场上",
}
DEFAULT_OPENER = "{name}正好也在这里"


def _reset_for_tests() -> None:  # pragma: no cover
    # Redis-backed now; fakeredis is installed fresh per test (conftest), so
    # there is no process-local state left to clear. Kept for call-site compat.
    pass


async def maybe_encounter(db, user_id: str, location_id: str, rng=random) -> dict | None:
    """Maybe surface an encounter with a nearby idle resident. Returns the payload if sent."""
    today = datetime.now(UTC).date().isoformat()
    r = get_redis()

    if await r.exists(_cooldown_key(user_id, location_id)):
        return None
    if int(await r.get(_daily_key(user_id, today)) or 0) >= ENCOUNTER_DAILY_CAP:
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

    # Duty system: an explorer-type resident (好奇心过载的学生) on site makes
    # chance encounters likelier — highest encounter_multiplier present applies.
    # The 0.95 cap only bounds the multiplied surplus and never lowers the base,
    # so tests/ops that pin ENCOUNTER_BASE_PROB = 1.0 stay deterministic.
    from app.services.duty_service import max_perk as _duty_max_perk
    multiplier = _duty_max_perk(residents, "encounter_multiplier", 1.0)
    prob = max(ENCOUNTER_BASE_PROB, min(0.95, ENCOUNTER_BASE_PROB * multiplier))
    if rng.random() >= prob:
        return None

    # P2-3: familiar residents are likelier to "happen to" be the one you run
    # into — weight 1 + coef×familiarity(player, resident). Uniform when off.
    if settings.realism_relations_enabled:
        from app.services import relation_service
        rels = await relation_service.relations_for(db, user_id, party_type="player")
        coef = settings.realism_rel_encounter_fam_coef

        def _w(r):
            v = rels.get(r.id)
            return 1.0 + coef * (v.familiarity if v else 0.0)

        resident = relation_service.weighted_pick(residents, _w, rng)
    else:
        resident = rng.choice(residents)
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

    await r.set(_cooldown_key(user_id, location_id), "1", ex=ENCOUNTER_COOLDOWN_SECONDS)
    cnt = await r.incr(_daily_key(user_id, today))
    if cnt == 1:  # first encounter today — set the midnight-ish cleanup TTL once
        await r.expire(_daily_key(user_id, today), _DAILY_TTL_SECONDS)
    return payload
