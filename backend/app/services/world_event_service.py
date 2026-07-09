"""World event service (S1): active-event cache + cron flip logic.

The active-event list is read on every agent tick and every player-dialogue
prompt build, so it is cached process-locally for 60s to avoid hammering the DB.
The cron invalidates the cache when it flips events so a fresh snapshot is picked
up within one tick of a transition.
"""

import time
import logging
from datetime import datetime, UTC

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.world_event import WorldEvent

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60.0
_cache: dict = {"ts": 0.0, "events": []}


def _to_dict(e: WorldEvent) -> dict:
    return {
        "id": e.id,
        "type": e.type,
        "title": e.title,
        "description": e.description,
        "payload_json": e.payload_json or {},
        "starts_at": e.starts_at.isoformat() if e.starts_at else None,
        "ends_at": e.ends_at.isoformat() if e.ends_at else None,
    }


def invalidate_active_cache() -> None:
    """Force the next get_active_events_cached call to re-query."""
    _cache["ts"] = 0.0


async def get_active_events_cached(db: AsyncSession) -> list[dict]:
    """Return active events as dicts, refreshing at most once per CACHE_TTL."""
    now = time.monotonic()
    if now - _cache["ts"] < CACHE_TTL_SECONDS and _cache["ts"] > 0.0:
        return _cache["events"]
    try:
        result = await db.execute(select(WorldEvent).where(WorldEvent.is_active.is_(True)))
        events = [_to_dict(e) for e in result.scalars().all()]
    except Exception:
        # Fail open: never let a query error break perceive/dialogue.
        logger.warning("world_event active query failed", exc_info=True)
        return _cache["events"]
    _cache["ts"] = now
    _cache["events"] = events
    return events


async def flip_active_events(db: AsyncSession) -> list[tuple[dict, str]]:
    """Turn events on/off based on the current time.

    Returns a list of (event_dict, phase) where phase is "start" or "end" for
    each event whose is_active flag changed this pass.
    """
    now = datetime.now(UTC)
    changes: list[tuple[dict, str]] = []

    result = await db.execute(select(WorldEvent))
    for e in result.scalars().all():
        # Compare naive/aware safely: normalize stored datetimes to aware UTC.
        starts = e.starts_at
        ends = e.ends_at
        if starts is not None and starts.tzinfo is None:
            starts = starts.replace(tzinfo=UTC)
        if ends is not None and ends.tzinfo is None:
            ends = ends.replace(tzinfo=UTC)

        should_be_active = (starts is not None and starts <= now) and (ends is not None and ends > now)
        if should_be_active and not e.is_active:
            e.is_active = True
            changes.append((_to_dict(e), "start"))
        elif not should_be_active and e.is_active:
            e.is_active = False
            changes.append((_to_dict(e), "end"))

    if changes:
        await db.commit()
        invalidate_active_cache()
    return changes


async def write_collective_memories(db: AsyncSession, event: dict) -> int:
    """A2: on a world event start, give every active resident a shared memory
    (batch, no LLM). 'active' = not sleeping. Returns count written."""
    from app.models.resident import Resident
    from app.models.memory import Memory

    residents = (await db.execute(
        select(Resident.id).where(Resident.status != "sleeping")
    )).scalars().all()
    content = (event.get("description") or event.get("title") or "")[:200]
    if not content or not residents:
        return 0
    for rid in residents:
        db.add(Memory(
            resident_id=rid, type="event", content=content,
            importance=0.5, source="world_event",
        ))
    await db.commit()
    return len(residents)
