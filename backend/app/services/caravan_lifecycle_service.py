"""Restart-safe caravan state machine and its REST/WS snapshot contract."""
from __future__ import annotations

import logging
import math
import socket
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.caravan_visit import (
    CARAVAN_ACTIVE_PHASES,
    CARAVAN_VISIBLE_PHASES,
    CaravanVisit,
)
from app.models.world_event import WorldEvent
from app.services import caravan_service, treasury_service

logger = logging.getLogger(__name__)

EMPTY_SUMMARY = {
    "fee_sc": 0,
    "bought": 0,
    "spent_sc": 0,
    "tax_sc": 0,
    "imports_stocked": 0,
}
ADMISSION_PENDING_ERROR = "admission_pending"


def worker_owner() -> str:
    return f"{socket.gethostname()}:{uuid.uuid4()}"


def _route_plan():
    """Resolve lazily so a route-data fault cannot prevent API import/startup."""
    from app.services.caravan_route import build_caravan_route

    return build_caravan_route()


def _aware(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _event_value(event: WorldEvent | dict, name: str):
    return event.get(name) if isinstance(event, dict) else getattr(event, name, None)


def _is_market_event(event: WorldEvent | dict) -> bool:
    return bool((_event_value(event, "payload_json") or {}).get("market_day"))


def empty_snapshot(*, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    return {
        "type": "caravan_state",
        "visit_id": None,
        "world_event_id": None,
        "version": 0,
        "phase": None,
        "server_time": now.isoformat(),
        "position": None,
        "motion": None,
        "summary": dict(EMPTY_SUMMARY),
        "visible": False,
    }


def serialize_visit(visit: CaravanVisit, *, now: datetime | None = None) -> dict:
    """The single full-snapshot protocol used by both REST and WebSocket."""
    now = now or datetime.now(UTC)
    route = visit.route_json or []
    motion = None
    if (visit.phase in {"inbound", "outbound"} and route
            and visit.motion_started_at and visit.motion_ends_at):
        motion = {
            "path": [[int(point[0]), int(point[1])] for point in route],
            "started_at": _aware(visit.motion_started_at).isoformat()
            if visit.motion_started_at else None,
            "ends_at": _aware(visit.motion_ends_at).isoformat()
            if visit.motion_ends_at else None,
        }
    return {
        "type": "caravan_state",
        "visit_id": visit.id,
        "world_event_id": visit.world_event_id,
        "version": int(visit.version),
        "phase": visit.phase,
        "server_time": now.isoformat(),
        "position": {"tile_x": int(visit.tile_x), "tile_y": int(visit.tile_y)},
        "motion": motion,
        "summary": {
            key: int((visit.summary_json or {}).get(key, default))
            for key, default in EMPTY_SUMMARY.items()
        },
        "visible": visit.phase in CARAVAN_VISIBLE_PHASES,
    }


async def current_snapshot(db: AsyncSession, *, now: datetime | None = None) -> dict:
    """Return only the currently renderable visit.

    Future ``scheduled`` rows are deliberately invisible here: the look-ahead
    reconciler creates them up to two days early and they must never replace an
    already trading visit in a reconnect reducer. Terminal cleanup is delivered
    by WS; REST absence converges to the canonical empty snapshot.
    """
    visit = (await db.execute(
        select(CaravanVisit)
        .where(CaravanVisit.phase.in_(CARAVAN_VISIBLE_PHASES))
        .order_by(CaravanVisit.next_action_at.asc(), CaravanVisit.created_at.asc())
        .limit(1)
    )).scalars().first()
    return empty_snapshot(now=now) if visit is None else serialize_visit(visit, now=now)


async def ensure_visit_for_event(
    db: AsyncSession, event: WorldEvent | dict, *, now: datetime | None = None,
) -> CaravanVisit | None:
    """Idempotently enqueue one market event; never performs settlement inline."""
    if not settings.caravan_lifecycle_enabled or not _is_market_event(event):
        return None
    event_id = _event_value(event, "id")
    starts_at = _aware(_event_value(event, "starts_at"))
    ends_at = _aware(_event_value(event, "ends_at"))
    if not event_id or starts_at is None or ends_at is None or ends_at <= starts_at:
        logger.warning("caravan lifecycle skipped malformed market event %r", event_id)
        return None
    now = now or datetime.now(UTC)
    existing = (await db.execute(
        select(CaravanVisit).where(CaravanVisit.world_event_id == event_id)
    )).scalar_one_or_none()
    if existing is not None:
        return existing
    # Safe cutover in the other direction: a legacy one-shot may already have
    # settled this market before the lifecycle gate opened.  Persist a terminal
    # tombstone instead of replaying its unaudited money effects.
    legacy_settled = (
        await treasury_service.kv_read(db, caravan_service.LAST_VISIT_KEY) == event_id
    )
    try:
        outside = _route_plan().outside_staging
    except Exception:
        logger.exception("caravan route unavailable; refusing to enqueue %s", event_id)
        await db.rollback()
        return None
    due = max(starts_at - timedelta(seconds=settings.caravan_wait_lead_seconds), now)
    values = {
        "id": str(uuid.uuid4()),
        "world_event_id": event_id,
        "phase": "cancelled" if legacy_settled else "scheduled",
        "version": 1,
        "next_action_at": now if legacy_settled else due,
        "tile_x": int(outside[0]),
        "tile_y": int(outside[1]),
        "fee_sc": 0,
        "summary_json": {},
        "error_code": "legacy_already_settled" if legacy_settled else None,
        "departed_at": now if legacy_settled else None,
        "created_at": now,
        "updated_at": now,
    }
    dialect = db.get_bind().dialect.name
    if dialect in ("postgresql", "sqlite"):
        insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
        await db.execute(
            insert(CaravanVisit).values(**values).on_conflict_do_nothing(
                index_elements=[CaravanVisit.world_event_id]
            )
        )
    else:
        existing = (await db.execute(
            select(CaravanVisit.id).where(CaravanVisit.world_event_id == event_id)
        )).scalar_one_or_none()
        if existing is None:
            db.add(CaravanVisit(**values))
    await db.commit()
    return (await db.execute(
        select(CaravanVisit).where(CaravanVisit.world_event_id == event_id)
    )).scalar_one()


async def reconcile_market_events(
    db: AsyncSession, *, now: datetime | None = None,
) -> int:
    """Discover near-future events so a restart still creates the waiting visit."""
    if not settings.caravan_lifecycle_enabled:
        return 0
    now = now or datetime.now(UTC)
    rows = (await db.execute(
        select(WorldEvent).where(
            WorldEvent.ends_at > now - timedelta(days=1),
            WorldEvent.starts_at <= now + timedelta(days=2),
        ).order_by(WorldEvent.starts_at)
    )).scalars().all()
    count = 0
    for event in rows:
        if _is_market_event(event):
            await ensure_visit_for_event(db, event, now=now)
            count += 1
    admitted = await _admitted(db)
    if admitted:
        # Admission is deliberately reversible for the whole market window. A
        # row parked at opens/closes while policy was off carries this marker;
        # waking only marked rows avoids pulling ordinary two-day look-ahead
        # visits forward before their configured wait lead.
        await db.execute(
            update(CaravanVisit)
            .where(
                CaravanVisit.phase.in_(("scheduled", "waiting")),
                CaravanVisit.error_code == ADMISSION_PENDING_ERROR,
                CaravanVisit.next_action_at > now,
            )
            .values(next_action_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
    # A policy kill switch must not wait until a long motion/trading deadline.
    # Wake a newly-paused waiting row once so the next driver pass can park it,
    # or animate a safe outbound return from an in-progress route. Already
    # parked admission rows stay asleep until policy reopens or the window ends.
    else:
        await db.execute(
            update(CaravanVisit)
            .where(
                or_(
                    CaravanVisit.phase.in_(("inbound", "trading")),
                    and_(
                        CaravanVisit.phase == "waiting",
                        or_(
                            CaravanVisit.error_code.is_(None),
                            CaravanVisit.error_code != ADMISSION_PENDING_ERROR,
                        ),
                    ),
                ),
                CaravanVisit.next_action_at > now,
            )
            .values(next_action_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
    return count


async def wake_visit_for_event(
    db: AsyncSession, event: WorldEvent | dict, *, now: datetime | None = None,
) -> CaravanVisit | None:
    """Move a cron-triggered event transition to the front of the due queue."""
    if not settings.caravan_lifecycle_enabled or not _is_market_event(event):
        return None
    now = now or datetime.now(UTC)
    visit = await ensure_visit_for_event(db, event, now=now)
    if visit is None or visit.phase not in CARAVAN_ACTIVE_PHASES:
        return visit
    await db.execute(
        update(CaravanVisit)
        .where(
            CaravanVisit.id == visit.id,
            CaravanVisit.phase.in_(CARAVAN_ACTIVE_PHASES),
            CaravanVisit.next_action_at > now,
        )
        .values(next_action_at=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return await db.get(CaravanVisit, visit.id, populate_existing=True)


def _claimable(now: datetime):
    return and_(
        CaravanVisit.phase.in_(CARAVAN_ACTIVE_PHASES),
        CaravanVisit.next_action_at <= now,
        or_(
            CaravanVisit.lease_owner.is_(None),
            CaravanVisit.lease_expires_at.is_(None),
            CaravanVisit.lease_expires_at <= now,
        ),
    )


async def claim_next_visit(
    db: AsyncSession, *, owner: str, now: datetime,
) -> CaravanVisit | None:
    """CAS-claim one due row; expired leases provide automatic restart recovery."""
    for _ in range(5):
        candidate = (await db.execute(
            select(CaravanVisit.id, CaravanVisit.version)
            .where(_claimable(now))
            .order_by(CaravanVisit.next_action_at, CaravanVisit.created_at)
            .limit(1)
        )).first()
        if candidate is None:
            return None
        visit_id, version = candidate
        result = await db.execute(
            update(CaravanVisit)
            .where(
                CaravanVisit.id == visit_id,
                CaravanVisit.version == version,
                _claimable(now),
            )
            .values(
                lease_owner=owner,
                lease_expires_at=now + timedelta(seconds=settings.caravan_lease_seconds),
                version=CaravanVisit.version + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            continue
        await db.commit()
        return await db.get(CaravanVisit, visit_id, populate_existing=True)
    return None


async def _transition(
    db: AsyncSession, visit: CaravanVisit, owner: str, *, now: datetime, **values,
) -> CaravanVisit:
    values.setdefault("updated_at", now)
    values.setdefault("lease_owner", None)
    values.setdefault("lease_expires_at", None)
    target_phase = values.get("phase", visit.phase)
    values.setdefault(
        "visibility_slot",
        "world" if target_phase in CARAVAN_VISIBLE_PHASES else None,
    )
    visit_id = visit.id
    version = visit.version
    result = await db.execute(
        update(CaravanVisit)
        .where(
            CaravanVisit.id == visit_id,
            CaravanVisit.version == version,
            CaravanVisit.lease_owner == owner,
        )
        .values(version=CaravanVisit.version + 1, **values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise caravan_service.CaravanLeaseLost(visit_id)
    await db.commit()
    return await db.get(CaravanVisit, visit_id, populate_existing=True)


async def _admitted(db: AsyncSession) -> bool:
    return bool(
        settings.npc_economy_enabled
        and await caravan_service.is_caravan_enabled(db)
    )


async def _other_visible_visit_id(db: AsyncSession, visit_id: str) -> str | None:
    return (await db.execute(
        select(CaravanVisit.id).where(
            CaravanVisit.id != visit_id,
            CaravanVisit.phase.in_(CARAVAN_VISIBLE_PHASES),
        ).limit(1)
    )).scalar_one_or_none()


def _route() -> list[list[int]] | None:
    try:
        return [[int(x), int(y)] for x, y in _route_plan().full_path]
    except Exception:
        logger.exception("caravan route unavailable")
        return None


def _motion_end(now: datetime, route: list[list[int]]) -> datetime:
    seconds = max(
        1,
        math.ceil(max(1, len(route) - 1) * settings.caravan_route_tile_ms / 1000),
    )
    return now + timedelta(seconds=seconds)


async def _begin_inbound(
    db: AsyncSession, visit: CaravanVisit, owner: str, *, now: datetime,
    closes_at: datetime,
) -> CaravanVisit:
    other_visible = await _other_visible_visit_id(db, visit.id)
    if other_visible is not None:
        return await _transition(
            db, visit, owner, now=now, phase="cancelled", next_action_at=now,
            error_code="another_visit_visible", departed_at=now,
        )
    route = _route()
    if not route:
        return await _transition(
            db, visit, owner, now=now, phase="cancelled",
            next_action_at=now, error_code="route_unreachable", departed_at=now,
        )
    motion_end = _motion_end(now, route)
    # Fence the legacy one-shot path before the visible admission is committed.
    await treasury_service.kv_upsert_pending(
        db, caravan_service.LAST_VISIT_KEY, visit.world_event_id,
        updated_by="caravan_lifecycle",
    )
    visit_id = visit.id
    world_event_id = visit.world_event_id
    # Persist the real four-person invitation before the caravan becomes
    # visible.  The transition commit owns these rows, so reconnect/restart
    # cannot substitute a different decorative crowd midway through a visit.
    try:
        from app.services.caravan_market_service import ensure_market_visitors

        await ensure_market_visitors(db, visit.id, now=now)
    except Exception:
        # Visitor assignment is non-financial and fail-isolated: a malformed
        # roster must not strand the caravan outside or tear its state machine.
        logger.warning(
            "caravan market visitor assignment failed for %s", visit_id,
            exc_info=True,
        )
        await db.rollback()
        # Re-establish the legacy fence after rollback; _transition commits it
        # atomically with inbound visibility.
        await treasury_service.kv_upsert_pending(
            db, caravan_service.LAST_VISIT_KEY, world_event_id,
            updated_by="caravan_lifecycle",
        )
        visit = await db.get(CaravanVisit, visit_id, populate_existing=True)
    from app.services import crowd_service

    crowd_service.invalidate_market_day_cohort(world_event_id)
    return await _transition(
        db, visit, owner, now=now, phase="inbound",
        next_action_at=min(motion_end, closes_at),
        route_json=route, motion_started_at=now, motion_ends_at=motion_end,
        tile_x=route[0][0], tile_y=route[0][1], error_code=None,
    )


async def _begin_outbound(
    db: AsyncSession, visit: CaravanVisit, owner: str, *, now: datetime,
    route: list[list[int]] | None = None,
) -> CaravanVisit:
    visit_id = visit.id
    from app.services import crowd_service

    crowd_service.invalidate_market_day_cohort(visit.world_event_id)
    await caravan_service.withdraw_visit_imports(db, visit_id, owner, now=now)
    visit = await db.get(CaravanVisit, visit_id, populate_existing=True)
    route = route or list(reversed(visit.route_json or (_route() or [])))
    if not route:
        return await _transition(
            db, visit, owner, now=now, phase="departed", next_action_at=now,
            tile_x=visit.tile_x, tile_y=visit.tile_y, departed_at=now,
        )
    motion_end = _motion_end(now, route)
    return await _transition(
        db, visit, owner, now=now, phase="outbound", next_action_at=motion_end,
        route_json=route, motion_started_at=now, motion_ends_at=motion_end,
        tile_x=route[0][0], tile_y=route[0][1],
    )


def _safe_return_route(visit: CaravanVisit, now: datetime) -> list[list[int]]:
    """Reverse only the completed inbound prefix; never teleport to the plaza."""
    route = [[int(point[0]), int(point[1])] for point in (visit.route_json or [])]
    if not route:
        return [[int(visit.tile_x), int(visit.tile_y)]]
    started = _aware(visit.motion_started_at)
    ends = _aware(visit.motion_ends_at)
    if started is None or ends is None or ends <= started:
        return [route[0]]
    progress = max(0.0, min(1.0, (now - started).total_seconds()
                            / (ends - started).total_seconds()))
    completed_index = min(len(route) - 1, math.floor(progress * (len(route) - 1)))
    return list(reversed(route[:completed_index + 1]))


async def process_claimed_visit(
    db: AsyncSession, visit: CaravanVisit, *, owner: str, now: datetime,
) -> CaravanVisit:
    """Advance exactly one durable step. Financial substeps commit independently."""
    event = await db.get(WorldEvent, visit.world_event_id)
    if event is None:
        return await _transition(
            db, visit, owner, now=now, phase="cancelled", next_action_at=now,
            error_code="world_event_missing", departed_at=now,
        )
    starts_at, closes_at = _aware(event.starts_at), _aware(event.ends_at)
    if starts_at is None or closes_at is None or closes_at <= starts_at:
        return await _transition(
            db, visit, owner, now=now, phase="cancelled", next_action_at=now,
            error_code="world_event_invalid", departed_at=now,
        )

    if visit.phase == "scheduled":
        if now >= closes_at:
            return await _transition(
                db, visit, owner, now=now, phase="cancelled", next_action_at=now,
                error_code="market_window_missed", departed_at=now,
            )
        if now < starts_at:
            if await _admitted(db):
                if await _other_visible_visit_id(db, visit.id) is not None:
                    # Keep the look-ahead row hidden and re-evaluate at open.
                    # Entering waiting here would violate the single-visible
                    # invariant and could hide the active trading visit.
                    return await _transition(
                        db, visit, owner, now=now, phase="scheduled",
                        next_action_at=starts_at,
                    )
                return await _transition(
                    db, visit, owner, now=now, phase="waiting",
                    next_action_at=starts_at, error_code=None,
                )
            return await _transition(
                db, visit, owner, now=now, phase="scheduled", next_action_at=starts_at,
                error_code=ADMISSION_PENDING_ERROR,
            )
        if not await _admitted(db):
            # Policy can be amended at any point during the all-day market. Keep
            # the durable fence and retryable visit until the real window closes;
            # reconcile wakes it immediately when admission reopens.
            return await _transition(
                db, visit, owner, now=now, phase="scheduled",
                next_action_at=closes_at, error_code=ADMISSION_PENDING_ERROR,
            )
        return await _begin_inbound(db, visit, owner, now=now, closes_at=closes_at)

    if visit.phase == "waiting":
        if now >= closes_at:
            return await _transition(
                db, visit, owner, now=now, phase="cancelled", next_action_at=now,
                error_code="market_window_missed", departed_at=now,
            )
        if not await _admitted(db):
            return await _transition(
                db, visit, owner, now=now, phase="waiting",
                next_action_at=closes_at, error_code=ADMISSION_PENDING_ERROR,
            )
        if now < starts_at:
            return await _transition(
                db, visit, owner, now=now, phase="waiting", next_action_at=starts_at,
                error_code=None,
            )
        return await _begin_inbound(db, visit, owner, now=now, closes_at=closes_at)

    if visit.phase == "inbound":
        motion_end = _aware(visit.motion_ends_at)
        if motion_end is None:
            return await _begin_outbound(
                db, visit, owner, now=now,
                route=[[int(visit.tile_x), int(visit.tile_y)]],
            )
        if now >= closes_at or not await _admitted(db):
            return await _begin_outbound(
                db, visit, owner, now=now,
                route=_safe_return_route(visit, now),
            )
        if now < motion_end:
            return await _transition(
                db, visit, owner, now=now, phase="inbound", next_action_at=motion_end,
            )
        return await _transition(
            db, visit, owner, now=now, phase="trading", next_action_at=now,
            tile_x=(visit.route_json or [[visit.tile_x, visit.tile_y]])[-1][0],
            tile_y=(visit.route_json or [[visit.tile_x, visit.tile_y]])[-1][1],
            motion_started_at=None, motion_ends_at=None,
        )

    if visit.phase == "trading":
        # The policy switch is the operational close control.  Before any
        # remaining settlement it sends an admitted caravan back out; if the
        # visit already stocked imports, ``_begin_outbound`` withdraws them.
        if now >= closes_at or not await _admitted(db):
            return await _begin_outbound(db, visit, owner, now=now)
        if visit.settled_at is None:
            await caravan_service.settle_caravan_visit(db, visit.id, owner, now=now)
            visit = await db.get(CaravanVisit, visit.id, populate_existing=True)
        if now < closes_at:
            return await _transition(
                db, visit, owner, now=now, phase="trading", next_action_at=closes_at,
            )
        return await _begin_outbound(db, visit, owner, now=now)

    if visit.phase == "outbound":
        motion_end = _aware(visit.motion_ends_at)
        if motion_end is not None and now < motion_end:
            return await _transition(
                db, visit, owner, now=now, phase="outbound", next_action_at=motion_end,
            )
        return await _transition(
            db, visit, owner, now=now, phase="departed", next_action_at=now,
            tile_x=(visit.route_json or [[visit.tile_x, visit.tile_y]])[-1][0],
            tile_y=(visit.route_json or [[visit.tile_x, visit.tile_y]])[-1][1],
            motion_started_at=None, motion_ends_at=None,
        )
    return visit


async def drive_due_visits(
    db: AsyncSession, *, owner: str, now: datetime | None = None, limit: int = 20,
) -> list[dict]:
    """Advance due rows and return WS-ready full snapshots."""
    if not settings.caravan_lifecycle_enabled:
        return []
    now = now or datetime.now(UTC)
    snapshots: list[dict] = []
    for _ in range(limit):
        visit = await claim_next_visit(db, owner=owner, now=now)
        if visit is None:
            break
        visit_id = visit.id
        was_visible = visit.phase in CARAVAN_VISIBLE_PHASES
        try:
            moved = await process_claimed_visit(db, visit, owner=owner, now=now)
            # Hidden look-ahead rows are DB scheduling details, not render
            # state. Broadcasting scheduled->scheduled or never-visible
            # scheduled->cancelled would let its newer server_time hide a
            # different visit that is currently trading on clients.
            if moved.phase in CARAVAN_VISIBLE_PHASES or was_visible:
                snapshots.append(serialize_visit(moved, now=now))
        except Exception:
            await db.rollback()
            logger.exception("caravan visit %s step failed; lease will be reclaimed", visit_id)
    return snapshots
