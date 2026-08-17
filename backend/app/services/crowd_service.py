"""P2 §8.2 — crowd / 人流聚集. Pure rule, zero LLM.

Generic festival crowd effects are gated by ``REALISM_CROWD_ENABLED``. Durable
caravan visitor assignments reuse the routing helpers but are lifecycle gameplay,
so their directed walk remains active independently of that cosmetic gate.

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
from app.agent.location_caps import CAP_STAGE
from app.agent.map_data import (
    LOCATIONS, get_location_id_at, location_capabilities,
)
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
_market_cohort_cache: dict[
    tuple[str, str, str, bool], tuple[float, frozenset[str]]
] = {}
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


def _stable_rank(parts: tuple[str, ...], resident_id: str) -> bytes:
    """每个 (事件, 居民) 对的确定性排序材料。

    集市与舞台两条 cohort 共用同一条哈希规则:同一场事件在任何进程、任何重启后都选
    出同一批人(名单不能随 PYTHONHASHSEED 漂),这是 cohort 能被缓存与复算的前提。
    """
    material = "\x1f".join((*parts, resident_id)).encode("utf-8")
    return hashlib.sha256(material).digest()


def _stable_market_rank(event_key: tuple[str, str, str], resident_id: str) -> bytes:
    return _stable_rank(event_key, resident_id)


# ── P2 #9 舞台事件 (stage event) ──────────────────────────────────────
# 「哪场事件算演出」= 事件类型 ∈ _EVENT_TYPES_WITH_CROWD;「演在哪」= 事件 payload
# 指的那栋楼自己声明了 stage 能力。场地资格**不看在场人数、也不看 slug 字面量** ——
# 这正是 design_P2.md §③ 路 B「地点吸引力与在场人数解耦」的机器表述:拉力全部来自
# 事件 + 能力声明,actions.py 的 CHAT_RESIDENT 判据一个字不改。


def _stage_venue_is_reachable(venue: str) -> bool:
    """该场地的目的地 tile 是否与镇区连通。

    必须用 get_reachable_tiles 而不是 get_walkable_tiles:后者被
    pathfinder._get_forced_walkable(:60-68)无边界检查地塞进每个地点的 entrance 与
    center,会自证成功(实测 theater center(175,45)walkable=True / reachable=False)。
    孤岛场地一旦被认下,名单里的人会每 tick 走一条 find_path 恒返 None 的路线 ——
    arrivals 永远 0,而每 tick 照吃一格日行动 cap。

    fail-**closed**:探测异常时返回 False(不认场地、不导流)。宁可少一场戏,也不能把
    人往走不到的地方赶。剧院 bounds 越界的修复归 P3-c(独立迁移批次),本函数是它落地
    前的自保,不是它的替代品。
    """
    from app.agent.map_data import get_valid_target_tile
    from app.agent.pathfinder import get_reachable_tiles
    try:
        tile = get_valid_target_tile(venue)
        if not tile:
            return False
        return (int(tile[0]), int(tile[1])) in get_reachable_tiles()
    except Exception:
        logger.warning("stage venue reachability probe failed: %s", venue,
                       exc_info=True)
        return False


def _active_stage_event_key(world_events) -> tuple[str, str, str, str] | None:
    """活跃舞台事件的稳定缓存/选人键 (marker, starts, ends, venue)。

    venue 进键:同一栋楼换一场戏要重开名单,同一场戏挪了地方也要重开。
    """
    for event in world_events or []:
        if event.get("type") not in _EVENT_TYPES_WITH_CROWD:
            continue
        venue = resolve_event_location_id(event.get("payload_json"))
        if not isinstance(venue, str) or venue not in LOCATIONS:
            continue
        if CAP_STAGE not in location_capabilities(venue):
            continue
        if not _stage_venue_is_reachable(venue):
            logger.debug("stage event venue is not reachable, ignored: %s", venue)
            continue
        # id 有就以 id 为准;starts/ends 区分畸形或合成夹具,并让改期的同一行成为新名单。
        marker = event.get("id") or event.get("title") or "stage_event"
        return (str(marker), str(event.get("starts_at") or ""),
                str(event.get("ends_at") or ""), venue)
    return None


def stage_event_venue(world_events) -> str | None:
    """正在演出的场地 id;没有合格演出则 None。纯函数、零查询。"""
    key = _active_stage_event_key(world_events)
    return key[3] if key is not None else None


def active_stage_event_id(world_events) -> str | None:
    """该场演出的公开身份(用于日志与归因)。"""
    key = _active_stage_event_key(world_events)
    return key[0] if key is not None else None


def stage_venue_at(x: int, y: int) -> str | None:
    """居民此刻脚下、且声明了 stage 能力的地点 id;不在任何场地里则 None。

    用 capability_location_at 而**不是** get_location_id_at:后者首命中即返,命中序 =
    dict 插入序 = 静态在前、动态追加在尾,而 theater(172,40,178,50)完全落在 outdoor
    街区 east_gardens(140,35,179,58)内部 —— 实测 get_location_id_at(174,45) 返
    "east_gardens"。照它写,人站在剧院正中也判不出「已在场」,于是每 tick 都会被再拉
    一次。
    """
    from app.agent.map_data import capability_location_at
    return capability_location_at(x or 0, y or 0, CAP_STAGE)


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
    persisted_only: bool = False,
) -> frozenset[str]:
    """Return at most four deterministic residents invited toward the hall.

    This selects *real* awake autonomous residents; movement remains the normal
    ``VISIT_DISTRICT`` path.  The short process-local cache plus a single-flight
    lock prevents one resident query per concurrently executing tick. Active
    trips and urgent behavior are protected later by the decide-phase ordering.
    ``persisted_only`` disables the decorative fallback cohort so lifecycle
    callers cannot invent buyers that lack a durable visitor assignment.
    """
    event_key = _active_market_day_key(world_events)
    if event_key is None:
        return frozenset()
    cache_key = (*event_key, bool(persisted_only))

    now = time.monotonic()
    cached = _market_cohort_cache.get(cache_key)
    if cached is not None and ttl > 0 and now - cached[0] < ttl:
        return cached[1]

    async with _market_cohort_lock:
        now = time.monotonic()
        cached = _market_cohort_cache.get(cache_key)
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
                _market_cohort_cache[cache_key] = (now, persisted)
                return persisted
            if persisted_only:
                chosen = frozenset()
                _market_cohort_cache[cache_key] = (now, chosen)
                return chosen

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

        _market_cohort_cache[cache_key] = (now, chosen)
        # Only active keys are useful; keep this tiny cache bounded if synthetic
        # events rotate ids unusually quickly.
        if len(_market_cohort_cache) > 8:
            oldest = min(_market_cohort_cache, key=lambda key: _market_cohort_cache[key][0])
            if oldest != cache_key:
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
