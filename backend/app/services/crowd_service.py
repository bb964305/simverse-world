"""P2 §8.2 — crowd / 人流聚集. Pure rule, zero LLM, gated by REALISM_CROWD_ENABLED.

Two effects that make an event day *look* different on the map:
  (a) festival / script events pull residents to the event location — the event
      location gets a ×realism_festival_weight boost in the VISIT_DISTRICT draw
      (this is the candidate-weight layer the P1 task-9 deviation lacked);
  (b) a herd micro-rule: an under-socialized resident who perceives an already
      lively spot (≥ threshold residents) gets a soft "那边好像很热闹" decide hint.

Location→resident-count is cached briefly so the herd check adds ~one query per
tick batch, not one per resident (perf red line).
"""
from __future__ import annotations

import time

from app.config import settings
from app.agent.map_data import LOCATIONS, get_location_id_at

_EVENT_TYPES_WITH_CROWD = ("festival", "script")

_counts_cache: dict = {"ts": -1e9, "data": {}}


def _reset_for_tests() -> None:  # pragma: no cover - test hook
    _counts_cache["ts"] = -1e9
    _counts_cache["data"] = {}


def active_event_location(world_events) -> str | None:
    """Location id of an active festival/script event: ``payload_json.location_id``
    if given, else the default festival gathering place. None if no such event."""
    for e in world_events or []:
        etype = e.get("type")
        if etype in _EVENT_TYPES_WITH_CROWD:
            loc = (e.get("payload_json") or {}).get("location_id")
            if not loc and etype == "festival":
                loc = settings.realism_festival_location
            if loc and loc in LOCATIONS:
                return loc
    return None


def festival_draw_target(world_events, current_loc, rng) -> str | None:
    """Weighted VISIT_DISTRICT draw over all locations, giving the active event
    location a ×realism_festival_weight boost. Returns that location *iff it wins
    the draw* — so the pull is a ×3 bias, not a hard force. None when there is no
    event, the resident is already there, or another location won the draw."""
    loc = active_event_location(world_events)
    if not loc or loc == current_loc:
        return None
    candidates = list(LOCATIONS.keys())
    w = settings.realism_festival_weight
    weights = [w if c == loc else 1.0 for c in candidates]
    pick = rng.choices(candidates, weights=weights, k=1)[0]
    return loc if pick == loc else None


async def location_resident_counts(db, ttl: float = 30.0) -> dict[str, int]:
    """Map of location_id → count of awake residents currently there. Cached for
    ``ttl`` seconds (residents move slowly; a soft hint tolerates staleness)."""
    now = time.monotonic()
    if ttl > 0 and (now - _counts_cache["ts"]) < ttl and _counts_cache["data"]:
        return _counts_cache["data"]
    from sqlalchemy import select
    from app.models.resident import Resident
    rows = (await db.execute(
        select(Resident.tile_x, Resident.tile_y).where(Resident.status != "sleeping")
    )).all()
    counts: dict[str, int] = {}
    for tx, ty in rows:
        lid = get_location_id_at(tx or 0, ty or 0)
        if lid:
            counts[lid] = counts.get(lid, 0) + 1
    _counts_cache["ts"] = now
    _counts_cache["data"] = counts
    return counts


def busiest_crowded_location(counts: dict[str, int], exclude: str | None = None) -> str | None:
    """The busiest location with ≥ realism_crowd_threshold residents, excluding
    ``exclude`` (usually where the resident already is). None if none qualifies."""
    best, best_n = None, 0
    for lid, n in counts.items():
        if lid == exclude:
            continue
        if n >= settings.realism_crowd_threshold and n > best_n:
            best, best_n = lid, n
    return best
