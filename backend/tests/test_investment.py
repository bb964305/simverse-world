"""E13 goal investment: invest validation/cap, three settlement paths."""

import pytest
from sqlalchemy import select

from app.models.user import User
from app.models.resident import Resident
from app.models.memory import Memory
from app.models.goal_investment import GoalInvestment


async def _user(db, email, bal=1000):
    u = User(name="u", email=email, soul_coin_balance=bal)
    db.add(u)
    await db.commit()
    return u


async def _goal(db, creator_id="system", kind="life", status="active"):
    r = Resident(slug="klaus", name="克劳斯", creator_id=creator_id, district="cafe", status="idle", tile_x=1, tile_y=1)
    db.add(r)
    await db.commit()
    from app.services.goal_service import create_goal
    g = await create_goal(db, r.id, "开咖啡馆", "热爱咖啡", kind=kind)
    if status != "active":
        g.status = status
        await db.commit()
    return r, g


@pytest.mark.anyio
async def test_invest_charges_and_records(db_session):
    from app.services.investment_service import invest
    creator = await _user(db_session, "cr@i.com", bal=0)
    investor = await _user(db_session, "inv@i.com", bal=1000)
    res, goal = await _goal(db_session, creator_id=creator.id)

    inv = await invest(db_session, investor.id, goal.id, 100)
    assert inv.amount == 100 and inv.status == "active"
    await db_session.refresh(investor)
    assert investor.soul_coin_balance == 900

    mems = (await db_session.execute(select(Memory).where(Memory.source == "investment"))).scalars().all()
    assert len(mems) == 1 and mems[0].importance == 0.85


@pytest.mark.anyio
async def test_invest_rejects_non_active_and_bounds(db_session):
    from app.services.investment_service import invest, InvestmentError
    investor = await _user(db_session, "b@i.com")
    _, goal = await _goal(db_session, status="achieved")  # not active
    with pytest.raises(InvestmentError):
        await invest(db_session, investor.id, goal.id, 100)

    _, active_goal = await _goal2(db_session)
    with pytest.raises(InvestmentError):
        await invest(db_session, investor.id, active_goal.id, 10)  # below min


async def _goal2(db):
    r = Resident(slug="maria", name="玛丽亚", creator_id="system", district="cafe", status="idle", tile_x=2, tile_y=2)
    db.add(r)
    await db.commit()
    from app.services.goal_service import create_goal
    g = await create_goal(db, r.id, "环游世界", "想看世界", kind="life")
    return r, g


@pytest.mark.anyio
async def test_pool_cap(db_session):
    from app.services.investment_service import invest, InvestmentError
    investor = await _user(db_session, "cap@i.com", bal=5000)
    _, goal = await _goal(db_session)
    # 4 × 500 = 2000 (cap), 5th rejected.
    for _ in range(4):
        await invest(db_session, investor.id, goal.id, 500)
    with pytest.raises(InvestmentError):
        await invest(db_session, investor.id, goal.id, 50)


@pytest.mark.anyio
async def test_settle_achieved_dividend(db_session):
    from app.services.investment_service import invest, settle_goal_investments
    investor = await _user(db_session, "a@i.com", bal=1000)
    _, goal = await _goal(db_session)
    await invest(db_session, investor.id, goal.id, 100)  # balance 900

    await settle_goal_investments(db_session, goal.id, "achieved")
    await db_session.refresh(investor)
    assert investor.soul_coin_balance == 900 + 150  # 1.5x dividend
    inv = (await db_session.execute(select(GoalInvestment))).scalar_one()
    assert inv.status == "paid" and inv.payout == 150


@pytest.mark.anyio
async def test_settle_failed_half_refund(db_session):
    from app.services.investment_service import invest, settle_goal_investments
    investor = await _user(db_session, "f@i.com", bal=1000)
    _, goal = await _goal(db_session)
    await invest(db_session, investor.id, goal.id, 100)

    await settle_goal_investments(db_session, goal.id, "failed")
    await db_session.refresh(investor)
    assert investor.soul_coin_balance == 900 + 50  # 50% refund


@pytest.mark.anyio
async def test_invest_notify_skips_sentinel_creator(db_session):
    """invest() notifies resident.creator_id — guarded only by the bare
    literal "system". Seed NPCs carry creator_id=SYSTEM_CREATOR_ID (a UUID),
    so investing in a built-in NPC's goal piles up Notification rows on the
    sentinel account. No coin is minted here (lesser case than the other
    three sites), but it's the same defect and worth closing for
    consistency."""
    from app.models.notification import Notification
    from app.services.investment_service import invest
    from app.services.system_users import SYSTEM_CREATOR_ID

    db_session.add(User(id=SYSTEM_CREATOR_ID, name="System",
                        email="system-invest@test.com", soul_coin_balance=0))
    await db_session.commit()
    investor = await _user(db_session, "sentinel-invest@i.com", bal=1000)
    _, goal = await _goal(db_session, creator_id=SYSTEM_CREATOR_ID)

    await invest(db_session, investor.id, goal.id, 100)

    notes = (await db_session.execute(
        select(Notification).where(Notification.user_id == SYSTEM_CREATOR_ID)
    )).scalars().all()
    assert notes == [], "invest() must never notify the sentinel account"


@pytest.mark.anyio
async def test_settle_abandoned_full_refund(db_session):
    from app.services.investment_service import invest, settle_goal_investments
    investor = await _user(db_session, "ab@i.com", bal=1000)
    _, goal = await _goal(db_session)
    await invest(db_session, investor.id, goal.id, 100)

    await settle_goal_investments(db_session, goal.id, "abandoned")
    await db_session.refresh(investor)
    assert investor.soul_coin_balance == 1000  # full refund
