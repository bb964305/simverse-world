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


@pytest.mark.anyio
async def test_ensure_admin_creator_user_survives_concurrent_insert_race(db_engine, db_session):
    """Two concurrent ``POST /admin/residents/presets`` requests can both hit
    ``ensure_admin_creator_user`` before the sentinel row exists, both miss
    the SELECT, and both attempt the INSERT. The loser's ``commit()`` then
    collides on the primary key — on PostgreSQL that's a real
    ``IntegrityError``, not a hypothetical. This must not propagate (it
    would surface in ``create_preset`` as a spurious 400 with a raw DB error
    string), and it must not leave the caller's session broken for whatever
    it does next.

    Modeled without real threads: a separate ("winner") session commits the
    row for real and closes, then this call's own existence-check is forced
    to report "missing" — reproducing the actual race window where its
    SELECT ran before the winner's commit was visible — so its own INSERT
    genuinely collides with the real row already in the database (a DB-level
    constraint violation, not an in-session identity conflict, since this
    session's identity map never saw the winner's row).
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

    winner_factory = async_sessionmaker(db_engine, class_=_AsyncSession, expire_on_commit=False)
    async with winner_factory() as winner:
        await ensure_admin_creator_user(winner)

    real_execute = db_session.execute

    class _Miss:
        def scalar_one_or_none(self):
            return None

    async def execute_forcing_one_miss(statement, *args, **kwargs):
        db_session.execute = real_execute  # only fake the very next call
        return _Miss()

    db_session.execute = execute_forcing_one_miss

    await ensure_admin_creator_user(db_session)  # must not raise

    count = (await db_session.execute(
        select(func.count(User.id)).where(User.id == ADMIN_CREATOR_ID)
    )).scalar()
    assert count == 1
