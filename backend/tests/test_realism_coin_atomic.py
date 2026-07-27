"""Realism P0-5d: reward_creator_passive is atomic (no stale ORM overwrite)."""
import pytest
from sqlalchemy import select

from app.models.user import User
from app.services import coin_service


@pytest.mark.anyio
async def test_reward_creator_passive_no_stale_overwrite(db_session):
    u = User(id="u1", name="u", email="u@x", soul_coin_balance=10)
    db_session.add(u)
    await db_session.commit()

    # Identity-map the user at 10, then an atomic charge lowers the DB balance to
    # 5 (synchronize_session=False leaves the mapped object stale at 10).
    _stale = await db_session.get(User, "u1")
    assert _stale.soul_coin_balance == 10
    ok = await coin_service.charge(db_session, "u1", 5, "spend")
    assert ok is True

    payload = await coin_service.reward_creator_passive(db_session, "u1", "res")
    # +1 on the REAL balance (5) → 6, not the stale 10 → 11.
    assert payload is not None and payload["new_balance"] == 6
    fresh = (await db_session.execute(
        select(User.soul_coin_balance).where(User.id == "u1")
    )).scalar_one()
    assert fresh == 6


@pytest.mark.anyio
async def test_reward_creator_passive_system_and_missing(db_session):
    assert await coin_service.reward_creator_passive(db_session, "system", "r") is None
    assert await coin_service.reward_creator_passive(db_session, "nobody", "r") is None


@pytest.mark.anyio
async def test_reward_creator_passive_skips_every_sentinel(db_session):
    """seed 把内置 NPC 的 creator_id 写成 SYSTEM_CREATOR_ID（UUID），而哨兵只挡
    字面量 "system" —— 于是每一轮内置 NPC 对话都在给 System 账号铸币，
    并污染 admin 经济面板的 total_issued / net_circulation。"""
    from sqlalchemy import select

    from app.models.user import User
    from app.services.system_users import NON_USER_CREATOR_IDS, SYSTEM_CREATOR_ID

    db_session.add(User(id=SYSTEM_CREATOR_ID, name="System",
                        email="system-mint@test.com", soul_coin_balance=0))
    await db_session.commit()

    for sentinel in NON_USER_CREATOR_IDS:
        assert await coin_service.reward_creator_passive(db_session, sentinel, "r") is None

    balance = (await db_session.execute(
        select(User.soul_coin_balance).where(User.id == SYSTEM_CREATOR_ID)
    )).scalar_one()
    assert balance == 0, "the seed system account must never be credited"

    # creator_id 是 nullable（注销会置 NULL），不能炸也不能铸币
    assert await coin_service.reward_creator_passive(db_session, None, "r") is None
