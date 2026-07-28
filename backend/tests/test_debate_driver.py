"""E3/#3-1 辩论生命周期推进器。

run_live 与 settle 在 app/ 下零调用方（debate_service.py:57-58 的注释自己
承认了），辩论建出来就停在 announced：不产生辩词、不开投票、不结算，而
stake 接口是开放的且真扣币——玩家的押注币被永久冻结。生产上 1c00ba36 冻结
了一笔 10 SC 超过 2 天。
"""
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.debate import Debate, DebateStake
from app.models.resident import Resident
from app.models.user import User
from app.services import debate_service as ds


def _mock_client(text="我方观点更站得住脚。"):
    client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    client.messages.create = AsyncMock(return_value=resp)
    return client


async def _user(db, email, bal=1000):
    u = User(name="u", email=email, soul_coin_balance=bal)
    db.add(u)
    await db.commit()
    return u


async def _residents(db):
    db.add_all([
        Resident(slug="ann", name="安", creator_id="system", district="cafe",
                 status="idle", tile_x=1, tile_y=1),
        Resident(slug="bo", name="波", creator_id="system", district="cafe",
                 status="idle", tile_x=2, tile_y=2),
    ])
    await db.commit()


async def _debate(db, *, status="announced", age_min=0):
    await _residents(db)
    d = await ds.create_debate(db, "猫和狗谁更好", "ann", "bo")
    d.status = status
    d.starts_at = datetime.now(UTC) - timedelta(minutes=age_min)
    await db.commit()
    return d


@pytest.mark.anyio
async def test_announced_inside_the_stake_window_is_left_alone(db_session):
    """押注窗口没满就开打 = 提前掐断玩家下注。"""
    d = await _debate(db_session, age_min=1)
    with patch("app.llm.client.get_client", return_value=_mock_client()):
        moved = await ds.drive_due_debates(db_session)
    assert moved["live"] == 0
    await db_session.refresh(d)
    assert d.status == "announced"


@pytest.mark.anyio
async def test_announced_past_the_stake_window_goes_live_then_voting(db_session):
    d = await _debate(db_session, age_min=settings.debate_stake_window_min + 1)
    with patch("app.llm.client.get_client", return_value=_mock_client()), \
         patch("app.llm.metering.record_usage", new_callable=AsyncMock):
        moved = await ds.drive_due_debates(db_session)
    assert moved["live"] == 1
    await db_session.refresh(d)
    assert d.status == "voting"
    assert len(d.transcript_json) == ds.ROUNDS


@pytest.mark.anyio
async def test_voting_past_the_vote_window_settles_and_pays_out(db_session):
    d = await _debate(db_session)
    a1 = await _user(db_session, "drv-a@d.com", bal=1000)
    b1 = await _user(db_session, "drv-b@d.com", bal=1000)
    await ds.stake(db_session, d.id, a1.id, "a", 100)
    await ds.stake(db_session, d.id, b1.id, "b", 100)
    d.status = "voting"
    d.votes_a = 5  # a 胜
    d.starts_at = datetime.now(UTC) - timedelta(
        minutes=settings.debate_stake_window_min + settings.debate_vote_window_min + 1)
    await db_session.commit()

    moved = await ds.drive_due_debates(db_session)
    assert moved["settled"] == 1
    await db_session.refresh(d)
    assert d.status == "settled" and d.winner == "a"
    stakes = (await db_session.execute(
        select(DebateStake).where(DebateStake.debate_id == d.id))).scalars().all()
    assert all(s.payout is not None for s in stakes)


@pytest.mark.anyio
async def test_voting_inside_the_vote_window_is_left_alone(db_session):
    d = await _debate(db_session, status="voting",
                      age_min=settings.debate_stake_window_min + 1)
    moved = await ds.drive_due_debates(db_session)
    assert moved["settled"] == 0
    await db_session.refresh(d)
    assert d.status == "voting"


@pytest.mark.anyio
async def test_a_debate_stuck_past_the_deadline_refunds_every_stake(db_session):
    """兜底：无论卡在哪个非终态，超过 stuck_hours 一律平局全额退款。

    这条正是生产上 1c00ba36 的处境——建于 07-26，到 07-28 仍是 announced，
    玩家 10 SC 冻结。上线后第一个 tick 必须把它捞走。
    """
    d = await _debate(db_session, age_min=settings.debate_stuck_hours * 60 + 1)
    u = await _user(db_session, "stuck@d.com", bal=1000)
    await ds.stake(db_session, d.id, u.id, "a", 10)
    await db_session.refresh(u)
    assert u.soul_coin_balance == 990

    moved = await ds.drive_due_debates(db_session)
    assert moved["refunded"] == 1
    await db_session.refresh(d)
    assert d.status == "settled" and d.winner == "draw"
    await db_session.refresh(u)
    assert u.soul_coin_balance == 1000  # 全额退回


@pytest.mark.anyio
async def test_stuck_sweep_takes_priority_over_going_live(db_session):
    """超期的 announced 走退款，不该先被 run_live 拉起来再跑一场 LLM。"""
    d = await _debate(db_session, age_min=settings.debate_stuck_hours * 60 + 1)
    client = _mock_client()
    with patch("app.llm.client.get_client", return_value=client):
        moved = await ds.drive_due_debates(db_session)
    assert moved["refunded"] == 1 and moved["live"] == 0
    client.messages.create.assert_not_called()


@pytest.mark.anyio
async def test_settled_debates_are_never_touched_again(db_session):
    d = await _debate(db_session, status="settled",
                      age_min=settings.debate_stuck_hours * 60 + 1)
    d.winner = "a"
    await db_session.commit()
    moved = await ds.drive_due_debates(db_session)
    assert moved == {"live": 0, "settled": 0, "refunded": 0}


@pytest.mark.anyio
async def test_one_failing_debate_does_not_block_the_others(db_session):
    """每条独立 try/except——一场辩论炸了不能让整轮 cron 停摆。"""
    good = await _debate(db_session, age_min=settings.debate_stake_window_min + 1)
    bad = Debate(topic="坏辩论", resident_a_slug="ghost-a",
                 resident_b_slug="ghost-b", status="announced",
                 starts_at=datetime.now(UTC) - timedelta(
                     minutes=settings.debate_stake_window_min + 1))
    db_session.add(bad)
    await db_session.commit()

    calls = {"n": 0}
    real_run_live = ds.run_live

    async def _flaky(db, debate):
        calls["n"] += 1
        if debate.id == bad.id:
            raise RuntimeError("boom")
        return await real_run_live(db, debate)

    with patch.object(ds, "run_live", _flaky), \
         patch("app.llm.client.get_client", return_value=_mock_client()), \
         patch("app.llm.metering.record_usage", new_callable=AsyncMock):
        moved = await ds.drive_due_debates(db_session)

    assert calls["n"] == 2       # 两条都试过了
    assert moved["live"] == 1    # 好的那条成功推进
    await db_session.refresh(good)
    assert good.status == "voting"


def test_event_cron_wires_the_debate_driver():
    """接线本身是回归面：推进器写好了但没人调 = 什么都没修。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app" / "tasks"
           / "event_cron.py").read_text(encoding="utf-8")
    assert "drive_due_debates" in src, "event_cron 必须调用辩论推进器"
