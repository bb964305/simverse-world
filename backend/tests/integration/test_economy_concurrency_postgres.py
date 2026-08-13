"""真 PostgreSQL 上的经济并发实证(M-A 加固,opt-in)。

**为什么这些断言不能待在默认门里。** 文件型 sqlite 做不出这两个竞态:它是库
级写锁,两条连接的写不可能同时在飞,交错只能退化成"写1提交 / 读2 / 写2"——那种
顺序连旧的读-改-写都算得对。所以竞态的最终证据放在这里:真 PG、真
``asyncio.gather``、真行锁。默认门里留的是**结构判据**(累加只发一条语句、守卫
零行返回 None),见 tests/test_tax_carry.py 与 tests/test_item_stock_guard.py。

跑法(需要一个可丢弃的 PG):

    docker run -d --rm --name simverse-econ-pg -e POSTGRES_PASSWORD=pg \\
      -p 55432:5432 postgres:16
    ECONOMY_TEST_DATABASE_URL=postgresql+asyncpg://postgres:pg@localhost:55432/postgres \\
      .venv/bin/python -m pytest tests/integration/test_economy_concurrency_postgres.py \\
      -m economy_postgres -v

只建这几张用得上的表(不是 ``create_all``):全量建表会拖进 pgvector 依赖,而这
几条断言一张 embedding 表都不需要。
"""
from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.resident_treasury import ResidentTreasury
from app.models.shop import Item
from app.models.system_config import SystemConfig
from app.models.town_treasury import TownTreasury
from app.models.town_treasury_entry import TownTreasuryEntry
from app.services import treasury_service

pytestmark = [pytest.mark.economy_postgres, pytest.mark.anyio]

DB_URL = (os.environ.get("ECONOMY_TEST_DATABASE_URL")
          or os.environ.get("LAB_TEST_DATABASE_URL") or "")
TABLES = (SystemConfig.__table__, TownTreasury.__table__,
          TownTreasuryEntry.__table__, Item.__table__, ResidentTreasury.__table__)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def pg_sessions():
    if not DB_URL.startswith("postgresql+asyncpg"):
        pytest.skip("ECONOMY_TEST_DATABASE_URL must be postgresql+asyncpg://...")
    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        for table in reversed(TABLES):
            await conn.run_sync(table.drop, checkfirst=True)
        for table in TABLES:
            await conn.run_sync(table.create)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        for table in reversed(TABLES):
            await conn.run_sync(table.drop, checkfirst=True)
    await engine.dispose()


async def test_concurrent_skims_never_lose_a_millisecond_of_carry(
    pg_sessions, monkeypatch,
):
    """20 笔并发 skim(每笔 exact = 0.8 SC = 800 milli)。

    守恒式:征进镇库的 SC × 1000 + 账上剩余 milli == 累计应征 milli。
    旧的浮点读-改-写在这里会丢尾数(多个事务读到同一个 carry,后提交的抹掉先
    提交的),等式左边小于右边。
    """
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "tax_carry_enabled", True)
    monkeypatch.setattr(settings, "polis_policy_enabled", False)
    rounds, gross, rate = 20, 16, 0.05          # 16 × 0.05 = 0.8 SC = 800 milli

    async def one(i: int) -> int:
        async with pg_sessions() as db:
            return await treasury_service.skim_tax(db, gross, rate, f"race:{i}")

    cuts = await asyncio.gather(*(one(i) for i in range(rounds)))

    async with pg_sessions() as db:
        town = await treasury_service.balance(db)
        carry = await treasury_service.kv_read_int(db, treasury_service.TAX_CARRY_KEY)

    expected = rounds * int(round(gross * rate * treasury_service.CARRY_SCALE))
    assert sum(cuts) == town, "返回的 cut 之和必须等于镇库实收"
    assert town * treasury_service.CARRY_SCALE + carry == expected, (
        f"尾数丢了:入库 {town} SC + 账上 {carry} milli ≠ 应征 {expected} milli")
    assert 0 <= carry < treasury_service.CARRY_SCALE


async def test_concurrent_buyers_cannot_oversell_a_single_copy(
    pg_sessions, monkeypatch,
):
    """8 个并发买家抢同一件 stock=1 的作品:恰好一个抢到,其余全 None。

    真行锁的实证:PG 把 8 条 ``UPDATE ... WHERE stock >= 1`` 串起来,第一条之后
    的 7 条守卫都不再匹配。闸关(旧 payload 读-改-写)时 8 条会全部"成交"。
    """
    from app.services import item_stock

    monkeypatch.setattr(settings, "item_stock_guard_enabled", True)
    async with pg_sessions() as db:
        db.add(Item(code="work_a", kind="resident_work", name="陶罐",
                    description="", price_sc=15,
                    payload_json={"creator_slug": "maker", "stock": 1},
                    stock=1, active=True))
        await db.commit()

    async def buy() -> int | None:
        async with pg_sessions() as db:
            item = (await db.execute(
                select(Item).where(Item.code == "work_a"))).scalar_one()
            got = await item_stock.take_stock(db, item, 1)
            await db.commit()
            return got

    results = await asyncio.gather(*(buy() for _ in range(8)))

    assert sum(1 for r in results if r is not None) == 1, (
        f"一件货只能卖一次,实测成交 {[r for r in results if r is not None]}")
    async with pg_sessions() as db:
        row = (await db.execute(
            select(Item.stock, Item.active).where(Item.code == "work_a"))).one()
    assert row.stock == 0
    assert row.active is False


async def test_concurrent_public_wages_obey_the_income_budget(pg_sessions):
    """The town-row lock serialises budget reads: 10 simultaneous 1-SC wages
    against 10 SC income at 70% may land exactly seven, never eight-plus."""
    async with pg_sessions() as db:
        await treasury_service.tax(db, 10, reason="sales_tax:seed")

    async def pay(i: int) -> bool:
        async with pg_sessions() as db:
            return await treasury_service.town_to_resident(
                db,
                f"worker-{i}",
                1,
                reason=f"wage:worker-{i}",
                ref_key=f"wage:concurrent:{i}",
                wage_budget_ratio=0.70,
            )

    results = await asyncio.gather(*(pay(i) for i in range(10)))
    assert sum(results) == 7
    async with pg_sessions() as db:
        assert await treasury_service.balance(db) == 3
        assert await treasury_service.wage_window_totals(db) == (10, 7)


async def test_rejected_wage_releases_town_row_lock(pg_sessions):
    """A SELECT-FOR-UPDATE no-op must end its transaction before returning.

    If the insufficient-funds branch merely returns, the second session blocks
    on the town row until this test times out.
    """
    async with pg_sessions() as db:
        await treasury_service.tax(db, 1, reason="sales_tax:seed")

    async with pg_sessions() as rejecting:
        assert not await treasury_service.town_to_resident(
            rejecting, "worker", 2, reason="wage:worker",
        )
        assert rejecting.in_transaction() is False

        async def credit_from_other_session() -> None:
            async with pg_sessions() as other:
                await treasury_service.tax(other, 1, reason="sales_tax:next")

        await asyncio.wait_for(credit_from_other_session(), timeout=2.0)
