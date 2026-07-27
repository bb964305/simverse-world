"""非人类 ``creator_id`` 哨兵账号。

两个刻意分开的哨兵：

- ``SYSTEM_CREATOR_ID`` —— seed 内置角色班底的所有者（``seed/preset_characters.py``
  的 ``SYSTEM_USER_ID``，由 ``seed_residents.ensure_system_user()`` 建行）。
- ``ADMIN_CREATOR_ID``  —— 通过 admin 控制台创建的居民的所有者。

两者都**必须**在 ``users`` 表里有真实行：``residents.creator_id`` 是
``ForeignKey("users.id")``（``app/models/resident.py``）。admin 建预设居民一直写字面量
``"system"`` 而从没有人建过这一行，所以生产 PostgreSQL 会直接拒绝插入（sqlite
测试不强制外键，所以一直没被发现）。

``docs/plans/2026-07-27-T-ops.md`` 的 F2/T 线用同名同值的三个常量；那条线执行时
从本模块 import，不要再声明第二份。
"""
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

SYSTEM_CREATOR_ID = "00000000-0000-0000-0000-000000000001"
ADMIN_CREATOR_ID = "system"
NON_USER_CREATOR_IDS = frozenset({SYSTEM_CREATOR_ID, ADMIN_CREATOR_ID})


async def ensure_admin_creator_user(db: AsyncSession) -> None:
    """Make sure the ``users`` row admin-created residents point at exists.

    Race-safe under concurrent callers, not just repeat calls within one
    session: the SELECT-then-INSERT below has a window where two callers can
    both miss the SELECT before either commits — exactly what happens when
    two concurrent ``POST /admin/residents/presets`` requests both reach this
    self-heal call before the sentinel row exists (production, where the
    bootstrap seed path may not have run). The loser's INSERT then collides
    with the winner's on the unique ``users.id``/``users.email`` columns;
    that surfaces as ``IntegrityError`` when ``commit()`` flushes it. We
    treat that as success — the row exists now, which is the only
    postcondition this function promises — and roll back the loser's failed
    transaction so its session stays usable for whatever the caller does
    next (``create_preset`` goes straight on to its own insert+commit; a
    poisoned transaction would just move the bug there).

    Deliberately not an admin account. Reliably protected from ever carrying
    a balance: every credit path that can reach a resident's creator now
    imports ``NON_USER_CREATOR_IDS`` from this module and skips both ids —
    ``coin_service.reward_creator_passive``, ``shop_effects.py``'s
    ``gift_share:``/``tip_share:`` payouts (their sentinel check is separate
    from the ``_skim_town_tax`` call they share an ``if`` with, so narrowing
    the payout guard does not also skip the town-tax skim),
    ``lab_terminalization_service.py``'s ``lab_reward:`` split (lab line, off
    by default), ``ws/handlers/rating.py``'s ``good_rating:`` reward, and
    ``investment_service.py``'s invest notification. See
    ``docs/ADMIN_BOOTSTRAP.md`` for the account summary.
    """
    existing = await db.execute(select(User).where(User.id == ADMIN_CREATOR_ID))
    if existing.scalar_one_or_none():
        return
    db.add(User(
        id=ADMIN_CREATOR_ID,
        name="Admin Console",
        email="admin-console@skills.world",
        soul_coin_balance=0,
        is_admin=False,
    ))
    try:
        await db.commit()
    except IntegrityError:
        # Lost the race: a concurrent caller's INSERT for this same row won.
        # Roll back our own failed attempt — the row exists either way.
        await db.rollback()
