"""S1-5 镇财政 — the town's public account (tax in, wages / public spending out).

Deliberately a *thin* wrapper over the atomic write idioms ``coin_service``
already proves against ``resident_treasuries``: same guarded UPDATE / dialect
upsert, same three hard API rules. Only the table and the PK column differ.

Three hard rules copied verbatim from ``coin_service`` (violating any of them
is a bug, so they are restated here):

1. ``amount <= 0`` is a silent no-op (``tax``) / ``False`` (``disburse``) — never
   an exception, never a row write.
2. When a guard matches **0 rows, never call ``db.rollback()``**. Nothing was
   written, so there is nothing to undo, and rollback expires EVERY ORM object
   in the caller's session → ``MissingGreenlet`` on the next lazy attribute
   access under asyncio (see ``coin_service.charge``'s comment). Rollback only
   after a real write (the IntegrityError upsert retry below).
3. ``synchronize_session=False`` leaves already-loaded ORM rows stale — callers
   must re-SELECT (``balance()``) rather than read a cached object. The funded
   wage path additionally refreshes ``duty_service.set_wallet_cache``.

Auditability: town flows are NOT ledger rows (``transactions.user_id`` is a
users.id FK — see ``app/models/town_treasury.py``); ``balance_sc`` +
``updated_at`` are the audit surface, and the nightly job stamps
``town_last_spend_at`` through ``ConfigService``.

INTERFACE FREEZE (S1-5 §8 downstream contract). ``tax`` / ``disburse`` /
``balance`` are consumed by S2-5 (税率进政策表), S2-2 (镇长财政排序权), S5-8
(医疗补贴) and S5-9 (遗产充公). Their signatures are frozen:

    async def tax(db, amount: int, reason: str = "") -> None
    async def disburse(db, amount: int, reason: str = "") -> bool
    async def balance(db) -> int
"""
from __future__ import annotations

import logging
from datetime import datetime, UTC

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.town_treasury import TOWN_KEY, TownTreasury

logger = logging.getLogger(__name__)

# ConfigService key stamped by the nightly public-spending job (§2 任务 5): the
# scalar policy state lives in system_config, not in new columns.
LAST_SPEND_KEY = "town_last_spend_at"


async def balance(db: AsyncSession) -> int:
    """The town's current balance; 0 when the account row does not exist yet
    (mirrors ``coin_service.treasury_balance``)."""
    row = await db.execute(
        select(TownTreasury.balance_sc).where(TownTreasury.key == TOWN_KEY)
    )
    return row.scalar_one_or_none() or 0


async def tax_pending(db: AsyncSession, amount: int, reason: str = "") -> None:
    """Flush-owned town credit (upsert). The caller owns the transaction.

    Mirrors ``coin_service.treasury_credit_pending``: dialect-native
    ``ON CONFLICT DO UPDATE`` on postgres/sqlite, and on any other dialect the
    guarded UPDATE → insert-when-zero-rows fallback.
    """
    if amount <= 0:
        return
    now = datetime.now(UTC)
    values = {"key": TOWN_KEY, "balance_sc": amount, "updated_at": now}
    dialect = db.get_bind().dialect.name
    if dialect in ("postgresql", "sqlite"):
        insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
        statement = insert(TownTreasury).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[TownTreasury.key],
            set_={
                "balance_sc": TownTreasury.balance_sc + amount,
                "updated_at": now,
            },
        )
        await db.execute(statement)
    else:
        result = await db.execute(
            update(TownTreasury)
            .where(TownTreasury.key == TOWN_KEY)
            .values(balance_sc=TownTreasury.balance_sc + amount, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:
            db.add(TownTreasury(**values))
    await db.flush()


async def tax(db: AsyncSession, amount: int, reason: str = "") -> None:
    """Credit the town treasury (sales tax / gift tax / fines / escheat).

    ``amount <= 0`` is a silent no-op. The town row is created on demand.
    ``reason`` is accepted for call-site readability and symmetry with
    ``coin_service`` but is not persisted — there is no town ledger table (see
    the module docstring).
    """
    if amount <= 0:
        return
    await tax_pending(db, amount, reason)
    await db.commit()


async def disburse(db: AsyncSession, amount: int, reason: str = "") -> bool:
    """Spend from the town treasury (wage funding / public works / subsidies).

    Atomic ``UPDATE ... WHERE key = 'town' AND balance_sc >= amount`` — returns
    False on insufficient funds (or a missing account) rather than raising, and
    NEVER rolls back on the zero-row path (rule 2 above).
    """
    if amount <= 0:
        return False
    result = await db.execute(
        update(TownTreasury)
        .where(TownTreasury.key == TOWN_KEY, TownTreasury.balance_sc >= amount)
        .values(
            balance_sc=TownTreasury.balance_sc - amount,
            updated_at=datetime.now(UTC),
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return (result.rowcount or 0) > 0
