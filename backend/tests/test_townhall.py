"""Read-only /townhall aggregation router (Society Expansion §10).

The panel is a read-only projection over existing M1–M6 data (duty holders,
open civic proposals, the most recent mayor election, config-projected town
finances) plus a small market-day helper the shop catalog reads. No new tables,
no writes — every source here already exists on the M1–M6 branch.
"""
import pytest
from datetime import datetime, timedelta, UTC

from app.config import settings
from app.models.resident import Resident
from app.models.season import Poll
from app.models.world_event import WorldEvent
from app.services.config_service import ConfigService


def _res(slug, name, duty=None, mayor=False, **kw):
    meta = {}
    if duty:
        meta["duty"] = duty
    if mayor:
        meta["mayor"] = True
    d = dict(slug=slug, name=name, district="central_plaza", status="idle",
             resident_type="npc", creator_id="sys", tile_x=70, tile_y=56,
             meta_json=meta or None)
    d.update(kw)
    return Resident(**d)


async def _seed_town(db):
    """Sitting mayor + a second duty holder + one open civic proposal + one
    closed election poll carrying the standard won/final_votes tags."""
    await ConfigService(db).set("current_mayor", "zhao", group="civic", updated_by="test")
    db.add_all([
        _res("zhao", "赵启文", duty={"key": "town_clerk", "title": "公告与登记处"}, mayor=True),
        _res("qiao", "何巧云", duty={"key": "shop_keeper", "title": "杂货铺掌柜"}),
    ])
    now = datetime.now(UTC)
    # open civic proposal (not an election)
    db.add(Poll(
        id="poll-civic", season_id=None, question="广场是否加装长椅",
        options_json=[{"label": "加装"}, {"label": "维持原样"}],
        closes_at=now + timedelta(days=2), status="open",
    ))
    # closed mayor election with a tagged winner
    db.add(Poll(
        id="poll-elec", season_id=None, question="镇长选举:谁来当下一任镇长?",
        options_json=[
            {"label": "赵启文", "effect": {"type": "mayor", "slug": "zhao"}, "won": True, "final_votes": 7},
            {"label": "何巧云", "effect": {"type": "mayor", "slug": "qiao"}, "final_votes": 3},
        ],
        closes_at=now - timedelta(days=1), status="closed",
    ))
    await db.commit()


@pytest.mark.anyio
async def test_overview_aggregates_mayor_duties_polls_and_finances(client, db_session):
    await _seed_town(db_session)

    resp = await client.get("/townhall/overview")
    assert resp.status_code == 200
    data = resp.json()

    # sitting mayor slug resolved to a display name
    assert data["mayor"]["slug"] == "zhao"
    assert data["mayor"]["name"] == "赵启文"

    # duty holders enumerated from resident meta_json["duty"]
    duty_keys = {d["key"] for d in data["duties"]}
    assert {"town_clerk", "shop_keeper"} <= duty_keys

    # open proposals include the civic poll but NOT the election
    questions = {p["question"] for p in data["open_polls"]}
    assert "广场是否加装长椅" in questions
    assert all(not q.startswith("镇长选举") for q in questions)

    # most recent election result surfaces the winner
    assert data["recent_election"]["winner_slug"] == "zhao"
    assert data["recent_election"]["winner_name"] == "赵启文"

    # town finances projected from config (no treasury table yet)
    fin = data["finances"]
    assert fin["npc_default_wage_sc"] == settings.npc_default_wage_sc
    assert fin["market_day_discount"] == settings.market_day_discount


@pytest.mark.anyio
async def test_market_day_inactive_by_default(client, db_session):
    resp = await client.get("/townhall/market-day")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active"] is False
    assert data["discount"] == 1.0
    assert data["weekday"] == settings.market_day_weekday


@pytest.mark.anyio
async def test_market_day_active_reports_discount(client, db_session):
    now = datetime.now(UTC)
    db_session.add(WorldEvent(
        type="festival", title="集市日", description="",
        payload_json={"market_day": True},
        starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=6),
        is_active=True,
    ))
    await db_session.commit()
    from app.services import world_event_service
    world_event_service.invalidate_active_cache()

    resp = await client.get("/townhall/market-day")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active"] is True
    assert data["discount"] == settings.market_day_discount


@pytest.mark.anyio
async def test_overview_fails_open_when_civic_disabled(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "civic_polls_enabled", False)
    resp = await client.get("/townhall/overview")
    assert resp.status_code == 200
    assert resp.json()["open_polls"] == []


@pytest.mark.anyio
async def test_townhall_router_registered(client):
    # Registered routes answer (non-404); an unregistered router would 404.
    assert (await client.get("/townhall/overview")).status_code != 404
    assert (await client.get("/townhall/market-day")).status_code != 404
