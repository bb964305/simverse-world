"""S1-5 镇财政闭环 — TreasuryService / 税 hook / funded wage / nightly / REST-WS.

Spec: archive/2026-07-25/docs/kickoffs/KICKOFF_S1-5_treasury.md §5 (test names are
taken verbatim from that section).

Every gated path carries a "gate off → byte-level status quo" assertion: the
module's single master switch is ``settings.town_treasury_enabled`` and it
defaults to False, so the whole suite must pin it explicitly.
"""
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.models.town_treasury import TOWN_KEY, TownTreasury
from app.services import treasury_service


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --------------------------------------------------------------------------- #
# Task 1 — town_treasuries table + model + migration                          #
# --------------------------------------------------------------------------- #

def test_town_treasury_migration_single_head():
    """`alembic heads` stays single-headed in this worktree and the S1-5
    migration chains onto the measured head (047_add_issue_stances).

    NOTE (收口): the migration file keeps the ``NNN`` placeholder number — the
    parallel S2-5 line also chains onto 047, so the main session linearizes the
    numbers at merge time and re-runs this single-head assertion.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    script = ScriptDirectory.from_config(Config(str(ini)))
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic multi-head: {heads}"
    rev = script.get_revision("NNN_add_town_treasury")
    assert rev is not None
    assert rev.down_revision == "047_add_issue_stances"


def test_town_treasury_model_shape():
    """Mirrors resident_treasuries: slug-ish PK + balance_sc + updated_at."""
    cols = TownTreasury.__table__.columns
    assert TownTreasury.__tablename__ == "town_treasuries"
    assert cols["key"].primary_key is True
    assert isinstance(cols["key"].type, sa.String)
    assert cols["key"].type.length == 100
    assert isinstance(cols["balance_sc"].type, sa.Integer)
    assert isinstance(cols["updated_at"].type, sa.DateTime)
    assert cols["updated_at"].type.timezone is True
    assert TOWN_KEY == "town"


@pytest.mark.anyio
async def test_town_treasuries_table_created(db_engine):
    """models/__init__.py registers the model so Base.metadata.create_all
    (the main.py / conftest test path) sees the new table."""
    async with db_engine.connect() as conn:
        names = await conn.run_sync(lambda sc: sa.inspect(sc).get_table_names())
    assert "town_treasuries" in names


@pytest.mark.anyio
async def test_town_treasury_starts_empty(db_session):
    """The town account is created on demand (upsert), not seeded."""
    rows = (await db_session.execute(select(TownTreasury))).scalars().all()
    assert rows == []


# --------------------------------------------------------------------------- #
# Task 2 — TreasuryService.tax / disburse / balance                            #
# --------------------------------------------------------------------------- #

async def _row(db):
    return (await db.execute(
        select(TownTreasury).execution_options(populate_existing=True)
    )).scalars().first()


@pytest.mark.anyio
async def test_tax_credits_town_balance(db_session):
    await treasury_service.tax(db_session, 100, reason="sales_tax:x")
    assert await treasury_service.balance(db_session) == 100
    # the account row is upserted on demand
    row = await _row(db_session)
    assert row is not None and row.key == TOWN_KEY and row.balance_sc == 100
    # a second credit accumulates on the same row (no duplicate insert)
    await treasury_service.tax(db_session, 5, reason="sales_tax:y")
    assert await treasury_service.balance(db_session) == 105
    assert len((await db_session.execute(select(TownTreasury))).scalars().all()) == 1


@pytest.mark.anyio
async def test_tax_amount_zero_is_noop(db_session):
    """coin_service's ``amount <= 0`` guard is preserved verbatim: no balance
    change and — critically — no row is created."""
    await treasury_service.tax(db_session, 0, reason="zero")
    await treasury_service.tax(db_session, -5, reason="negative")
    assert await treasury_service.balance(db_session) == 0
    assert (await _row(db_session)) is None


@pytest.mark.anyio
async def test_disburse_guarded_decrement(db_session):
    await treasury_service.tax(db_session, 100, reason="seed")
    assert await treasury_service.disburse(db_session, 30, reason="wage:ann") is True
    assert await treasury_service.balance(db_session) == 70


@pytest.mark.anyio
async def test_disburse_insufficient_returns_false_no_exception(db_session):
    await treasury_service.tax(db_session, 10, reason="seed")
    assert await treasury_service.disburse(db_session, 50, reason="wage:ann") is False
    assert await treasury_service.balance(db_session) == 10


@pytest.mark.anyio
async def test_disburse_missing_account_returns_false(db_session):
    """No town row at all → the guard matches 0 rows → False, no row created."""
    assert await treasury_service.disburse(db_session, 1, reason="x") is False
    assert (await _row(db_session)) is None


@pytest.mark.anyio
async def test_disburse_amount_zero_is_noop_false(db_session):
    await treasury_service.tax(db_session, 10, reason="seed")
    assert await treasury_service.disburse(db_session, 0, reason="zero") is False
    assert await treasury_service.disburse(db_session, -3, reason="neg") is False
    assert await treasury_service.balance(db_session) == 10


@pytest.mark.anyio
async def test_concurrent_tax_no_lost_update(db_engine):
    """Two independent sessions crediting the same town row must sum, not clobber.

    A read-modify-write implementation loses one of the two credits here (both
    sessions read the same pre-image); the guarded UPDATE / ON CONFLICT DO UPDATE
    upsert survives it.
    """
    import asyncio
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def credit(amount: int) -> None:
        async with maker() as db:
            await treasury_service.tax(db, amount, reason="concurrent")

    # first credit creates the row; the racing pair then both hit the conflict path
    await credit(10)
    await asyncio.gather(*(credit(7) for _ in range(5)))
    async with maker() as db:
        assert await treasury_service.balance(db) == 10 + 7 * 5


@pytest.mark.anyio
async def test_concurrent_disburse_no_overspend(db_engine):
    """The ``balance_sc >= amount`` guard means concurrent spenders can never
    drive the town account negative — the losers just get False."""
    import asyncio
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as db:
        await treasury_service.tax(db, 100, reason="seed")

    async def spend() -> bool:
        async with maker() as db:
            return await treasury_service.disburse(db, 30, reason="wage")

    results = await asyncio.gather(*(spend() for _ in range(6)))
    async with maker() as db:
        remaining = await treasury_service.balance(db)
    assert remaining >= 0
    assert sum(results) == 3          # 3 × 30 fits in 100, the 4th+ are refused
    assert remaining == 100 - 30 * sum(results)


@pytest.mark.anyio
async def test_no_rollback_on_zero_row_guard(db_session):
    """MissingGreenlet regression gate (coin_service.charge's comment): when the
    guard matches 0 rows nothing was written, so rollback() — which expires every
    ORM object in the caller's session — must NOT be called."""
    await treasury_service.tax(db_session, 10, reason="seed")
    calls = []
    original = db_session.rollback

    async def _spy():
        calls.append(1)
        await original()

    db_session.rollback = _spy
    try:
        assert await treasury_service.disburse(db_session, 999, reason="too much") is False
        assert await treasury_service.disburse(db_session, 0, reason="zero") is False
    finally:
        db_session.rollback = original
    assert calls == []


@pytest.mark.anyio
async def test_town_treasury_not_in_transactions_ledger(db_session):
    """transactions.user_id is a hard FK to users.id, so the synthetic town
    account cannot be a ledger row (deliberate deviation, model docstring)."""
    from app.models.transaction import Transaction

    await treasury_service.tax(db_session, 50, reason="sales_tax:x")
    await treasury_service.disburse(db_session, 20, reason="wage:ann")
    rows = (await db_session.execute(select(Transaction))).scalars().all()
    assert rows == []
