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

import asyncio
import hashlib
import logging
import time

from app.config import settings
from app.agent.map_data import LOCATIONS, get_location_id_at
from app.services.event_location import resolve_event_location_id

_EVENT_TYPES_WITH_CROWD = ("festival", "script")
_MARKET_HALL_ID = "market_hall"
MARKET_DAY_CROWD_LIMIT = 4
MARKET_DAY_COHORT_TTL_SECONDS = 20.0
# Collision-open visitor pockets on the hall's east side. They sit clear of
# the five-row loading envelope and the wagon anchor at (109, 94), so the
# visible crowd never blocks or overlaps the caravan centreline.
MARKET_DAY_VISITOR_TILES: tuple[tuple[int, int], ...] = (
    (114, 93), (116, 93), (114, 95), (116, 95),
)

_counts_cache: dict = {"ts": -1e9, "data": {}}
_market_cohort_cache: dict[tuple[str, str, str], tuple[float, frozenset[str]]] = {}
_market_cohort_lock = asyncio.Lock()

logger = logging.getLogger(__name__)


def invalidate_market_day_cohort(world_event_id: str) -> None:
    """Drop process-local cohort snapshots at lifecycle admission/close edges."""
    for key in list(_market_cohort_cache):
        if key[0] == str(world_event_id):
            _market_cohort_cache.pop(key, None)


def _reset_for_tests() -> None:  # pragma: no cover - test hook
    global _market_cohort_lock
    _counts_cache["ts"] = -1e9
    _counts_cache["data"] = {}
    _market_cohort_cache.clear()
    # Tests may use a fresh event loop per case. Production keeps one loop, but
    # replacing this test-only lock avoids retaining a lock from an old loop.
    _market_cohort_lock = asyncio.Lock()


def active_event_location(world_events) -> str | None:
    """Location id of an active festival/script event: ``payload_json.location_id``
    if given, else the default festival gathering place. None if no such event."""
    for e in world_events or []:
        etype = e.get("type")
        if etype in _EVENT_TYPES_WITH_CROWD:
            loc = resolve_event_location_id(e.get("payload_json"))
            if not loc and etype == "festival":
                loc = settings.realism_festival_location
            if loc and loc in LOCATIONS:
                return loc
    return None


def _active_market_day_key(world_events) -> tuple[str, str, str] | None:
    """Stable cache/selection key for an active market day at the market hall."""
    for event in world_events or []:
        if event.get("type") not in _EVENT_TYPES_WITH_CROWD:
            continue
        payload = event.get("payload_json") or {}
        if not bool(payload.get("market_day")):
            continue
        if resolve_event_location_id(payload) != _MARKET_HALL_ID:
            continue
        # An id is authoritative when present. starts/ends distinguish malformed
        # or synthetic fixtures and make a rescheduled row a new cohort.
        marker = event.get("id") or event.get("title") or "market_day"
        return (str(marker), str(event.get("starts_at") or ""), str(event.get("ends_at") or ""))
    return None


def active_market_day_id(world_events) -> str | None:
    """Public event identity used to bound a directed visitor trip."""
    key = _active_market_day_key(world_events)
    return key[0] if key is not None else None


def _stable_market_rank(event_key: tuple[str, str, str], resident_id: str) -> bytes:
    material = "\x1f".join((*event_key, resident_id)).encode("utf-8")
    return hashlib.sha256(material).digest()


def market_day_visitor_tile(
    resident_id: str,
    cohort: frozenset[str],
    world_events,
) -> tuple[int, int] | None:
    """Assign each selected visitor a stable, non-overlapping hall pocket."""
    event_key = _active_market_day_key(world_events)
    if event_key is None or resident_id not in cohort:
        return None
    # Assignment persistence uses the same reconstruction rule; no per-agent
    # database read is needed to recover a resident's unique slot.
    ordered = sorted(cohort)
    return MARKET_DAY_VISITOR_TILES[ordered.index(resident_id) % len(MARKET_DAY_VISITOR_TILES)]


async def market_day_crowd_cohort(
    db,
    world_events,
    *,
    ttl: float = MARKET_DAY_COHORT_TTL_SECONDS,
) -> frozenset[str]:
    """Return at most four deterministic residents invited toward the hall.

    This selects *real* awake autonomous residents; movement remains the normal
    ``VISIT_DISTRICT`` path.  The short process-local cache plus a single-flight
    lock prevents one resident query per concurrently executing tick. Active
    trips and urgent behavior are protected later by the decide-phase ordering.
    """
    event_key = _active_market_day_key(world_events)
    if event_key is None:
        return frozenset()

    now = time.monotonic()
    cached = _market_cohort_cache.get(event_key)
    if cached is not None and ttl > 0 and now - cached[0] < ttl:
        return cached[1]

    async with _market_cohort_lock:
        now = time.monotonic()
        cached = _market_cohort_cache.get(event_key)
        if cached is not None and ttl > 0 and now - cached[0] < ttl:
            return cached[1]

        try:
            # A lifecycle-backed visit owns the cohort once inbound begins.
            # This survives process restarts and an empty persisted cohort is
            # authoritative (it means fewer than one resident could really
            # afford an import), so do not replace it with decorative actors.
            # Keep this query in the same failure boundary as the fallback: a
            # database error may leave the transaction aborted, in which case
            # attempting a second query would only amplify the fault.
            from app.services.caravan_market_service import assigned_visitor_ids
            persisted = await assigned_visitor_ids(db, event_key[0])
            if persisted is not None:
                _market_cohort_cache[event_key] = (now, persisted)
                return persisted

            from sqlalchemy import select
            from app.models.resident import Resident

            rows = (await db.execute(
                select(Resident.id, Resident.tile_x, Resident.tile_y).where(
                    Resident.is_autonomous,
                    Resident.resident_type.in_(["npc", "resident"]),
                    Resident.status.not_in(["sleeping", "chatting", "socializing"]),
                )
            )).all()
            eligible = [
                str(resident_id)
                for resident_id, tile_x, tile_y in rows
                if get_location_id_at(tile_x or 0, tile_y or 0) != _MARKET_HALL_ID
            ]
            chosen = frozenset(sorted(
                eligible,
                key=lambda resident_id: _stable_market_rank(event_key, resident_id),
            )[:MARKET_DAY_CROWD_LIMIT])
        except Exception:
            # Fail open to the existing weighted festival draw and cache the
            # empty result briefly so a database outage is not amplified N-fold.
            logger.warning("market-day crowd cohort query failed", exc_info=True)
            chosen = frozenset()

        _market_cohort_cache[event_key] = (now, chosen)
        # Only active keys are useful; keep this tiny cache bounded if synthetic
        # events rotate ids unusually quickly.
        if len(_market_cohort_cache) > 8:
            oldest = min(_market_cohort_cache, key=lambda key: _market_cohort_cache[key][0])
            if oldest != event_key:
                _market_cohort_cache.pop(oldest, None)
        return chosen


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
