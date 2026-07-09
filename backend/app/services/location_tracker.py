"""LocationTracker (S5).

The move message is a hot path, so ``on_move`` is pure in-memory: it maps the
tile to a named location via a precomputed O(1) table and, only when the user
crosses into a *new* location, enqueues a visit for a background consumer. The
consumer upserts the visit row off the hot path and emits location_first_visit
exactly once (DB-authoritative, so a reconnect to another worker can't double it).
"""

import asyncio
import logging
from datetime import datetime, UTC

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent.map_data import LOCATIONS
from app.database import async_session
from app.events.bus import emit
from app.models.location_visit import LocationVisit

logger = logging.getLogger(__name__)

TILE_SIZE = 32

# Precomputed tile -> location id (first-match wins, matching get_location_id_at
# iteration order over LOCATIONS).
_tile_to_location: dict[tuple[int, int], str] = {}


def _build_lookup() -> None:
    _tile_to_location.clear()
    for loc_id, loc in LOCATIONS.items():
        bounds = loc.get("bounds")
        if not bounds:
            continue
        x1, y1, x2, y2 = bounds
        for x in range(x1, x2 + 1):
            for y in range(y1, y2 + 1):
                _tile_to_location.setdefault((x, y), loc_id)


_build_lookup()

# Per-user last known location (in-memory, this worker only).
_last_location: dict[str, str | None] = {}
_secret_seen: set[tuple[str, str]] = set()  # (user_id, location_id) hidden spots already triggered
_queue: asyncio.Queue = asyncio.Queue(maxsize=10000)


def location_at_tile(tile_x: int, tile_y: int) -> str | None:
    return _tile_to_location.get((tile_x, tile_y))


def pixel_to_tile(x: float, y: float) -> tuple[int, int]:
    return int(x // TILE_SIZE), int(y // TILE_SIZE)


def on_move(user_id: str, tile_x: int, tile_y: int) -> None:
    """Pure in-memory: enqueue a visit only when entering a new named location."""
    loc = _tile_to_location.get((tile_x, tile_y))
    if loc is None:
        # Left into open world — reset so re-entering re-triggers.
        _last_location[user_id] = None
        return
    # E8: exact hidden-spot hit → enqueue a one-time secret visit.
    from app.agent.location_lore import SECRET_TILE_TO_LOCATION
    secret_loc = SECRET_TILE_TO_LOCATION.get((tile_x, tile_y))
    if secret_loc is not None and (user_id, secret_loc) not in _secret_seen:
        _secret_seen.add((user_id, secret_loc))
        try:
            _queue.put_nowait((user_id, f"{secret_loc}:secret"))
        except asyncio.QueueFull:  # pragma: no cover
            pass

    if _last_location.get(user_id) == loc:
        return  # unchanged — hot path returns immediately
    _last_location[user_id] = loc
    try:
        _queue.put_nowait((user_id, loc))
    except asyncio.QueueFull:  # pragma: no cover - overload guard
        logger.warning("location visit queue full; dropping %s@%s", user_id, loc)


async def _record_visit(db, user_id: str, location_id: str) -> bool:
    """Upsert one visit. Returns True if this was the first visit."""
    now = datetime.now(UTC)
    existing = (await db.execute(
        select(LocationVisit).where(
            LocationVisit.user_id == user_id, LocationVisit.location_id == location_id,
        )
    )).scalar_one_or_none()

    if existing is None:
        db.add(LocationVisit(
            user_id=user_id, location_id=location_id,
            visit_count=1, first_visited_at=now, last_visited_at=now,
        ))
        try:
            await db.commit()
            return True
        except IntegrityError:
            # Race: another insert won — fall through to increment.
            await db.rollback()
            existing = (await db.execute(
                select(LocationVisit).where(
                    LocationVisit.user_id == user_id, LocationVisit.location_id == location_id,
                )
            )).scalar_one_or_none()

    if existing is not None:
        existing.visit_count += 1
        existing.last_visited_at = now
        await db.commit()
    return False


async def _distinct_location_count(db, user_id: str) -> int:
    from sqlalchemy import func
    rows = (await db.execute(
        select(LocationVisit.location_id).where(LocationVisit.user_id == user_id)
    )).scalars().all()
    return len({loc for loc in rows if not loc.endswith(":secret")})


async def process_one(user_id: str, location_id: str) -> None:
    """Consume a single queued visit (also used directly in tests)."""
    async with async_session() as db:
        first = await _record_visit(db, user_id, location_id)

        # E8: a hidden-spot secret visit — reward + codex star on first find only.
        if location_id.endswith(":secret"):
            if first:
                try:
                    from app.agent.location_lore import SECRET_REWARD_SC
                    from app.services.coin_service import reward
                    from app.services.notification_service import notify
                    await reward(db, user_id, SECRET_REWARD_SC, f"secret:{location_id}")
                    await notify(db, user_id, "system", "发现隐藏地点！",
                                 f"你发现了一处隐藏角落，获得 {SECRET_REWARD_SC} 🪙", {"location_id": location_id})
                except Exception:
                    logger.warning("secret reward failed for %s", location_id, exc_info=True)
            return

        if first:
            await emit(db, "location_first_visit", user_id=user_id, location_id=location_id)
            # E8: show this location's lore + codex progress on first visit.
            try:
                from app.agent.location_lore import lore_for
                from app.agent.map_data import LOCATIONS
                from app.services.notification_service import notify
                lore = lore_for(location_id)
                if lore:
                    total = len([l for l in LOCATIONS.values() if l.get("bounds")])
                    n = await _distinct_location_count(db, user_id)
                    await notify(db, user_id, "system", f"新发现：{LOCATIONS.get(location_id, {}).get('name', location_id)}",
                                 lore, {"location_id": location_id, "progress": f"{n}/{total}"})
            except Exception:
                logger.warning("lore notify failed for %s", location_id, exc_info=True)
        # B2: entering a location may surface an encounter with a nearby resident.
        try:
            from app.services.encounter_service import maybe_encounter
            await maybe_encounter(db, user_id, location_id)
        except Exception:
            logger.warning("encounter check failed for %s@%s", user_id, location_id, exc_info=True)
        # B1: entering a location may complete a visit_location commission.
        try:
            from app.services.commission_service import check_visit_commissions
            await check_visit_commissions(db, user_id, location_id)
        except Exception:
            logger.warning("commission visit check failed for %s@%s", user_id, location_id, exc_info=True)


async def location_consumer_loop() -> None:
    """Background consumer — drains the visit queue. Runs on every API worker."""
    while True:
        user_id, location_id = await _queue.get()
        try:
            await process_one(user_id, location_id)
        except Exception:
            logger.warning("location visit record failed for %s@%s", user_id, location_id, exc_info=True)
        finally:
            _queue.task_done()


def forget_user(user_id: str) -> None:
    """Drop a user's in-memory last-location (e.g., on disconnect)."""
    _last_location.pop(user_id, None)
