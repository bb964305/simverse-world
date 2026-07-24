"""M6 mayor-election tests: open, install, wage bonus, re-election handover."""
import pytest
from sqlalchemy import select

from app.config import settings
from app.models.resident import Resident
from app.models.season import Poll, Vote
from app.services import civic_service, election_service, duty_service, coin_service


def _res(slug, name, sbti=None, duty=None, **kw):
    meta = {}
    if sbti:
        meta["sbti"] = {"dimensions": sbti}
    if duty:
        meta["duty"] = duty
    d = dict(slug=slug, name=name, district="central_plaza", status="idle",
             resident_type="npc", creator_id="sys", tile_x=70, tile_y=56,
             meta_json=meta or None)
    d.update(kw)
    return Resident(**d)


@pytest.mark.anyio
async def test_open_election_picks_ambitious_candidates(db_session):
    clerk = _res("zhao", "赵启文", duty={"key": "town_clerk"})
    db_session.add_all([
        clerk,
        _res("amb1", "野心家甲", sbti={"Ac1": "H"}),
        _res("amb2", "野心家乙", sbti={"So1": "H"}),
        _res("meek", "佛系", sbti={"Ac1": "L", "So1": "L"}),
    ])
    await db_session.commit()

    poll = await election_service.open_election(db_session)
    assert poll is not None
    labels = {o["label"] for o in poll.options_json}
    assert "野心家甲" in labels and "野心家乙" in labels
    assert "佛系" not in labels


@pytest.mark.anyio
async def test_election_close_installs_mayor_and_bonus(db_session):
    clerk = _res("zhao", "赵启文", duty={"key": "town_clerk"})
    a = _res("cand_a", "候选甲", sbti={"Ac1": "H"},
             duty={"key": "shop_keeper", "perks": {"wage_sc": 10}}, district="shop",
             tile_x=84, tile_y=48)
    b = _res("cand_b", "候选乙", sbti={"So1": "H"})
    db_session.add_all([clerk, a, b])
    await db_session.commit()

    poll = await election_service.open_election(db_session, days=0)
    # everyone votes for candidate甲 (option index of cand_a)
    idx_a = next(i for i, o in enumerate(poll.options_json) if o["effect"]["slug"] == "cand_a")
    db_session.add(Vote(poll_id=poll.id, user_id="u1", option_idx=idx_a))
    await db_session.commit()

    await civic_service.close_due_polls(db_session)
    await db_session.refresh(a)
    assert (a.meta_json or {}).get("mayor") is True
    assert await election_service.current_mayor(db_session) == "cand_a"

    # mayor earns the town-wide wage bonus on WORK
    from app.models.shop import Item
    db_session.add(Item(code="x", kind="consumable", name="X", price_sc=5))
    await db_session.commit()
    await duty_service.on_work(db_session, a)
    expected = round(10 * settings.election_mayor_wage_bonus)
    assert await coin_service.treasury_balance(db_session, "cand_a") == expected


@pytest.mark.anyio
async def test_reelection_hands_over_mayor_flag(db_session):
    old = _res("old", "老镇长", sbti={"Ac1": "H"})
    old.meta_json = {"sbti": {"dimensions": {"Ac1": "H"}}, "mayor": True}
    new = _res("new", "新镇长", sbti={"So1": "H"})
    db_session.add_all([old, new])
    await db_session.commit()

    assert await election_service.install_mayor(db_session, "new") is True
    await db_session.refresh(old)
    await db_session.refresh(new)
    assert (old.meta_json or {}).get("mayor") in (None, False)
    assert (new.meta_json or {}).get("mayor") is True


@pytest.mark.anyio
async def test_election_disabled(db_session, monkeypatch):
    monkeypatch.setattr(settings, "election_enabled", False)
    assert await election_service.open_election(db_session) is None


# ── nightly trigger (the piece that makes M6 actually fire) ────────────

@pytest.mark.anyio
async def test_seasonal_trigger_once_per_active_season(db_session):
    from datetime import datetime, timedelta, UTC
    from app.models.season import Season

    db_session.add_all([
        _res("amb1", "野心家甲", sbti={"Ac1": "H"}),
        _res("amb2", "野心家乙", sbti={"So1": "H"}),
        Season(title="夏日赛季", status="active",
               starts_at=datetime.now(UTC) - timedelta(days=1),
               ends_at=datetime.now(UTC) + timedelta(days=13)),
    ])
    await db_session.commit()

    poll = await election_service.maybe_open_seasonal_election(db_session)
    assert poll is not None and election_service.ELECTION_TAG in poll.question

    # while it's open → no second election
    assert await election_service.maybe_open_seasonal_election(db_session) is None

    # closed, but the season already held its election → still no re-open
    poll.status = "closed"
    await db_session.commit()
    assert await election_service.maybe_open_seasonal_election(db_session) is None


@pytest.mark.anyio
async def test_offseason_trigger_respects_interval(db_session, monkeypatch):
    db_session.add_all([
        _res("amb1", "野心家甲", sbti={"Ac1": "H"}),
        _res("amb2", "野心家乙", sbti={"So1": "H"}),
    ])
    await db_session.commit()

    poll = await election_service.maybe_open_seasonal_election(db_session)
    assert poll is not None

    poll.status = "closed"
    await db_session.commit()
    # opened today, interval 28 → not due again
    assert await election_service.maybe_open_seasonal_election(db_session) is None
    # interval elapsed → due again
    monkeypatch.setattr(settings, "election_interval_days", 0)
    assert await election_service.maybe_open_seasonal_election(db_session) is not None


@pytest.mark.anyio
async def test_seasonal_trigger_gated(db_session, monkeypatch):
    monkeypatch.setattr(settings, "election_enabled", False)
    assert await election_service.maybe_open_seasonal_election(db_session) is None
