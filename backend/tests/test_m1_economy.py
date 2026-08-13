"""M1 economy tests: duty wages, meal cost + 赊账, wallet-pressure hint,
resident-made goods, and 集市日 (event + discount)."""
from datetime import date

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.resident import Resident
from app.services import duty_service, coin_service


def _res(slug, name, duty=None, **kw):
    meta = {"duty": duty} if duty else None
    d = dict(slug=slug, name=name, district="workshop", status="idle",
             resident_type="npc", tile_x=116, tile_y=27, meta_json=meta)
    d.update(kw)
    return Resident(**d)


# ── F1.1 wages ─────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_duty_work_pays_wage_into_treasury(db_session):
    r = _res("chen", "陈铁生", {"key": "workshop_fixer",
             "perks": {"commission_reward": 8, "wage_sc": 8}})
    db_session.add(r)
    await db_session.commit()

    line = await duty_service.on_work(db_session, r)
    assert line is not None
    assert await coin_service.treasury_balance(db_session, "chen") == 8
    # write-through wallet cache is set for the prompt to read
    assert (r.meta_json or {}).get("wallet") == 8


@pytest.mark.anyio
async def test_wage_uses_default_when_perk_absent(db_session):
    r = _res("gu", "顾明远", {"key": "lecturer", "perks": {}}, district="academy",
             tile_x=28, tile_y=26)
    db_session.add(r)
    await db_session.commit()
    await duty_service.on_work(db_session, r)
    assert await coin_service.treasury_balance(db_session, "gu") == settings.npc_default_wage_sc


@pytest.mark.anyio
async def test_no_wage_when_economy_off(db_session, monkeypatch):
    monkeypatch.setattr(settings, "npc_economy_enabled", False)
    r = _res("he", "何巧云", {"key": "shop_keeper", "perks": {"wage_sc": 5}},
             district="shop", tile_x=84, tile_y=48)
    from app.models.shop import Item
    db_session.add_all([r, Item(code="tea", kind="consumable", name="茶", price_sc=10)])
    await db_session.commit()
    await duty_service.on_work(db_session, r)
    assert await coin_service.treasury_balance(db_session, "he") == 0


# ── F1.2 meal cost + 赊账 ──────────────────────────────────────────────

@pytest.mark.anyio
async def test_charge_meal_debits_when_funded(db_session):
    from app.agent.phases.execute.basic import _charge_meal
    r = _res("diner", "食客", tile_x=57, tile_y=20, district="cafe")
    db_session.add(r)
    await db_session.commit()
    await coin_service.treasury_credit(db_session, "diner", 10)

    await _charge_meal(db_session, r)
    assert await coin_service.treasury_balance(db_session, "diner") == 10 - settings.npc_meal_cost_sc
    assert (r.meta_json or {}).get("wallet") == 10 - settings.npc_meal_cost_sc


@pytest.mark.anyio
async def test_charge_meal_credit_when_broke_creates_memory_and_tie(db_session):
    from app.agent.phases.execute.basic import _charge_meal
    from app.models.memory import Memory
    from app.services import relation_service

    host = _res("lin", "林晚秋", {"key": "cafe_host", "perks": {}},
                district="cafe", tile_x=57, tile_y=20)
    diner = _res("broke", "穷食客", tile_x=57, tile_y=20, district="cafe")
    db_session.add_all([host, diner])
    await db_session.commit()

    await _charge_meal(db_session, diner)  # zero balance → 赊账
    mems = (await db_session.execute(
        select(Memory).where(Memory.resident_id == diner.id)
    )).scalars().all()
    assert any("赊" in m.content for m in mems)
    pair = await relation_service.get_pair(db_session, diner.id, host.id)
    assert pair is not None and pair.familiarity > 0


# ── F1.3 wallet-pressure prompt hint ───────────────────────────────────

def test_wallet_pressure_hint_in_prompt():
    from app.agent.prompts import build_decision_prompt
    from app.agent.actions import ActionType

    poor = _res("poor", "穷人", {"key": "shop_keeper",
                "prompt_hint": "你打理杂货铺", "perks": {}})
    poor.meta_json = {**(poor.meta_json or {}), "wallet": 0}
    system, _ = build_decision_prompt(
        poor, "工作时段", "10:00", [], [], [], [ActionType.WORK], 20,
    )
    assert "手头很紧" in system

    rich = _res("rich", "富人", {"key": "shop_keeper",
                "prompt_hint": "你打理杂货铺", "perks": {}})
    rich.meta_json = {**(rich.meta_json or {}), "wallet": 999}
    system2, _ = build_decision_prompt(
        rich, "工作时段", "10:00", [], [], [], [ActionType.WORK], 20,
    )
    assert "手头很紧" not in system2


# ── F1.4 resident-made goods ───────────────────────────────────────────

@pytest.mark.anyio
async def test_resident_work_listed_and_sold(db_session, monkeypatch):
    from app.models.shop import Item
    from app.models.user import User
    from app.services import shop_service
    import app.services.shop_effects  # noqa: F401 register handlers

    # Force the listing probability to 1 so the sketch always lists an item.
    monkeypatch.setattr(duty_service.random, "random", lambda: 0.0)

    artist = _res("alan", "阿岚", {"key": "street_artist", "perks": {"sketch_radius": 8}},
                  district="central_plaza", tile_x=70, tile_y=56)
    subject = _res("subj", "路人", tile_x=71, tile_y=56, district="central_plaza")
    buyer = User(id="buyer", name="Buyer", email="b@x.io", soul_coin_balance=100)
    db_session.add_all([artist, subject, buyer])
    await db_session.commit()

    await duty_service.on_work(db_session, artist)
    item = (await db_session.execute(
        select(Item).where(Item.kind == "resident_work")
    )).scalars().one()
    assert item.payload_json["creator_slug"] == "alan"
    assert item.payload_json["stock"] == settings.npc_work_item_stock

    before = await coin_service.treasury_balance(db_session, "alan")
    res = await shop_service.purchase(db_session, "buyer", item.code, qty=1)
    assert res["ok"]
    after = await coin_service.treasury_balance(db_session, "alan")
    assert after == before + item.price_sc
    await db_session.refresh(item)
    assert item.payload_json["stock"] == settings.npc_work_item_stock - 1


# ── F1.5 集市日 ────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_market_day_event_scheduled(db_session, monkeypatch):
    from app.tasks import event_templates
    from app.models.world_event import WorldEvent

    monkeypatch.setattr(event_templates.random, "random", lambda: 1.0)  # no news
    # 2026-07-25 is a Saturday (weekday 5).
    await event_templates.ensure_scheduled_events(db_session, date(2026, 7, 25))
    ev = (await db_session.execute(
        select(WorldEvent).where(WorldEvent.title == "集市日")
    )).scalars().first()
    assert ev is not None
    assert (ev.payload_json or {}).get("market_day") is True
    assert (ev.payload_json or {}).get("location_id") == "market_hall"
    assert "集市大厅" in ev.description


@pytest.mark.anyio
async def test_market_day_discounts_shop(db_session, monkeypatch):
    from datetime import datetime, timedelta, UTC
    from app.models.world_event import WorldEvent
    from app.models.shop import Item
    from app.models.user import User
    from app.services import shop_service, world_event_service

    now = datetime.now(UTC)
    db_session.add_all([
        WorldEvent(type="festival", title="集市日", description="",
                   payload_json={"market_day": True}, is_active=True,
                   starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=6)),
        Item(code="widget", kind="consumable", name="小物", price_sc=100),
        User(id="shopper", name="S", email="s@x.io", soul_coin_balance=1000),
    ])
    await db_session.commit()
    world_event_service.invalidate_active_cache()

    res = await shop_service.purchase(db_session, "shopper", "widget", qty=1)
    assert res["total_sc"] == round(100 * settings.market_day_discount)
