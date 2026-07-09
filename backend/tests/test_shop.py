"""S3 shop pipeline: purchase, insufficient funds, effect dispatch, API."""

import pytest
from sqlalchemy import select, func

from app.models.user import User
from app.models.shop import Item, Purchase
from app.models.transaction import Transaction


async def _user(db_session, email, balance):
    u = User(name="s", email=email, soul_coin_balance=balance)
    db_session.add(u)
    await db_session.commit()
    return u


async def _item(db_session, code="flower", kind="gift", price=20, active=True):
    it = Item(code=code, kind=kind, name="鲜花", description="一束花", price_sc=price, active=active)
    db_session.add(it)
    await db_session.commit()
    return it


@pytest.mark.anyio
async def test_purchase_success_records_and_charges(db_session):
    from app.services.shop_service import purchase

    user = await _user(db_session, "buy@test.com", 100)
    await _item(db_session, price=20)

    result = await purchase(db_session, user.id, "flower", qty=2)
    assert result["total_sc"] == 40

    await db_session.refresh(user)
    assert user.soul_coin_balance == 60

    purchases = (await db_session.execute(select(Purchase))).scalars().all()
    assert len(purchases) == 1 and purchases[0].qty == 2
    tx = (await db_session.execute(
        select(Transaction).where(Transaction.reason == "purchase:flower")
    )).scalars().all()
    assert len(tx) == 1 and tx[0].amount == -40


@pytest.mark.anyio
async def test_purchase_insufficient_has_no_side_effect(db_session):
    from app.services.shop_service import purchase, ShopError

    user = await _user(db_session, "poor@test.com", 10)
    await _item(db_session, price=50)

    with pytest.raises(ShopError):
        await purchase(db_session, user.id, "flower")

    await db_session.refresh(user)
    assert user.soul_coin_balance == 10  # unchanged
    count = (await db_session.execute(select(func.count()).select_from(Purchase))).scalar()
    assert count == 0


@pytest.mark.anyio
async def test_purchase_unknown_or_inactive_item(db_session):
    from app.services.shop_service import purchase, ShopError

    user = await _user(db_session, "u2@test.com", 100)
    await _item(db_session, code="dead", price=5, active=False)

    with pytest.raises(ShopError):
        await purchase(db_session, user.id, "nope")
    with pytest.raises(ShopError):
        await purchase(db_session, user.id, "dead")


@pytest.mark.anyio
async def test_effect_dispatch_by_kind(db_session):
    from app.services import shop_effects
    from app.services.shop_service import purchase

    # Save/restore the registry so we don't wipe the built-in D2 handlers.
    saved_e, saved_p = dict(shop_effects._effects), dict(shop_effects._prechecks)
    shop_effects._effects.clear()
    shop_effects._prechecks.clear()
    seen = {}

    @shop_effects.register("gift")
    async def _gift(db, user_id, item, qty, context):
        seen["called"] = (user_id, item.code, qty, context)
        return {"boost": 0.1}

    try:
        user = await _user(db_session, "eff@test.com", 100)
        await _item(db_session, kind="gift", price=10)
        result = await purchase(db_session, user.id, "flower", qty=1, context={"target": "klaus"})
        assert result["effect"] == {"boost": 0.1}
        assert seen["called"][1] == "flower" and seen["called"][3] == {"target": "klaus"}
    finally:
        shop_effects._effects.clear(); shop_effects._effects.update(saved_e)
        shop_effects._prechecks.clear(); shop_effects._prechecks.update(saved_p)


@pytest.mark.anyio
async def test_shop_api_catalog_and_purchase(client, db_session):
    from app.services.auth_service import create_token

    user = await _user(db_session, "api@shop.com", 100)
    await _item(db_session, code="lamp", kind="decor", price=30)
    headers = {"Authorization": f"Bearer {create_token(user.id)}"}

    cat = await client.get("/shop/catalog")
    assert cat.status_code == 200
    assert any(i["code"] == "lamp" for i in cat.json()["items"])

    ok = await client.post("/shop/purchase", json={"item_code": "lamp", "qty": 1}, headers=headers)
    assert ok.status_code == 200
    assert ok.json()["total_sc"] == 30

    # Now broke → 400.
    bad = await client.post("/shop/purchase", json={"item_code": "lamp", "qty": 100}, headers=headers)
    assert bad.status_code == 400
