"""Durable caravan state machine: idempotency, leases, recovery and protocol."""
from datetime import UTC, datetime, timedelta
import importlib
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update

from app.config import settings
from app.models.caravan_visit import CaravanVisit, CaravanVisitPurchase
from app.models.resident import Resident
from app.models.shop import Item
from app.models.world_event import WorldEvent
from app.services import caravan_lifecycle_service as lifecycle
from app.services import caravan_service, coin_service, treasury_service

pytestmark = pytest.mark.anyio


def waypoints():
    plan = lifecycle._route_plan()
    return plan.outside_staging, plan.entrance_staging, plan.market_hall_parking


@pytest.fixture(autouse=True)
def lifecycle_on(monkeypatch):
    monkeypatch.setattr(settings, "caravan_lifecycle_enabled", True)
    monkeypatch.setattr(settings, "caravan_lifecycle_interval_seconds", 1)
    monkeypatch.setattr(settings, "caravan_wait_lead_seconds", 600)
    monkeypatch.setattr(settings, "caravan_route_tile_ms", 10)
    monkeypatch.setattr(settings, "caravan_lease_seconds", 30)
    monkeypatch.setattr(settings, "npc_economy_enabled", True)
    monkeypatch.setattr(settings, "caravan_enabled", True)
    monkeypatch.setattr(settings, "polis_policy_enabled", False)
    monkeypatch.setattr(settings, "town_treasury_enabled", False)
    monkeypatch.setattr(settings, "caravan_stall_fee_sc", 5)
    monkeypatch.setattr(settings, "caravan_budget_sc", 30)
    monkeypatch.setattr(caravan_service, "_narrate", AsyncMock())


def market_event(*, event_id="market-1", starts_at, ends_at) -> WorldEvent:
    return WorldEvent(
        id=event_id,
        type="festival",
        title="集市日",
        description="",
        payload_json={"market_day": True, "location_id": "central_plaza"},
        starts_at=starts_at,
        ends_at=ends_at,
        is_active=False,
    )


async def test_dark_gate_is_zero_write(db_session, monkeypatch):
    monkeypatch.setattr(settings, "caravan_lifecycle_enabled", False)
    now = datetime.now(UTC)
    event = market_event(starts_at=now, ends_at=now + timedelta(hours=1))
    db_session.add(event)
    await db_session.commit()

    assert await lifecycle.ensure_visit_for_event(db_session, event, now=now) is None
    assert await lifecycle.drive_due_visits(db_session, owner="worker", now=now) == []
    assert (await db_session.execute(
        select(func.count()).select_from(CaravanVisit)
    )).scalar_one() == 0


async def test_world_event_identity_is_unique_and_enqueue_is_idempotent(db_session):
    now = datetime.now(UTC)
    event = market_event(
        starts_at=now + timedelta(minutes=20),
        ends_at=now + timedelta(hours=2),
    )
    db_session.add(event)
    await db_session.commit()

    first = await lifecycle.ensure_visit_for_event(db_session, event, now=now)
    second = await lifecycle.ensure_visit_for_event(db_session, event, now=now)

    assert first.id == second.id
    assert first.phase == "scheduled"
    assert lifecycle._aware(first.next_action_at) == now + timedelta(minutes=10)
    assert (await db_session.execute(
        select(func.count()).select_from(CaravanVisit)
    )).scalar_one() == 1


async def test_durable_row_fences_legacy_fallback_after_gate_flip(
    db_session, monkeypatch,
):
    now = datetime.now(UTC)
    event = market_event(starts_at=now + timedelta(minutes=5), ends_at=now + timedelta(hours=1))
    maker = Resident(
        id="legacy-maker-id", slug="legacy-maker", name="陶匠", district="free",
        status="idle", resident_type="npc",
    )
    work = Item(
        code="legacy_work", kind="resident_work", name="陶罐", description="",
        price_sc=15, stock=2,
        payload_json={"creator_slug": maker.slug, "stock": 2}, active=True,
    )
    db_session.add_all([event, maker, work])
    await db_session.commit()
    await lifecycle.ensure_visit_for_event(db_session, event, now=now)
    monkeypatch.setattr(settings, "caravan_lifecycle_enabled", False)

    result = await caravan_service.run_caravan_visit(
        db_session,
        {"id": event.id, "payload_json": {"market_day": True}},
    )

    assert result == {"bought": 0, "spent": 0, "tax": 0, "fee": 0, "imported": 0}
    assert await coin_service.treasury_balance(db_session, maker.slug) == 0
    assert (await db_session.execute(
        select(Item.stock).where(Item.code == work.code)
    )).scalar_one() == 2


async def test_legacy_marker_becomes_terminal_tombstone_on_cutover(
    db_session, monkeypatch,
):
    now = datetime.now(UTC)
    event = market_event(starts_at=now, ends_at=now + timedelta(hours=1))
    db_session.add(event)
    await db_session.commit()
    monkeypatch.setattr(settings, "caravan_lifecycle_enabled", False)
    legacy = await caravan_service.run_caravan_visit(
        db_session, {"id": event.id, "payload_json": {"market_day": True}},
    )
    assert legacy["imported"] == 3

    monkeypatch.setattr(settings, "caravan_lifecycle_enabled", True)
    visit = await lifecycle.ensure_visit_for_event(db_session, event, now=now)

    assert visit.phase == "cancelled"
    assert visit.error_code == "legacy_already_settled"
    assert await lifecycle.drive_due_visits(db_session, owner="worker", now=now) == []


async def test_full_lifecycle_settles_once_and_withdraws_imports(db_session):
    base = datetime.now(UTC).replace(microsecond=0)
    opens = base + timedelta(minutes=20)
    closes = opens + timedelta(hours=1)
    event = market_event(starts_at=opens, ends_at=closes)
    maker = Resident(
        id="maker-id", slug="maker", name="陶匠", district="free",
        status="idle", resident_type="npc",
    )
    work = Item(
        code="work_pot", kind="resident_work", name="陶罐", description="",
        price_sc=15, stock=2,
        payload_json={"creator_slug": "maker", "stock": 2}, active=True,
    )
    db_session.add_all([event, maker, work])
    await db_session.commit()
    visit = await lifecycle.ensure_visit_for_event(db_session, event, now=base)

    waiting = await lifecycle.drive_due_visits(
        db_session, owner="worker-a", now=opens - timedelta(minutes=10),
    )
    assert waiting[-1]["phase"] == "waiting" and waiting[-1]["visible"] is True

    inbound = await lifecycle.drive_due_visits(db_session, owner="worker-a", now=opens)
    outside, entrance, plaza = waypoints()
    assert inbound[-1]["phase"] == "inbound"
    assert waiting[-1]["position"] == {
        "tile_x": outside[0], "tile_y": outside[1],
    }
    assert inbound[-1]["motion"]["path"][0] == list(outside)
    assert list(entrance) in inbound[-1]["motion"]["path"]
    assert inbound[-1]["motion"]["path"][-1] == list(plaza)

    visit = await db_session.get(CaravanVisit, visit.id, populate_existing=True)
    arrival = lifecycle._aware(visit.motion_ends_at)
    trading = await lifecycle.drive_due_visits(
        db_session, owner="worker-a", now=arrival,
    )
    assert trading[-1]["phase"] == "trading"
    assert trading[-1]["summary"] == {
        "fee_sc": 0, "bought": 1, "spent_sc": 15,
        "tax_sc": 0, "imports_stocked": 3,
    }
    assert await coin_service.treasury_balance(db_session, "maker") == 15
    assert (await db_session.execute(
        select(func.count()).select_from(CaravanVisitPurchase)
    )).scalar_one() == 1
    assert (await db_session.execute(
        select(Item.stock).where(Item.code == "work_pot")
    )).scalar_one() == 1
    assert len((await db_session.execute(
        select(Item).where(Item.kind == caravan_service.IMPORT_KIND, Item.active.is_(True))
    )).scalars().all()) == 3
    assert await treasury_service.kv_read(db_session, caravan_service.LAST_VISIT_KEY) == event.id

    # Simulate a retry after the settlement commit.  The purchase identity and
    # step timestamps make it a read-only replay, not another mint/stock reset.
    visit = await db_session.get(CaravanVisit, visit.id, populate_existing=True)
    await db_session.execute(
        update(CaravanVisit).where(CaravanVisit.id == visit.id).values(
            lease_owner="retry", lease_expires_at=arrival + timedelta(seconds=30)
        )
    )
    await db_session.commit()
    replay = await caravan_service.settle_caravan_visit(
        db_session, visit.id, "retry", now=arrival + timedelta(seconds=1),
    )
    assert replay["bought"] == 1
    assert await coin_service.treasury_balance(db_session, "maker") == 15
    assert (await db_session.execute(
        select(func.count()).select_from(CaravanVisitPurchase)
    )).scalar_one() == 1

    # Restore a due, unleased trading row exactly as the driver would leave it.
    await db_session.execute(
        update(CaravanVisit).where(CaravanVisit.id == visit.id).values(
            lease_owner=None, lease_expires_at=None, next_action_at=closes,
        )
    )
    await db_session.commit()
    outbound = await lifecycle.drive_due_visits(
        db_session, owner="worker-b", now=closes,
    )
    assert outbound[-1]["phase"] == "outbound"
    assert len((await db_session.execute(
        select(Item).where(Item.kind == caravan_service.IMPORT_KIND, Item.active.is_(True))
    )).scalars().all()) == 0

    visit = await db_session.get(CaravanVisit, visit.id, populate_existing=True)
    departed = await lifecycle.drive_due_visits(
        db_session, owner="worker-b", now=lifecycle._aware(visit.motion_ends_at),
    )
    assert departed[-1]["phase"] == "departed"
    assert departed[-1]["visible"] is False


async def test_market_closing_mid_inbound_animates_reverse_route(db_session):
    opens = datetime.now(UTC).replace(microsecond=0)
    closes = opens + timedelta(milliseconds=500)
    event = market_event(starts_at=opens, ends_at=closes)
    db_session.add(event)
    await db_session.commit()
    await lifecycle.ensure_visit_for_event(db_session, event, now=opens)

    inbound = await lifecycle.drive_due_visits(db_session, owner="worker", now=opens)
    inbound_path = inbound[-1]["motion"]["path"]
    assert inbound[-1]["phase"] == "inbound"

    outbound = await lifecycle.drive_due_visits(db_session, owner="worker", now=closes)
    state = outbound[-1]
    outside, _, _ = waypoints()
    assert state["phase"] == "outbound"
    assert state["visible"] is True
    assert state["motion"]["path"][-1] == list(outside)
    assert state["motion"]["path"] == list(reversed(
        inbound_path[:len(state["motion"]["path"])]
    ))


async def test_policy_close_wakes_inbound_and_animates_safe_return(
    db_session, monkeypatch,
):
    opens = datetime.now(UTC).replace(microsecond=0)
    event = market_event(starts_at=opens, ends_at=opens + timedelta(hours=1))
    db_session.add(event)
    await db_session.commit()
    await lifecycle.ensure_visit_for_event(db_session, event, now=opens)
    inbound = await lifecycle.drive_due_visits(db_session, owner="worker", now=opens)
    assert inbound[-1]["phase"] == "inbound"

    policy_close = opens + timedelta(milliseconds=500)
    monkeypatch.setattr(settings, "caravan_enabled", False)
    await lifecycle.reconcile_market_events(db_session, now=policy_close)
    outbound = await lifecycle.drive_due_visits(
        db_session, owner="worker", now=policy_close,
    )

    outside, _, _ = waypoints()
    assert outbound[-1]["phase"] == "outbound"
    assert outbound[-1]["visible"] is True
    assert outbound[-1]["motion"]["path"][-1] == list(outside)


async def test_expired_lease_is_reclaimed_with_higher_version(db_session):
    now = datetime.now(UTC).replace(microsecond=0)
    event = market_event(starts_at=now + timedelta(hours=1), ends_at=now + timedelta(hours=2))
    db_session.add(event)
    await db_session.commit()
    visit = await lifecycle.ensure_visit_for_event(db_session, event, now=now)
    await db_session.execute(
        update(CaravanVisit).where(CaravanVisit.id == visit.id).values(
            next_action_at=now,
            lease_owner="dead-worker",
            lease_expires_at=now - timedelta(seconds=1),
        )
    )
    await db_session.commit()
    old_version = visit.version

    claimed = await lifecycle.claim_next_visit(db_session, owner="recovery", now=now)
    assert claimed.lease_owner == "recovery"
    assert claimed.version > old_version
    assert await lifecycle.claim_next_visit(db_session, owner="other", now=now) is None


async def test_lease_renewal_uses_wall_clock_never_moves_back_and_blocks_reclaim(
    db_session, monkeypatch,
):
    driver_now = datetime.now(UTC).replace(microsecond=0)
    wall_now = driver_now + timedelta(minutes=2)
    event = market_event(
        event_id="market-lease",
        starts_at=driver_now - timedelta(minutes=1),
        ends_at=driver_now + timedelta(hours=1),
    )
    db_session.add(event)
    await db_session.flush()
    _, _, plaza = waypoints()
    visit = CaravanVisit(
        world_event_id=event.id, phase="trading", next_action_at=driver_now,
        visibility_slot="world",
        tile_x=plaza[0], tile_y=plaza[1], summary_json={},
        lease_owner="slow-worker",
        lease_expires_at=driver_now + timedelta(seconds=5),
    )
    db_session.add(visit)
    await db_session.commit()
    monkeypatch.setattr(caravan_service, "_wall_now", lambda: wall_now)

    await caravan_service._renew_owned_visit(
        db_session, visit.id, "slow-worker", now=driver_now,
    )
    await db_session.commit()
    refreshed = await db_session.get(CaravanVisit, visit.id, populate_existing=True)
    renewed_until = lifecycle._aware(refreshed.lease_expires_at)
    assert renewed_until >= wall_now + timedelta(seconds=settings.caravan_lease_seconds)
    assert await lifecycle.claim_next_visit(
        db_session, owner="second-worker", now=wall_now + timedelta(seconds=5),
    ) is None

    # A later call carrying an older driver timestamp must preserve a longer
    # lease already written by a prior financial step.
    longer = wall_now + timedelta(minutes=10)
    await db_session.execute(
        update(CaravanVisit).where(CaravanVisit.id == visit.id).values(
            lease_expires_at=longer,
        )
    )
    await db_session.commit()
    await caravan_service._renew_owned_visit(
        db_session, visit.id, "slow-worker", now=driver_now,
    )
    await db_session.commit()
    refreshed = await db_session.get(CaravanVisit, visit.id, populate_existing=True)
    assert lifecycle._aware(refreshed.lease_expires_at) == longer


async def test_settlement_budget_never_overshoots(db_session):
    now = datetime.now(UTC).replace(microsecond=0)
    event = market_event(
        event_id="market-budget",
        starts_at=now - timedelta(minutes=1),
        ends_at=now + timedelta(hours=1),
    )
    _, _, plaza = waypoints()
    visit = CaravanVisit(
        world_event_id=event.id, phase="trading", next_action_at=now,
        visibility_slot="world",
        tile_x=plaza[0], tile_y=plaza[1], summary_json={},
        lease_owner="budget-worker", lease_expires_at=now + timedelta(minutes=1),
    )
    makers = [
        Resident(
            id=f"budget-maker-{i}", slug=f"budget-maker-{i}", name=f"作者{i}",
            district="free", status="idle", resident_type="npc",
        )
        for i in range(2)
    ]
    works = [
        Item(
            code=f"budget-work-{i}", kind="resident_work", name=f"作品{i}",
            description="", price_sc=20, stock=1, active=True,
            payload_json={"creator_slug": makers[i].slug, "stock": 1},
        )
        for i in range(2)
    ]
    db_session.add_all([event, visit, *makers, *works])
    await db_session.commit()

    summary = await caravan_service.settle_caravan_visit(
        db_session, visit.id, "budget-worker", now=now,
    )

    assert summary["bought"] == 1
    assert summary["spent_sc"] == 20
    assert summary["spent_sc"] <= settings.caravan_budget_sc
    assert (await db_session.execute(
        select(func.count()).select_from(CaravanVisitPurchase).where(
            CaravanVisitPurchase.visit_id == visit.id,
        )
    )).scalar_one() == 1


async def test_policy_close_sends_trading_visit_out_and_withdraws_stock(
    db_session, monkeypatch,
):
    now = datetime.now(UTC).replace(microsecond=0)
    event = market_event(starts_at=now - timedelta(minutes=5), ends_at=now + timedelta(hours=1))
    _, _, plaza = waypoints()
    db_session.add(event)
    await db_session.flush()
    visit = CaravanVisit(
        world_event_id=event.id, phase="trading", version=1,
        visibility_slot="world",
        next_action_at=now, tile_x=plaza[0], tile_y=plaza[1],
        imports_stocked_at=now - timedelta(minutes=1),
        summary_json={"imports_stocked": 3},
    )
    db_session.add(visit)
    await db_session.flush()
    for definition in caravan_service.IMPORT_DEFS:
        db_session.add(Item(
            **definition, kind=caravan_service.IMPORT_KIND, stock=2, active=True,
            payload_json={
                "caravan": True, "caravan_visit_id": visit.id, "stock": 2,
            },
        ))
    await db_session.commit()
    monkeypatch.setattr(settings, "caravan_enabled", False)

    states = await lifecycle.drive_due_visits(db_session, owner="closer", now=now)
    assert states[-1]["phase"] == "outbound"
    assert (await db_session.execute(
        select(func.count()).select_from(Item).where(
            Item.kind == caravan_service.IMPORT_KIND, Item.active.is_(True),
        )
    )).scalar_one() == 0


async def test_closed_market_never_resumes_unsettled_trading(
    db_session, monkeypatch,
):
    now = datetime.now(UTC).replace(microsecond=0)
    event = market_event(
        event_id="market-closed-recovery",
        starts_at=now - timedelta(hours=2),
        ends_at=now - timedelta(seconds=1),
    )
    _, _, plaza = waypoints()
    db_session.add(event)
    await db_session.flush()
    route = [list(tile) for tile in lifecycle._route_plan().full_path]
    visit = CaravanVisit(
        world_event_id=event.id, phase="trading", next_action_at=now,
        visibility_slot="world",
        tile_x=plaza[0], tile_y=plaza[1], route_json=route, summary_json={},
    )
    db_session.add(visit)
    await db_session.commit()
    settle = AsyncMock()
    monkeypatch.setattr(caravan_service, "settle_caravan_visit", settle)

    states = await lifecycle.drive_due_visits(db_session, owner="recovery", now=now)

    settle.assert_not_awaited()
    assert states[-1]["phase"] == "outbound"
    assert states[-1]["visible"] is True


async def test_current_endpoint_and_ws_snapshot_share_exact_shape(client, db_session):
    empty = (await client.get("/caravans/current")).json()
    assert empty == {
        "type": "caravan_state", "visit_id": None, "world_event_id": None,
        "version": 0, "phase": None, "server_time": empty["server_time"],
        "position": None, "motion": None,
        "summary": {
            "fee_sc": 0, "bought": 0, "spent_sc": 0,
            "tax_sc": 0, "imports_stocked": 0,
        },
        "visible": False,
    }

    now = datetime.now(UTC).replace(microsecond=0)
    event = market_event(starts_at=now + timedelta(minutes=5), ends_at=now + timedelta(hours=1))
    db_session.add(event)
    await db_session.commit()
    visit = await lifecycle.ensure_visit_for_event(db_session, event, now=now)

    body = (await client.get("/caravans/current")).json()
    assert body["visit_id"] is None  # scheduled look-ahead rows are not renderable
    assert set(body) == {
        "type", "visit_id", "world_event_id", "version", "phase",
        "server_time", "position", "motion", "summary", "visible",
    }

    await db_session.execute(
        update(CaravanVisit).where(CaravanVisit.id == visit.id).values(
            phase="waiting", next_action_at=event.starts_at,
        )
    )
    await db_session.commit()
    visible = (await client.get("/caravans/current")).json()
    refreshed = await db_session.get(CaravanVisit, visit.id, populate_existing=True)
    direct = lifecycle.serialize_visit(
        refreshed, now=datetime.fromisoformat(visible["server_time"]),
    )
    assert visible == direct


async def test_route_failure_is_import_and_api_safe_while_gate_is_off(
    client, monkeypatch,
):
    from app.services import caravan_route

    def broken_route():
        raise RuntimeError("bad tilemap")

    monkeypatch.setattr(settings, "caravan_lifecycle_enabled", False)
    with monkeypatch.context() as scoped:
        scoped.setattr(caravan_route, "build_caravan_route", broken_route)
        # Reload proves module top-level does not touch route construction.
        importlib.reload(lifecycle)
        response = await client.get("/caravans/current")
        assert response.status_code == 200
        assert response.json()["type"] == "caravan_state"
        assert response.json()["visible"] is False
    # No failed builder is retained: _route_plan imports the restored helper
    # lazily on its next actual lifecycle call.
    importlib.reload(lifecycle)


async def test_future_scheduled_visit_cannot_hide_current_trading_visit(db_session):
    now = datetime.now(UTC).replace(microsecond=0)
    current_event = market_event(
        event_id="market-current",
        starts_at=now - timedelta(minutes=5),
        ends_at=now + timedelta(hours=1),
    )
    future_event = market_event(
        event_id="market-future",
        starts_at=now + timedelta(days=1),
        ends_at=now + timedelta(days=1, hours=1),
    )
    db_session.add_all([current_event, future_event])
    outside, _, plaza = waypoints()
    await db_session.flush()
    current = CaravanVisit(
        world_event_id=current_event.id, phase="trading", next_action_at=current_event.ends_at,
        visibility_slot="world",
        tile_x=plaza[0], tile_y=plaza[1], summary_json={},
        created_at=now - timedelta(hours=1), updated_at=now,
    )
    future = CaravanVisit(
        world_event_id=future_event.id, phase="scheduled", next_action_at=future_event.starts_at,
        tile_x=outside[0], tile_y=outside[1], summary_json={},
        created_at=now, updated_at=now,
    )
    db_session.add_all([current, future])
    await db_session.commit()

    state = await lifecycle.current_snapshot(db_session, now=now)

    assert state["visit_id"] == current.id
    assert state["phase"] == "trading"
    assert state["visible"] is True


async def test_overlapping_wait_lead_stays_hidden_while_another_visit_trades(db_session):
    now = datetime.now(UTC).replace(microsecond=0)
    current_event = market_event(
        event_id="market-overlap-current",
        starts_at=now - timedelta(minutes=5),
        ends_at=now + timedelta(hours=1),
    )
    next_event = market_event(
        event_id="market-overlap-next",
        starts_at=now + timedelta(minutes=10),
        ends_at=now + timedelta(hours=2),
    )
    db_session.add_all([current_event, next_event])
    _, _, plaza = waypoints()
    await db_session.flush()
    current = CaravanVisit(
        world_event_id=current_event.id, phase="trading",
        visibility_slot="world",
        next_action_at=current_event.ends_at,
        tile_x=plaza[0], tile_y=plaza[1], summary_json={},
    )
    db_session.add(current)
    await db_session.commit()
    second = await lifecycle.ensure_visit_for_event(db_session, next_event, now=now)

    moved = await lifecycle.drive_due_visits(db_session, owner="worker", now=now)
    refreshed = await db_session.get(CaravanVisit, second.id, populate_existing=True)
    visible = (await db_session.execute(
        select(CaravanVisit).where(CaravanVisit.phase.in_(
            ("waiting", "inbound", "trading", "outbound"),
        ))
    )).scalars().all()

    assert moved == []  # hidden second visit emits no cross-visit WS frame
    assert refreshed.phase == "scheduled"
    assert lifecycle._aware(refreshed.next_action_at) == lifecycle._aware(next_event.starts_at)
    assert [visit.id for visit in visible] == [current.id]
    assert (await lifecycle.current_snapshot(db_session, now=now))["visit_id"] == current.id


async def test_never_visible_overlap_cancellation_emits_no_cleanup_snapshot(db_session):
    now = datetime.now(UTC).replace(microsecond=0)
    current_event = market_event(
        event_id="market-ws-current", starts_at=now - timedelta(minutes=5),
        ends_at=now + timedelta(hours=1),
    )
    overlapping_event = market_event(
        event_id="market-ws-overlap", starts_at=now,
        ends_at=now + timedelta(hours=1),
    )
    db_session.add_all([current_event, overlapping_event])
    await db_session.flush()
    outside, _, plaza = waypoints()
    current = CaravanVisit(
        world_event_id=current_event.id, phase="trading",
        visibility_slot="world",
        next_action_at=current_event.ends_at, tile_x=plaza[0], tile_y=plaza[1],
        summary_json={},
    )
    hidden = CaravanVisit(
        world_event_id=overlapping_event.id, phase="scheduled",
        next_action_at=now, tile_x=outside[0], tile_y=outside[1], summary_json={},
    )
    db_session.add_all([current, hidden])
    await db_session.commit()

    snapshots = await lifecycle.drive_due_visits(db_session, owner="worker", now=now)

    assert snapshots == []
    refreshed = await db_session.get(CaravanVisit, hidden.id, populate_existing=True)
    assert refreshed.phase == "cancelled"
    state = await lifecycle.current_snapshot(db_session, now=now)
    assert state["visit_id"] == current.id
    assert state["visible"] is True
