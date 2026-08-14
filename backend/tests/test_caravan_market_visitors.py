"""Production caravan visitors: durable cohort, real sink and replay guards."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models.caravan_visit import CaravanMarketVisitor, CaravanVisit
from app.models.resident import Resident
from app.models.resident_treasury import ResidentTreasury
from app.models.shop import Item
from app.models.world_event import WorldEvent
from app.services import caravan_market_service, coin_service, crowd_service
from app.services.crowd_service import MARKET_DAY_VISITOR_TILES

pytestmark = pytest.mark.anyio


@pytest.fixture
def market_on(monkeypatch):
    monkeypatch.setattr(settings, "npc_economy_enabled", True)
    monkeypatch.setattr(settings, "caravan_lifecycle_enabled", True)
    monkeypatch.setattr(settings, "npc_trade_reserve_sc", 5)


def _resident(index: int, *, balance: int = 30):
    resident = Resident(
        id=f"resident-{index}", slug=f"resident-{index}", name=f"居民{index}",
        creator_id="system", resident_type="npc", district="cafe",
        status="idle", tile_x=10 + index, tile_y=10,
    )
    treasury = ResidentTreasury(
        resident_slug=resident.slug, balance_sc=balance,
    )
    return resident, treasury


async def _seed_trading_market(db, *, residents=6):
    now = datetime.now(UTC).replace(microsecond=0)
    event = WorldEvent(
        id="market-event", type="festival", title="集市日", description="",
        starts_at=now - timedelta(minutes=5), ends_at=now + timedelta(hours=1),
        payload_json={"market_day": True, "location_id": "market_hall"},
    )
    visit = CaravanVisit(
        id="visit-1", world_event_id=event.id, phase="trading",
        visibility_slot="world", version=1, next_action_at=event.ends_at,
        tile_x=109, tile_y=94, imports_stocked_at=now, settled_at=now,
        summary_json={},
    )
    db.add_all([event, visit])
    people = []
    for index in range(residents):
        resident, treasury = _resident(index)
        people.append(resident)
        db.add_all([resident, treasury])
    # This real autonomous resident is too poor to receive a fake invitation.
    poor, poor_treasury = _resident(99, balance=9)
    db.add_all([poor, poor_treasury])
    for code, name, price in (
        ("import_tea", "茶叶", 6),
        ("import_trinket", "小玩意", 4),
        ("import_cloth", "花布", 8),
    ):
        db.add(Item(
            code=code, kind="import_good", name=name, description="", price_sc=price,
            stock=2, active=True,
            payload_json={
                "caravan": True, "caravan_visit_id": visit.id, "stock": 2,
            },
        ))
    await db.commit()
    return now, event, visit, people, poor


async def test_assignment_is_four_real_funded_residents_and_restart_stable(
    db_session, market_on,
):
    now, event, visit, people, poor = await _seed_trading_market(db_session)

    first = await caravan_market_service.ensure_market_visitors(
        db_session, visit.id, now=now,
    )
    await db_session.commit()
    second = await caravan_market_service.ensure_market_visitors(
        db_session, visit.id, now=now + timedelta(seconds=5),
    )

    assert len(first) == len(second) == 4
    assert [row.resident_id for row in first] == [row.resident_id for row in second]
    assert [row.slot_index for row in first] == [0, 1, 2, 3]
    assert poor.id not in {row.resident_id for row in first}
    assert {row.resident_id for row in first} <= {person.id for person in people}
    assert await caravan_market_service.assigned_visitor_ids(
        db_session, event.id,
    ) == frozenset(row.resident_id for row in first)
    cohort = frozenset(row.resident_id for row in first)
    world_event = [{
        "id": event.id,
        "type": event.type,
        "starts_at": event.starts_at.isoformat(),
        "ends_at": event.ends_at.isoformat(),
        "payload_json": event.payload_json,
    }]
    for assignment in first:
        assert crowd_service.market_day_visitor_tile(
            assignment.resident_id, cohort, world_event,
        ) == MARKET_DAY_VISITOR_TILES[assignment.slot_index]


async def test_arrival_purchases_real_visit_stock_once_and_returns_authority_frame(
    db_session, market_on,
):
    now, _event, visit, people, _poor = await _seed_trading_market(db_session)
    assignments = await caravan_market_service.ensure_market_visitors(
        db_session, visit.id, now=now,
    )
    await db_session.commit()
    assignment = assignments[0]
    buyer = next(person for person in people if person.id == assignment.resident_id)
    buyer.tile_x, buyer.tile_y = MARKET_DAY_VISITOR_TILES[assignment.slot_index]
    await db_session.commit()
    before = await coin_service.treasury_balance(db_session, buyer.slug)

    frame = await caravan_market_service.maybe_purchase_for_resident(
        db_session, buyer, now=now + timedelta(seconds=1),
    )

    assert frame == {
        "type": "market_purchase",
        "visit_id": visit.id,
        "resident_slug": buyer.slug,
        "purchase_id": assignment.id,
        "sequence": 1,
        "item_name": frame["item_name"],
        "quantity": 1,
        "amount_sc": frame["amount_sc"],
    }
    assert frame["item_name"] in {"茶叶", "小玩意", "花布"}
    assert await coin_service.treasury_balance(db_session, buyer.slug) == (
        before - frame["amount_sc"]
    )
    receipt = await db_session.get(
        CaravanMarketVisitor, assignment.id, populate_existing=True,
    )
    assert receipt.item_code is not None
    assert receipt.spent_sc == frame["amount_sc"]
    assert receipt.purchase_sequence == 1
    stock = (await db_session.execute(
        select(Item.stock).where(Item.code == receipt.item_code)
    )).scalar_one()
    assert stock == 1

    # The assignment row is the idempotency claim: replay is read-only.
    assert await caravan_market_service.maybe_purchase_for_resident(
        db_session, buyer, now=now + timedelta(seconds=2),
    ) is None
    assert await coin_service.treasury_balance(db_session, buyer.slug) == (
        before - frame["amount_sc"]
    )
    assert (await db_session.execute(
        select(func.count()).select_from(CaravanMarketVisitor).where(
            CaravanMarketVisitor.visit_id == visit.id,
            CaravanMarketVisitor.purchased_at.is_not(None),
        )
    )).scalar_one() == 1


async def test_purchase_waits_for_slot_and_never_uses_another_visits_stock(
    db_session, market_on,
):
    now, _event, visit, people, _poor = await _seed_trading_market(db_session)
    assignments = await caravan_market_service.ensure_market_visitors(
        db_session, visit.id, now=now,
    )
    await db_session.commit()
    assignment = assignments[0]
    buyer = next(person for person in people if person.id == assignment.resident_id)
    before = await coin_service.treasury_balance(db_session, buyer.slug)

    assert await caravan_market_service.maybe_purchase_for_resident(
        db_session, buyer, now=now,
    ) is None
    assert await coin_service.treasury_balance(db_session, buyer.slug) == before

    buyer.tile_x, buyer.tile_y = MARKET_DAY_VISITOR_TILES[assignment.slot_index]
    await db_session.execute(
        Item.__table__.update().values(
            payload_json={
                "caravan": True, "caravan_visit_id": "other-visit", "stock": 2,
            }
        )
    )
    await db_session.commit()
    assert await caravan_market_service.maybe_purchase_for_resident(
        db_session, buyer, now=now,
    ) is None
    assert await coin_service.treasury_balance(db_session, buyer.slug) == before


async def test_all_four_lifecycle_visitors_buy_with_generic_crowd_gate_off(
    db_session, market_on, monkeypatch,
):
    """Caravan invitations remain executable when cosmetic crowd realism is off."""
    from app.agent.actions import ActionType
    from app.agent.phases.decide.basic import BasicDecidePlugin
    from app.agent.schemas import TickContext

    monkeypatch.setattr(settings, "realism_crowd_enabled", False)
    crowd_service._reset_for_tests()
    now, event, visit, people, _poor = await _seed_trading_market(db_session)
    assignments = await caravan_market_service.ensure_market_visitors(
        db_session, visit.id, now=now,
    )
    await db_session.commit()
    assert len(assignments) == 4

    world_events = [{
        "id": event.id,
        "type": event.type,
        "starts_at": event.starts_at.isoformat(),
        "ends_at": event.ends_at.isoformat(),
        "payload_json": event.payload_json,
    }]
    buyers = {person.id: person for person in people}
    frames = []
    plugin = BasicDecidePlugin()
    for assignment in assignments:
        buyer = buyers[assignment.resident_id]
        ctx = TickContext(
            db=db_session,
            resident=buyer,
            world_time="",
            hour=12,
            schedule_phase="free",
        )
        ctx.world_events = world_events
        ctx.available_actions = [ActionType.VISIT_DISTRICT]

        directed = await plugin._maybe_crowd_draw(ctx)
        assert directed is not None
        assert directed.target_slug == "market_hall"
        assert directed.target_tile == MARKET_DAY_VISITOR_TILES[assignment.slot_index]

        # Movement/pathfinding owns the intermediate ticks; reaching the exact
        # authoritative target invokes the existing atomic sink unchanged.
        buyer.tile_x, buyer.tile_y = directed.target_tile
        await db_session.commit()
        frame = await caravan_market_service.maybe_purchase_for_resident(
            db_session, buyer, now=now + timedelta(seconds=assignment.slot_index + 1),
        )
        assert frame is not None
        frames.append(frame)

    assert len(frames) == 4
    assert (await db_session.execute(
        select(func.count()).select_from(CaravanMarketVisitor).where(
            CaravanMarketVisitor.visit_id == visit.id,
            CaravanMarketVisitor.purchased_at.is_not(None),
        )
    )).scalar_one() == 4
    assert (await db_session.execute(
        select(func.coalesce(func.sum(Item.stock), 0)).where(
            Item.kind == "import_good",
        )
    )).scalar_one() == 2
