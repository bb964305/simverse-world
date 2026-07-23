"""E4 witness memories: a resident notices a nearby online player.

Pure rule (no LLM). perceive calls record_witnesses each tick; a short-TTL
snapshot of online player positions is shared across residents, and a per
(resident, player) 4h dedup keeps it from spamming. Each resident keeps at most
20 witness memories (oldest pruned).
"""

import time
import logging
from datetime import datetime, UTC

from sqlalchemy import select, delete, func

from app.config import settings
from app.database import async_session
from app.agent.map_data import get_location_at, get_location_id_at
from app.models.memory import Memory
from app.redis_client import get_redis
from app.services.location_tracker import pixel_to_tile
from app.ws.manager import manager

logger = logging.getLogger(__name__)

WITNESS_RADIUS_TILES = 10
WITNESS_DEDUP_SECONDS = 4 * 3600
MAX_WITNESS_PER_RESIDENT = 20
_SNAPSHOT_TTL = 5.0

# Realism P0-5c: the per-(resident, player) 4h dedup lives in Redis (SET NX EX =
# atomic check-and-set). The 5s online-player snapshot stays process-local (a
# hot per-round cache, not cross-worker cooldown state).
_snapshot: dict = {"ts": 0.0, "players": []}


def _witness_key(resident_id: str, user_id: str) -> str:
    return f"sv:witness:{resident_id}:{user_id}"


async def _players_snapshot() -> list[dict]:
    now = time.monotonic()
    if _snapshot["ts"] > 0 and now - _snapshot["ts"] < _SNAPSHOT_TTL:
        return _snapshot["players"]
    try:
        players = await manager.get_online_players()
    except Exception:
        return _snapshot["players"]
    _snapshot["ts"] = now
    _snapshot["players"] = players
    return players


def _situation(resident_home: str | None, player_tile: tuple[int, int]) -> str:
    if resident_home and get_location_id_at(*player_tile) == resident_home:
        return "在我家附近"
    return "路过"


async def record_witnesses(resident_id: str, tile_x: int, tile_y: int, home_location_id: str | None = None) -> int:
    """Record witness memories for online players near this resident. Returns count written."""
    players = await _players_snapshot()
    if not players:
        return 0

    loc = get_location_at(tile_x, tile_y)
    loc_name = loc["name"] if loc else "外面"
    r = get_redis()

    pending: list[tuple[str, str]] = []  # (user_id, content)
    for p in players:
        uid = p.get("player_id")
        if not uid:
            continue
        ptile = pixel_to_tile(p.get("x", 0), p.get("y", 0))
        if abs(ptile[0] - tile_x) + abs(ptile[1] - tile_y) > WITNESS_RADIUS_TILES:
            continue
        # SET NX EX: True iff not witnessed in the last 4h → atomic dedup mark.
        is_fresh = await r.set(_witness_key(resident_id, uid), "1",
                               ex=WITNESS_DEDUP_SECONDS, nx=True)
        if not is_fresh:
            continue
        phrase = _situation(home_location_id, ptile)
        content = f"在{loc_name}看到{p.get('name', '一位玩家')}{phrase}"
        pending.append((uid, content))

    if not pending:
        return 0

    async with async_session() as db:
        for uid, content in pending:
            db.add(Memory(
                resident_id=resident_id, type="event", content=content,
                importance=0.25, source="witness", related_user_id=uid,
            ))
        await db.commit()
        await _prune(db, resident_id)
        # Realism P2-2: witnessing a player nudges familiarity (+0.01). Rides the
        # existing (4h-deduped) witness event; no-op when the relations gate is off.
        if settings.realism_relations_enabled:
            try:
                from app.services import relation_service
                for uid, _ in pending:
                    await relation_service.bump(
                        db, resident_id, uid,
                        d_familiarity=settings.realism_rel_familiarity_witness,
                        type1="resident", type2="player",
                    )
            except Exception:
                logger.warning("witness relation bump failed", exc_info=True)
    return len(pending)


async def _prune(db, resident_id: str) -> None:
    count = (await db.execute(
        select(func.count()).select_from(Memory).where(
            Memory.resident_id == resident_id, Memory.source == "witness",
        )
    )).scalar() or 0
    if count <= MAX_WITNESS_PER_RESIDENT:
        return
    oldest = (await db.execute(
        select(Memory.id).where(
            Memory.resident_id == resident_id, Memory.source == "witness",
        ).order_by(Memory.created_at.asc()).limit(count - MAX_WITNESS_PER_RESIDENT)
    )).scalars().all()
    if oldest:
        await db.execute(delete(Memory).where(Memory.id.in_(oldest)))
        await db.commit()


def _reset_for_tests() -> None:  # pragma: no cover
    # Only the process-local snapshot remains; the dedup set is Redis-backed and
    # fakeredis is fresh per test (conftest).
    _snapshot["ts"] = 0.0
    _snapshot["players"] = []
