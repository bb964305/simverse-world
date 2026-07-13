"""B3 home decor: validation + full-replace writes + NPC notice easter egg.

Rules (FEATURE_SPECS §B3):
- only player residents (resident_type='player') decorate their own home
  (owner check — creator_id — lives in the router; 403 there, 400 here)
- at most ``DECOR_MAX_ITEMS`` items; x/y are tile offsets relative to the
  home_location_id bbox (map_data bounds) and must fall inside it
- every placed item must be covered by purchased qty (purchases aggregated
  per item_code, only kind='decor' catalog items count)
- PUT is a full replace; success broadcasts ``decor_updated`` (best-effort)

Deviation from spec: onboarding creates player residents with
``assign_housing=False`` (no home_location_id), so the first decor write
lazily claims a home. Unlike map_data.allocate_home (NPC occupancy only),
the player allocation counts *all* residents so two players don't land in
the same single-capacity house.

NPC easter egg: ``notice_decor_changes`` runs in the perceive phase
(witness_service pattern — own session, fail-open). A process-local cache
keeps the last seen decor hash per (observer, owner); the first sighting
only primes the cache, a later hash change writes one low-importance memory.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.map_data import assign_home, get_location_by_id, get_location_id_at
from app.database import async_session
from app.models.memory import Memory
from app.models.resident import Resident
from app.models.shop import Item, Purchase
from app.ws.manager import manager

logger = logging.getLogger(__name__)

DECOR_MAX_ITEMS = 12
VALID_ROTATIONS = (0, 90, 180, 270)
DECOR_MEMORY_IMPORTANCE = 0.3

# (observer_id, owner_id) -> (decor_hash, frozenset of item codes)
_decor_seen: dict[tuple[str, str], tuple[str, frozenset]] = {}


class DecorError(Exception):
    """Validation failure — surfaces as HTTP 400."""


def home_bounds(location_id: str | None) -> tuple[int, int, int, int] | None:
    loc = get_location_by_id(location_id) if location_id else None
    return loc["bounds"] if loc else None


def get_home_decor(resident: Resident) -> dict:
    """Public GET payload: current decor + the bbox the offsets are relative to."""
    bounds = home_bounds(resident.home_location_id)
    return {
        "resident_slug": resident.slug,
        "home_location_id": resident.home_location_id,
        "bounds": list(bounds) if bounds else None,
        "items": list(resident.home_decor_json or []),
    }


async def assign_player_home(db: AsyncSession) -> str | None:
    """First free housing slot counting ALL residents (players included)."""
    rows = await db.execute(
        select(Resident.home_location_id, func.count())
        .where(Resident.home_location_id.isnot(None))
        .group_by(Resident.home_location_id)
    )
    occupied = {row[0]: row[1] for row in rows.all()}
    return assign_home(occupied)


def _validate_items(items: list[dict], bounds: tuple[int, int, int, int]) -> None:
    if len(items) > DECOR_MAX_ITEMS:
        raise DecorError(f"最多摆放 {DECOR_MAX_ITEMS} 件装饰")
    x1, y1, x2, y2 = bounds
    max_dx, max_dy = x2 - x1, y2 - y1
    for it in items:
        code = it.get("item_code")
        if not code or not isinstance(code, str):
            raise DecorError("item_code 缺失")
        x, y = it.get("x"), it.get("y")
        if not isinstance(x, int) or not isinstance(y, int):
            raise DecorError("坐标必须是整数 tile 偏移")
        if not (0 <= x <= max_dx and 0 <= y <= max_dy):
            raise DecorError(f"坐标 ({x},{y}) 超出住房范围")
        if it.get("rot", 0) not in VALID_ROTATIONS:
            raise DecorError("rot 仅支持 0/90/180/270")


async def _check_purchased(db: AsyncSession, user_id: str, items: list[dict]) -> None:
    """Every placed code needs purchased qty >= placed count (decor items only)."""
    placed = Counter(it["item_code"] for it in items)
    if not placed:
        return
    rows = await db.execute(
        select(Purchase.item_code, func.sum(Purchase.qty))
        .join(Item, Item.code == Purchase.item_code)
        .where(
            Purchase.user_id == user_id,
            Item.kind == "decor",
            Purchase.item_code.in_(placed.keys()),
        )
        .group_by(Purchase.item_code)
    )
    owned = {code: int(qty or 0) for code, qty in rows.all()}
    for code, need in placed.items():
        if owned.get(code, 0) < need:
            raise DecorError(f"{code} 未购买或数量不足（已购 {owned.get(code, 0)}，需 {need}）")


async def set_home_decor(
    db: AsyncSession, resident: Resident, user_id: str, items: list[dict]
) -> dict:
    """Full-replace write. Raises DecorError on any validation failure."""
    if resident.home_location_id is None:
        home = await assign_player_home(db)
        if home is None:
            raise DecorError("全镇住房已满，暂时无法认领住房")
        resident.home_location_id = home

    bounds = home_bounds(resident.home_location_id)
    if bounds is None:
        raise DecorError("住房位置无效")

    _validate_items(items, bounds)
    await _check_purchased(db, user_id, items)

    normalized = [
        {"item_code": it["item_code"], "x": it["x"], "y": it["y"], "rot": it.get("rot", 0)}
        for it in items
    ]
    resident.home_decor_json = normalized
    await db.commit()

    try:
        await manager.broadcast({
            "type": "decor_updated",
            "resident_slug": resident.slug,
            "home_location_id": resident.home_location_id,
            "decor": normalized,
        })
    except Exception:
        logger.warning("decor_updated broadcast failed for %s", resident.slug, exc_info=True)

    return get_home_decor(resident)


# ── NPC easter egg (perceive phase) ─────────────────────────────────────

def _decor_hash(decor: list) -> str:
    return hashlib.md5(json.dumps(decor, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def notice_decor_changes(observer_id: str, tile_x: int, tile_y: int) -> int:
    """A resident standing in a housing location notices redecorated homes.

    Own session, fail-open at the call site. First sighting primes the
    process-local hash cache; a subsequent hash change writes one
    low-importance event memory. Returns memories written.
    """
    loc_id = get_location_id_at(tile_x, tile_y)
    loc = get_location_by_id(loc_id) if loc_id else None
    if not loc or loc.get("type") not in ("private", "apartment"):
        return 0

    written = 0
    async with async_session() as db:
        owners = (await db.execute(
            select(Resident).where(
                Resident.home_location_id == loc_id,
                Resident.home_decor_json.isnot(None),
                Resident.id != observer_id,
            )
        )).scalars().all()

        for owner in owners:
            decor = owner.home_decor_json or []
            digest = _decor_hash(decor)
            codes = frozenset(it.get("item_code", "") for it in decor)
            key = (observer_id, owner.id)
            prev = _decor_seen.get(key)
            _decor_seen[key] = (digest, codes)
            if prev is None or prev[0] == digest:
                continue  # first sighting primes; unchanged is unchanged

            added = codes - prev[1]
            flavor = "家里的布置变了样"
            if added:
                name = (await db.execute(
                    select(Item.name).where(Item.code == next(iter(added)))
                )).scalar_one_or_none()
                if name:
                    flavor = f"家里新添了{name}"
            db.add(Memory(
                resident_id=observer_id,
                type="event",
                content=f"路过{owner.name}的家，发现{flavor}",
                importance=DECOR_MEMORY_IMPORTANCE,
                source="decor",
                related_resident_id=owner.id,
            ))
            written += 1

        if written:
            await db.commit()
    return written


def _reset_for_tests() -> None:  # pragma: no cover
    _decor_seen.clear()
