"""P2 player market session, shared stock and idempotency regression tests."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.caravan_visit import CaravanVisit
from app.models.market import CaravanMarketPurchase
from app.models.shop import Item, Purchase
from app.models.user import User
from app.models.world_event import WorldEvent
from app.services import market_service
from app.services.market_service import MarketError

pytestmark = pytest.mark.anyio


@pytest.fixture
def player_market_on(monkeypatch):
    monkeypatch.setattr(settings, "npc_economy_enabled", True)
    monkeypatch.setattr(settings, "caravan_enabled", True)
    monkeypatch.setattr(settings, "market_player_enabled", True)
    monkeypatch.setattr(settings, "caravan_lifecycle_enabled", True)
    monkeypatch.setattr(settings, "item_stock_guard_enabled", True)
    monkeypatch.setattr(settings, "market_day_venue", "market_hall")


async def _seed_market(db_session):
    now = datetime.now(UTC).replace(microsecond=0)
    user = User(
        id="market-user",
        name="buyer",
        email="market-buyer@test.local",
        soul_coin_balance=20,
    )
    event = WorldEvent(
        id="player-market-event",
        type="festival",
        title="商队集市",
        starts_at=now - timedelta(minutes=5),
        ends_at=now + timedelta(hours=1),
        payload_json={"market_day": True, "location_id": "market_hall"},
        is_active=True,
    )
    visit = CaravanVisit(
        id="player-market-visit",
        world_event_id=event.id,
        phase="trading",
        visibility_slot="world",
        version=1,
        next_action_at=event.ends_at,
        tile_x=109,
        tile_y=94,
        summary_json={},
    )
    item = Item(
        code="import_tea",
        kind="import_good",
        name="茶叶",
        description="远方茶叶",
        icon="🍵",
        price_sc=6,
        stock=2,
        active=True,
        payload_json={
            "caravan": True,
            "caravan_visit_id": visit.id,
            "stock": 2,
        },
    )
    receipt = Item(
        code="market_tea_chest",
        kind="decor",
        name="远行茶箱",
        description="市场收据",
        price_sc=0,
        active=False,
        payload_json={"market_receipt": True},
    )
    db_session.add_all([user, event, visit, item, receipt])
    await db_session.commit()
    return user, visit, item


async def test_player_market_purchase_charges_stock_and_replays(
    db_session, player_market_on
):
    user, visit, item = await _seed_market(db_session)

    market = await market_service.current_market(db_session, user_id=user.id)
    tea = next(row for row in market["offers"] if row["code"] == "import_tea")
    assert market["purchase_enabled"] is True
    assert tea["available"] is True
    assert tea["stock"] == 2

    first = await market_service.purchase(
        db_session,
        user_id=user.id,
        visit_id=visit.id,
        offer_code="import_tea",
        request_key="request-key-0001",
    )
    assert first["idempotent"] is False
    assert first["total_sc"] == 6
    assert first["effect"]["item_code"] == "market_tea_chest"

    balance = (
        await db_session.execute(
            select(User.soul_coin_balance).where(User.id == user.id)
        )
    ).scalar_one()
    stock = (
        await db_session.execute(select(Item.stock).where(Item.id == item.id))
    ).scalar_one()
    assert balance == 14
    assert stock == 1
    receipt = (
        await db_session.execute(
            select(Purchase).where(Purchase.user_id == user.id)
        )
    ).scalar_one()
    assert receipt.item_code == "market_tea_chest"

    replay = await market_service.purchase(
        db_session,
        user_id=user.id,
        visit_id=visit.id,
        offer_code="import_tea",
        request_key="request-key-0001",
    )
    assert replay["purchase_id"] == first["purchase_id"]
    assert replay["idempotent"] is True
    assert (
        await db_session.execute(
            select(User.soul_coin_balance).where(User.id == user.id)
        )
    ).scalar_one() == 14
    assert (
        await db_session.execute(select(Item.stock).where(Item.id == item.id))
    ).scalar_one() == 1

    with pytest.raises(MarketError, match="限购一次"):
        await market_service.purchase(
            db_session,
            user_id=user.id,
            visit_id=visit.id,
            offer_code="import_tea",
            request_key="request-key-0002",
        )
    purchases = (
        await db_session.execute(select(CaravanMarketPurchase))
    ).scalars().all()
    assert len(purchases) == 1


async def test_player_market_is_preview_only_outside_trading(
    db_session, player_market_on
):
    user, visit, _item = await _seed_market(db_session)
    visit.phase = "outbound"
    await db_session.commit()

    market = await market_service.current_market(db_session, user_id=user.id)
    assert market["active"] is True
    assert market["purchase_enabled"] is False
    assert all(not offer["available"] for offer in market["offers"])
    with pytest.raises(MarketError, match="停止交易"):
        await market_service.purchase(
            db_session,
            user_id=user.id,
            visit_id=visit.id,
            offer_code="import_tea",
            request_key="request-key-0003",
        )


async def test_player_market_rejects_stock_from_another_visit(
    db_session, player_market_on
):
    user, visit, item = await _seed_market(db_session)
    item.payload_json = {
        "caravan": True,
        "caravan_visit_id": "older-visit",
        "stock": 2,
    }
    await db_session.commit()

    market = await market_service.current_market(db_session, user_id=user.id)
    tea = next(row for row in market["offers"] if row["code"] == "import_tea")
    assert tea["stock"] == 0
    assert tea["available"] is False
    with pytest.raises(MarketError, match="售罄"):
        await market_service.purchase(
            db_session,
            user_id=user.id,
            visit_id=visit.id,
            offer_code="import_tea",
            request_key="request-key-0004",
        )
