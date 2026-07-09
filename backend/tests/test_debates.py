"""E9 debate arena: staking, voting dedup, settlement math (burn/tie), auto-draw."""

import pytest
from sqlalchemy import select

from app.models.user import User
from app.models.resident import Resident
from app.models.debate import Debate, DebateStake
from app.services import debate_service as ds


async def _user(db, email, bal=1000):
    u = User(name="u", email=email, soul_coin_balance=bal)
    db.add(u)
    await db.commit()
    return u


async def _residents(db):
    a = Resident(slug="ann", name="安", creator_id="system", district="cafe", status="idle", tile_x=1, tile_y=1)
    b = Resident(slug="bo", name="波", creator_id="system", district="cafe", status="idle", tile_x=2, tile_y=2)
    db.add_all([a, b])
    await db.commit()
    return a, b


async def _debate(db, status="announced"):
    await _residents(db)
    d = await ds.create_debate(db, "猫和狗谁更好", "ann", "bo")
    if status != "announced":
        d.status = status
        await db.commit()
    return d


@pytest.mark.anyio
async def test_stake_charges_and_autovotes(db_session):
    d = await _debate(db_session)
    u = await _user(db_session, "s@d.com", bal=500)
    s = await ds.stake(db_session, d.id, u.id, "a", 100)
    assert s.amount == 100 and s.side == "a"
    await db_session.refresh(u)
    assert u.soul_coin_balance == 400
    await db_session.refresh(d)
    assert d.pool_a == 100 and d.votes_a == 1  # stake auto-counts as a vote


@pytest.mark.anyio
async def test_stake_bounds_and_status(db_session):
    d = await _debate(db_session)
    u = await _user(db_session, "b@d.com")
    with pytest.raises(ds.DebateError):
        await ds.stake(db_session, d.id, u.id, "a", 5)     # below min
    with pytest.raises(ds.DebateError):
        await ds.stake(db_session, d.id, u.id, "a", 999)   # above max
    with pytest.raises(ds.DebateError):
        await ds.stake(db_session, d.id, u.id, "c", 50)    # bad side

    d.status = "voting"
    await db_session.commit()
    with pytest.raises(ds.DebateError):
        await ds.stake(db_session, d.id, u.id, "a", 50)    # not open


@pytest.mark.anyio
async def test_double_stake_rejected_no_double_charge(db_session):
    d = await _debate(db_session)
    u = await _user(db_session, "dbl@d.com", bal=500)
    await ds.stake(db_session, d.id, u.id, "a", 100)
    with pytest.raises(ds.DebateError):
        await ds.stake(db_session, d.id, u.id, "b", 100)
    await db_session.refresh(u)
    assert u.soul_coin_balance == 400  # only the first charge stuck
    stakes = (await db_session.execute(select(DebateStake).where(DebateStake.debate_id == d.id))).scalars().all()
    assert len(stakes) == 1


@pytest.mark.anyio
async def test_vote_dedup_and_stage(db_session):
    d = await _debate(db_session, status="voting")
    u = await _user(db_session, "v@d.com")
    await ds.vote(db_session, d.id, u.id, "a")
    await db_session.refresh(d)
    assert d.votes_a == 1
    with pytest.raises(ds.DebateError):
        await ds.vote(db_session, d.id, u.id, "b")  # one vote per user


@pytest.mark.anyio
async def test_settle_winner_split_with_burn(db_session):
    d = await _debate(db_session, status="voting")
    a1 = await _user(db_session, "a1@d.com", bal=1000)
    a2 = await _user(db_session, "a2@d.com", bal=1000)
    b1 = await _user(db_session, "b1@d.com", bal=1000)
    # side a: 100 + 100 = 200 pool; side b: 200 pool.
    d.status = "announced"
    await db_session.commit()
    await ds.stake(db_session, d.id, a1.id, "a", 100)
    await ds.stake(db_session, d.id, a2.id, "a", 100)
    await ds.stake(db_session, d.id, b1.id, "b", 200)
    d.status = "voting"
    # a already has 2 auto-votes, b has 1 → a wins.
    await db_session.commit()

    res = await ds.settle(db_session, d.id)
    assert res["winner"] == "a"
    # loser pool 200, burn = 200 - int(200*0.95)=200-190=10.
    assert res["burn"] == 10
    assert res["distributable"] == 190
    # a1 and a2 each staked 100 of 200 winner pool → each gets 100 + 95 = 195.
    await db_session.refresh(a1)
    await db_session.refresh(a2)
    await db_session.refresh(b1)
    assert a1.soul_coin_balance == 900 + 195
    assert a2.soul_coin_balance == 900 + 195
    assert b1.soul_coin_balance == 800  # loser gets nothing back
    # Conservation: total paid out == winner_pool + distributable.
    assert res["total_paid"] == 200 + 190


@pytest.mark.anyio
async def test_settle_tie_full_refund(db_session):
    d = await _debate(db_session, status="announced")
    a1 = await _user(db_session, "ta@d.com", bal=1000)
    b1 = await _user(db_session, "tb@d.com", bal=1000)
    await ds.stake(db_session, d.id, a1.id, "a", 150)
    await ds.stake(db_session, d.id, b1.id, "b", 150)  # 1 vote each → tie
    d.status = "voting"
    await db_session.commit()

    res = await ds.settle(db_session, d.id)
    assert res["winner"] == "draw"
    await db_session.refresh(a1)
    await db_session.refresh(b1)
    assert a1.soul_coin_balance == 1000  # full refund
    assert b1.soul_coin_balance == 1000


@pytest.mark.anyio
async def test_settle_idempotent(db_session):
    d = await _debate(db_session, status="announced")
    a1 = await _user(db_session, "ia@d.com", bal=1000)
    b1 = await _user(db_session, "ib@d.com", bal=1000)
    await ds.stake(db_session, d.id, a1.id, "a", 100)
    await ds.stake(db_session, d.id, b1.id, "b", 100)
    d.status = "voting"
    d.votes_a = 5  # force a win
    await db_session.commit()
    await ds.settle(db_session, d.id)
    bal_after = (await db_session.execute(select(User.soul_coin_balance).where(User.id == a1.id))).scalar_one()
    second = await ds.settle(db_session, d.id)
    assert second.get("already") is True
    bal_again = (await db_session.execute(select(User.soul_coin_balance).where(User.id == a1.id))).scalar_one()
    assert bal_after == bal_again  # no double payout


@pytest.mark.anyio
async def test_run_live_llm_failure_auto_draws(db_session, monkeypatch):
    d = await _debate(db_session, status="announced")
    a1 = await _user(db_session, "la@d.com", bal=1000)
    b1 = await _user(db_session, "lb@d.com", bal=1000)
    await ds.stake(db_session, d.id, a1.id, "a", 100)
    await ds.stake(db_session, d.id, b1.id, "b", 100)

    class _BadClient:
        class messages:
            @staticmethod
            async def create(**kw):
                raise RuntimeError("no network")

    monkeypatch.setattr("app.llm.client.get_client", lambda owner="system": _BadClient())
    await ds.run_live(db_session, d)
    await db_session.refresh(d)
    assert d.status == "settled" and d.winner == "draw"
    await db_session.refresh(a1)
    await db_session.refresh(b1)
    assert a1.soul_coin_balance == 1000 and b1.soul_coin_balance == 1000  # all refunded


@pytest.mark.anyio
async def test_burn_never_mints_coins(db_session):
    """Property-ish: across pool sizes, total paid out ≤ total staked and burn ≥ 5%."""
    for i, (pool_a_amt, pool_b_amt) in enumerate([(10, 200), (137, 63), (200, 10), (50, 50)]):
        # Build debates directly — settlement math needs no residents.
        d = await ds.create_debate(db_session, f"辩题{i}", f"x{i}", f"y{i}")
        ua = await _user(db_session, f"pa{i}@d.com", bal=1000)
        ub = await _user(db_session, f"pb{i}@d.com", bal=1000)
        await ds.stake(db_session, d.id, ua.id, "a", pool_a_amt)
        await ds.stake(db_session, d.id, ub.id, "b", pool_b_amt)
        d.status = "voting"
        d.votes_a, d.votes_b = 9, 1  # a wins deterministically
        await db_session.commit()
        res = await ds.settle(db_session, d.id)
        total_in = pool_a_amt + pool_b_amt
        assert res["total_paid"] <= total_in
        assert res["burn"] >= int(pool_b_amt * ds.BURN_RATE)
