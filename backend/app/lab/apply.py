"""Apply engine + world-overlay reload (spec §4.6, §7).

Dispatches an approved WorldChangeProposal by ``kind`` into the overlay tables
(dynamic_locations / dynamic_mechanics) after structural + conflict validation
(bounds overlap / slug clash / spawn reachability). After a write it re-merges
the in-memory LOCATIONS in-process and publishes ``sv:world:reload`` so the
other processes (API / agent-worker / lab-runner) rebuild too — no redeploy.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, UTC

from sqlalchemy import select

from app.redis_client import get_redis
from app.models.dynamic_location import DynamicLocation
from app.models.dynamic_mechanic import DynamicMechanic
from app.models.world_change_proposal import WorldChangeProposal
from app.agent.map_data import LOCATIONS
from app.world_geometry import (
    MAP_HEIGHT_TILES, MAP_WIDTH_TILES, WALKABLE_X_RANGE, WALKABLE_Y_RANGE,
)

logger = logging.getLogger(__name__)

WORLD_RELOAD_CHANNEL = "sv:world:reload"


class ApplyError(Exception):
    """Structural/conflict validation failure (router maps to 400/409)."""


def _overlap(a: tuple, b: tuple) -> bool:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    return not (ax2 < bx1 or bx2 < ax1 or ay2 < by1 or by2 < ay1)


def _norm_bounds(raw) -> tuple[int, int, int, int] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        x1, y1, x2, y2 = (int(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return (x1, y1, x2, y2)


def validate_location_patch(
    patch: dict,
    *,
    allow_existing_slug: bool = False,
    outdoor_overlap_is_warning: bool = False,
    require_walkable_range: bool = False,
) -> tuple[list[str], list[str]]:
    """Structural + conflict checks for an add_location patch.

    Returns ``(errors, warnings)`` — empty ``errors`` = may be persisted.
    Checked against the *current* merged LOCATIONS (static + already-applied
    dynamic).

    ``outdoor_overlap_is_warning`` — an ``outdoor`` block is the ground, not a
    building: post_office sits inside south_quarter and theater inside
    east_gardens, and both are legitimate. With the downgrade on, the scan must
    NOT stop at the first hit: the six outdoor blocks are the last static
    entries (indices 28-33) and dynamic buildings are appended after them
    (map_data.py:386), so an early break would hide a real building clash.

    ``allow_existing_slug`` — the civic path is an upsert
    (civic_service.py:927 overwrites data_json for a known slug), so a known
    slug is neither fatal nor an overlap with itself.

    ``require_walkable_range`` — the pre-P3 range check only compared against
    MAP_WIDTH_TILES/MAP_HEIGHT_TILES, so theater (x2=178 < 180) passed while
    WALKABLE_X_RANGE tops out at 173. Off by default: the pre-P3 fixtures
    (tests/test_world_governance.py:36) legitimately sit outside that band.
    """
    errors: list[str] = []
    warnings: list[str] = []
    slug = patch.get("slug")
    data = patch.get("data") or {}
    if not slug or not isinstance(slug, str):
        errors.append("missing slug")
    elif slug in LOCATIONS and not allow_existing_slug:
        errors.append(f"slug '{slug}' already exists")

    bounds = _norm_bounds(data.get("bounds"))
    if bounds is None:
        errors.append("bounds must be [x1,y1,x2,y2] integers")
    else:
        x1, y1, x2, y2 = bounds
        if not (
            0 <= x1 <= x2 < MAP_WIDTH_TILES
            and 0 <= y1 <= y2 < MAP_HEIGHT_TILES
        ):
            errors.append(f"bounds out of map range or inverted: {bounds}")
        else:
            for other_slug, loc in LOCATIONS.items():
                if allow_existing_slug and other_slug == slug:
                    continue
                ob = _norm_bounds(loc.get("bounds"))
                if not (ob and _overlap(bounds, ob)):
                    continue
                if outdoor_overlap_is_warning and loc.get("type") == "outdoor":
                    warnings.append(
                        f"bounds sit inside outdoor block '{other_slug}'")
                    continue
                errors.append(f"bounds overlap existing location '{other_slug}'")
                if not outdoor_overlap_is_warning:
                    break
            # Spawn reachability heuristic: entrance must be inside the new bbox
            # and not swallowed by another location's bbox.
            entrance = data.get("entrance") or data.get("center")
            if entrance and len(entrance) == 2:
                ex, ey = int(entrance[0]), int(entrance[1])
                if not (x1 <= ex <= x2 and y1 <= ey <= y2):
                    errors.append("entrance/center must lie within bounds")
            if require_walkable_range:
                wx1, wx2 = min(WALKABLE_X_RANGE), max(WALKABLE_X_RANGE)
                wy1, wy2 = min(WALKABLE_Y_RANGE), max(WALKABLE_Y_RANGE)
                probes = [(x1, y1), (x2, y2)]
                ent = data.get("entrance") or data.get("center")
                if ent and len(ent) == 2:
                    probes.append((int(ent[0]), int(ent[1])))
                for px, py in probes:
                    if not (wx1 <= px <= wx2 and wy1 <= py <= wy2):
                        errors.append(
                            "bounds/entrance leave the walkable area "
                            f"[{wx1},{wx2}]x[{wy1},{wy2}]: ({px},{py})")
                        break
    if not data.get("name"):
        errors.append("missing name")
    return errors, warnings


def validate_add_location(patch: dict) -> list[str]:
    """Pre-P3 contract kept byte-for-byte: errors only, first overlap wins, a
    known slug is fatal. Callers: lab apply engine + admin proposal preview."""
    errors, _ = validate_location_patch(patch)
    return errors


async def _existing_dynamic_slug(db, slug: str) -> bool:
    row = await db.execute(select(DynamicLocation.id).where(DynamicLocation.slug == slug))
    return row.scalar_one_or_none() is not None


# ── dispatch ──────────────────────────────────────────────────────────

async def apply_proposal(db, proposal: WorldChangeProposal, *, broadcast: bool = True,
                         commit: bool = True) -> None:
    """Validate + write the overlay for an approved proposal. Raises ApplyError on
    a structural/conflict failure (the caller keeps the proposal un-applied).

    The per-kind ``_apply_*`` helpers only mutate + flush; whether the overlay is
    committed here is the caller's choice (recovery plan Phase 3):

    * ``commit=True`` (legacy / non-revisioned kinds) — commit the overlay, reload
      the world in-process, signal the other processes, and optionally broadcast.
      Preserves the pre-T6 behaviour byte-for-byte.
    * ``commit=False`` (revisioned kinds) — flush only and return. The caller
      (``proposal_service.approve_proposal``) then commits the overlay together
      with the immutable revision, the outbox record, and the proposal's terminal
      status in ONE transaction, and reloads/broadcasts AFTER that commit — so a
      crash before the commit leaves no visible overlay split from its audit.

    ``broadcast=False`` lets a caller send its own richer world_changed envelope
    afterward instead of the client seeing two pushes for one apply."""
    kind = proposal.kind
    patch = proposal.patch_json or {}
    if kind == "add_location":
        await _apply_add_location(db, proposal, patch)
    elif kind == "edit_location":
        await _apply_edit_location(db, proposal, patch)
    elif kind == "add_mechanic":
        await _apply_add_mechanic(db, proposal, patch)
    elif kind == "add_lore":
        await _apply_add_lore(db, proposal, patch)
    else:
        raise ApplyError(f"unsupported proposal kind '{kind}'")

    if not commit:
        return  # caller owns the atomic commit + reload + publish + broadcast

    await db.commit()
    await reload_world()
    await publish_world_reload()
    if broadcast:
        await broadcast_world_changed()


async def _apply_add_location(db, proposal, patch: dict) -> None:
    errors = validate_add_location(patch)
    if errors:
        raise ApplyError("; ".join(errors))
    slug = patch["slug"]
    if await _existing_dynamic_slug(db, slug):
        raise ApplyError(f"dynamic slug '{slug}' already recorded")
    db.add(DynamicLocation(slug=slug, data_json=patch["data"], active=True, proposal_id=proposal.id))
    await db.flush()


async def _apply_edit_location(db, proposal, patch: dict) -> None:
    """Edit a *dynamic* location's descriptive fields (name/description/
    boosted_actions). Static LOCATIONS are code-owned and not editable here."""
    slug = patch.get("slug")
    row = (await db.execute(select(DynamicLocation).where(DynamicLocation.slug == slug))).scalar_one_or_none()
    if row is None:
        raise ApplyError(f"edit_location target '{slug}' is not a dynamic location")
    data = dict(row.data_json or {})
    for field in ("name", "description", "boosted_actions", "role"):
        if field in (patch.get("data") or {}):
            data[field] = patch["data"][field]
    row.data_json = data
    await db.flush()


async def _apply_add_mechanic(db, proposal, patch: dict) -> None:
    code = patch.get("code") or f"mech:{uuid.uuid4().hex[:8]}"
    kind = patch.get("mechanic_kind") or "event"
    existing = (await db.execute(select(DynamicMechanic).where(DynamicMechanic.code == code))).scalar_one_or_none()
    if existing is not None:
        raise ApplyError(f"mechanic code '{code}' already exists")
    db.add(DynamicMechanic(code=code, kind=kind, spec_json=patch.get("spec") or {},
                           active=True, proposal_id=proposal.id))
    await db.flush()


async def _apply_add_lore(db, proposal, patch: dict) -> None:
    loc = patch.get("location_id") or (patch.get("data") or {}).get("location_id")
    text = patch.get("text") or (patch.get("data") or {}).get("text")
    if not loc or not text:
        raise ApplyError("add_lore requires location_id + text")
    code = f"lore:{loc}"
    existing = (await db.execute(select(DynamicMechanic).where(DynamicMechanic.code == code))).scalar_one_or_none()
    if existing is not None:
        existing.spec_json = {"location_id": loc, "text": text}
        existing.active = True
        existing.proposal_id = proposal.id  # re-attribute so revert targets this proposal
    else:
        db.add(DynamicMechanic(code=code, kind="lore", spec_json={"location_id": loc, "text": text},
                               active=True, proposal_id=proposal.id))
    await db.flush()


async def revert_proposal(db, proposal: WorldChangeProposal) -> None:
    """Soft-revert: deactivate every overlay row this proposal created, then
    reload. Audit rows are kept (active=False)."""
    locs = (await db.execute(
        select(DynamicLocation).where(DynamicLocation.proposal_id == proposal.id)
    )).scalars().all()
    for row in locs:
        row.active = False
    mechs = (await db.execute(
        select(DynamicMechanic).where(DynamicMechanic.proposal_id == proposal.id)
    )).scalars().all()
    for row in mechs:
        row.active = False
    await db.commit()
    await reload_world()
    await publish_world_reload()
    await broadcast_world_changed()


# ── reload orchestration ──────────────────────────────────────────────

async def reload_world() -> int:
    """Re-merge the overlay into memory + rebuild the tile index + refresh lore.
    Runs in whichever process received the signal (or applied the change)."""
    from app.agent.map_data import load_dynamic_locations
    from app.services import location_tracker

    n = await load_dynamic_locations()
    location_tracker.rebuild_lookup()
    try:
        from app.agent.location_lore import load_dynamic_lore
        await load_dynamic_lore()
    except Exception:
        logger.warning("dynamic lore reload failed", exc_info=True)
    return n


async def publish_world_reload() -> None:
    try:
        await get_redis().publish(WORLD_RELOAD_CHANNEL, "1")
    except Exception:
        logger.warning("world reload publish failed", exc_info=True)


async def broadcast_world_changed(payload: dict | None = None) -> None:
    """``payload`` given → broadcast the full canonical world_changed envelope
    (world_revision_service.world_changed_event); None → the pre-T6 bare ping
    (backward compatible for kinds that don't record a revision)."""
    try:
        from app.ws.manager import manager
        await manager.broadcast(payload if payload is not None else {"type": "world_changed"})
    except Exception:
        logger.warning("world_changed broadcast failed", exc_info=True)


async def world_reload_subscriber() -> None:
    """Long-lived task (one per process): reload the overlay when another process
    applies/reverts a proposal. Resilient to transient Redis errors."""
    while True:
        pubsub = None
        try:
            pubsub = get_redis().pubsub()
            await pubsub.subscribe(WORLD_RELOAD_CHANNEL)
            logger.info("world-reload subscriber listening on %s", WORLD_RELOAD_CHANNEL)
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    await reload_world()
                except Exception:
                    logger.warning("world reload failed", exc_info=True)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.warning("world-reload subscriber error; retrying", exc_info=True)
            await asyncio.sleep(1.0)
        finally:
            if pubsub is not None:
                try:
                    await pubsub.aclose()
                except Exception:
                    pass
