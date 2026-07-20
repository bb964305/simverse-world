"""Real-PostgreSQL ownership race for Lab escrow terminalization.

This is required AC01/AC02/AC03 evidence.  It deliberately fails instead of
skipping when the release driver's disposable PostgreSQL contract is absent.
SQLite cannot prove row-lock/CAS ownership, so it is never an accepted fallback.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401 - populate Base.metadata for the isolated schema
from app.database import Base
from app.models.coin_hold import CoinHold
from app.models.transaction import Transaction
from app.models.user import User
from app.services import coin_service


pytestmark = [pytest.mark.lab_postgres, pytest.mark.anyio]


def _required_postgres() -> tuple[str, str]:
    missing = [
        name
        for name in ("LAB_POSTGRES_REQUIRED", "LAB_TEST_DATABASE_URL", "LAB_RELEASE_RUN_ID")
        if not os.environ.get(name)
    ]
    if missing:
        pytest.fail(
            "real PostgreSQL terminalization evidence requires environment: "
            + ", ".join(missing)
        )
    if os.environ["LAB_POSTGRES_REQUIRED"].lower() not in {"1", "true", "yes", "on"}:
        pytest.fail("LAB_POSTGRES_REQUIRED must be true for required PostgreSQL evidence")

    database_url = os.environ["LAB_TEST_DATABASE_URL"]
    parsed = make_url(database_url)
    if parsed.drivername != "postgresql+asyncpg":
        pytest.fail(
            "LAB_TEST_DATABASE_URL must use postgresql+asyncpg; "
            f"received driver {parsed.drivername!r}"
        )
    return database_url, os.environ["LAB_RELEASE_RUN_ID"]


@pytest.fixture
async def postgres_factory():
    database_url, run_id = _required_postgres()
    schema = f"lab_terminalization_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(database_url)
    test_engine = None

    try:
        async with admin_engine.begin() as connection:
            database, disposable = (
                await connection.execute(
                    text(
                        "SELECT current_database(), "
                        "current_setting('simverse.release_disposable', true)"
                    )
                )
            ).one()
            expected_database = f"simverse_lab_release_{run_id}"
            if database != expected_database or disposable != "on":
                pytest.fail(
                    "LAB_TEST_DATABASE_URL is not the disposable release database: "
                    f"database={database!r}, expected={expected_database!r}, "
                    f"simverse.release_disposable={disposable!r}"
                )
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        test_engine = create_async_engine(
            database_url,
            connect_args={"server_settings": {"search_path": f'"{schema}"'}},
        )
        async with test_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        yield async_sessionmaker(
            test_engine, class_=AsyncSession, expire_on_commit=False
        )
    finally:
        if test_engine is not None:
            await test_engine.dispose()
        try:
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            await admin_engine.dispose()


async def _seed_race_round(
    factory: async_sessionmaker[AsyncSession], round_number: int
) -> tuple[str, str, str]:
    issuer_id = f"race-issuer-{round_number}"
    recipient_id = f"race-recipient-{round_number}"
    hold_id = f"race-hold-{round_number}"
    async with factory() as db:
        db.add_all(
            [
                User(
                    id=issuer_id,
                    name=issuer_id,
                    email=f"{issuer_id}@finance.test",
                    soul_coin_balance=0,
                ),
                User(
                    id=recipient_id,
                    name=recipient_id,
                    email=f"{recipient_id}@finance.test",
                    soul_coin_balance=0,
                ),
                CoinHold(
                    id=hold_id,
                    user_id=issuer_id,
                    amount=1,
                    reason=f"lab_task:race-{round_number}",
                    status="held",
                ),
                Transaction(
                    user_id=issuer_id,
                    amount=-1,
                    reason=f"hold:lab_task:race-{round_number}",
                ),
            ]
        )
        await db.commit()
    return issuer_id, recipient_id, hold_id


async def _race_operation(operation, start: asyncio.Event):
    await start.wait()
    try:
        await operation()
    except coin_service.CoinError as exc:
        return exc
    return None


@pytest.mark.parametrize("rounds", [100])
async def test_settle_vs_refund_has_exactly_one_owner_per_round(
    postgres_factory, rounds: int
):
    """Each held row has one terminal owner and one conservation-valid outcome."""
    factory = postgres_factory

    for round_number in range(rounds):
        issuer_id, recipient_id, hold_id = await _seed_race_round(
            factory, round_number
        )
        start = asyncio.Event()

        async def settle() -> None:
            async with factory() as db:
                await coin_service.settle(
                    db,
                    hold_id,
                    [(recipient_id, 1, f"lab_reward:race-{round_number}")],
                )

        async def refund() -> None:
            async with factory() as db:
                await coin_service.refund(
                    db, hold_id, f"lab_refund:race-{round_number}"
                )

        settle_task = asyncio.create_task(_race_operation(settle, start))
        refund_task = asyncio.create_task(_race_operation(refund, start))
        await asyncio.sleep(0)
        start.set()
        outcomes = await asyncio.gather(settle_task, refund_task)

        async with factory() as db:
            hold = await db.get(CoinHold, hold_id)
            balances = dict(
                (
                    await db.execute(
                        select(User.id, User.soul_coin_balance).where(
                            User.id.in_((issuer_id, recipient_id))
                        )
                    )
                ).all()
            )
            positive_ledger = (
                await db.execute(
                    select(Transaction.user_id, Transaction.amount).where(
                        Transaction.user_id.in_((issuer_id, recipient_id)),
                        Transaction.amount > 0,
                    )
                )
            ).all()

        assert hold is not None
        successes = sum(outcome is None for outcome in outcomes)
        assert successes == 1, (
            f"round {round_number}: expected one terminal owner, got {successes}; "
            f"status={hold.status!r}, balances={balances!r}"
        )

        settled = (
            hold.status == "settled"
            and balances == {issuer_id: 0, recipient_id: 1}
            and positive_ledger == [(recipient_id, 1)]
        )
        refunded = (
            hold.status == "refunded"
            and balances == {issuer_id: 1, recipient_id: 0}
            and positive_ledger == [(issuer_id, 1)]
        )
        assert settled ^ refunded, (
            f"round {round_number}: partial or double terminalization; "
            f"status={hold.status!r}, balances={balances!r}, "
            f"positive_ledger={positive_ledger!r}"
        )
