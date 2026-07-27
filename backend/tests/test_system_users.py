"""非人类 creator_id 哨兵的真源与幂等性。"""
import pytest

from sqlalchemy import select, func

from app.models.user import User
from app.services.system_users import (
    ADMIN_CREATOR_ID,
    NON_USER_CREATOR_IDS,
    SYSTEM_CREATOR_ID,
    ensure_admin_creator_user,
)


def test_constants_do_not_drift_from_seed():
    """seed 那份 SYSTEM_USER_ID 是同一个值的第二个声明点；漂移了必须炸。"""
    from seed.preset_characters import SYSTEM_USER_ID

    assert SYSTEM_CREATOR_ID == SYSTEM_USER_ID
    assert ADMIN_CREATOR_ID == "system"
    assert NON_USER_CREATOR_IDS == frozenset({SYSTEM_CREATOR_ID, ADMIN_CREATOR_ID})


@pytest.mark.anyio
async def test_ensure_admin_creator_user_is_idempotent(db_session):
    """residents.creator_id 是 users.id 的 FK —— 这一行必须真实存在，且只存在一行。"""
    await ensure_admin_creator_user(db_session)
    await ensure_admin_creator_user(db_session)

    count = (await db_session.execute(
        select(func.count(User.id)).where(User.id == ADMIN_CREATOR_ID)
    )).scalar()
    assert count == 1

    row = (await db_session.execute(
        select(User).where(User.id == ADMIN_CREATOR_ID)
    )).scalar_one()
    assert row.is_admin is False, "sentinel row must not be a usable admin account"
    assert row.soul_coin_balance == 0
