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

    # C3: the active season's world-view patch rides along as a synthetic event
    # so it lands in every resident prompt without touching each consumer.
    try:
        wv = await _active_season_worldview(db)
        if wv:
            events = events + [wv]
    except Exception:
        logger.warning("season world-view injection failed", exc_info=True)
    _cache["ts"] = now
    _cache["events"] = events
    return events


async def _active_season_worldview(db: AsyncSession) -> dict | None:
    """C3: the active season's ``payload_json['world_view']`` as an event dict."""
    from app.models.season import Season

    season = (await db.execute(
        select(Season).where(Season.status == "active").order_by(Season.starts_at.desc())
    )).scalars().first()
    if season is None:
        return None
    wv = (season.payload_json or {}).get("world_view")
    if not wv:
        return None
    return {"id": f"season:{season.id}", "type": "season", "title": season.title,
            "description": wv, "payload_json": {}, "starts_at": None, "ends_at": None}


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


def _geo_relevant_residents(event: dict, rows) -> set[str]:
    """Resident ids currently within ``realism_info_geo_radius`` tiles of the
    event's location. The event's location comes from ``payload_json.location_id``
    (Manhattan distance to the location center). Empty when the event names no
    location. NOTE: location_visits is player-scoped, so the plan's "7-day
    visitors" signal is not available for residents — current position within
    radius is the resident geo-relevance signal (see PROGRESS P2-5 deviation)."""
    from app.config import settings
    from app.agent.map_data import get_location_by_id
    loc_id = (event.get("payload_json") or {}).get("location_id")
    if not loc_id:
        return set()
    loc = get_location_by_id(loc_id)
    if not loc:
        return set()
    center = loc.get("center")
    if not center:
        b = loc.get("bounds")
        if b:
            center = ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
    if not center:
        return set()
    radius = settings.realism_info_geo_radius
    cx, cy = center
    return {rid for rid, tx, ty in rows if abs((tx or 0) - cx) + abs((ty or 0) - cy) <= radius}


async def write_collective_memories(db: AsyncSession, event: dict, rng=None) -> int:
    """On a world event start, write first-hand event memories (batch, no LLM).

    Pre-P2 / gate off: every active (non-sleeping) resident gets one — the old
    all-knowing broadcast. With ``REALISM_INFO_GRADIENT_ENABLED`` on, non-weather
    events instead inform only (a) residents geographically near the event
    (importance 0.6) and (b) a random ``sample_frac`` "well-informed" sample of
    the rest (importance 0.5); everyone else learns it second-hand via gossip
    (§8.1). Weather stays all-broadcast ("抬头可见"). First-hand memories carry
    ``event_id`` in metadata so the diffusion probe + gossip can follow them.
    Returns the number of first-hand memories written."""
    import random as _random
    from app.config import settings
    from app.models.resident import Resident
    from app.models.memory import Memory

    rng = rng or _random
    rows = (await db.execute(
        select(Resident.id, Resident.tile_x, Resident.tile_y).where(Resident.status != "sleeping")
    )).all()
    content = (event.get("description") or event.get("title") or "")[:200]
    if not content or not rows:
        return 0
    event_id = event.get("id")
    is_weather = event.get("type") == "weather"

    def _meta():
        m = {"first_hand": True}
        if event_id:
            m["event_id"] = event_id
        return m

    if not settings.realism_info_gradient_enabled or is_weather:
        # All-knowing broadcast (pre-P2 path; weather keeps it — sky is visible).
        for rid, _, _ in rows:
            db.add(Memory(resident_id=rid, type="event", content=content,
                          importance=0.5, source="world_event", metadata_json=_meta()))
        await db.commit()
        return len(rows)

    # Information gradient: geo-related + a random well-informed sample.
    geo_ids = _geo_relevant_residents(event, rows)
    informed: dict[str, float] = {rid: settings.realism_info_geo_importance for rid in geo_ids}
    others = [rid for rid, _, _ in rows if rid not in geo_ids]
    k = round(settings.realism_info_sample_frac * len(others))
    for rid in rng.sample(others, min(k, len(others))):
        informed.setdefault(rid, settings.realism_info_sample_importance)

    for rid, imp in informed.items():
        db.add(Memory(resident_id=rid, type="event", content=content,
                      importance=imp, source="world_event", metadata_json=_meta()))
    await db.commit()
    return len(informed)
