"""M-A 加固 — 库存扣减的守卫语义与超卖复现。

终审已知限制 2:三处扣减(shop_effects / npc_trade_service / caravan_service)
都是 ORM 读-改-写,cron 进程(商队到访 / NPC 夜间消费)与 API 玩家购买撞上同一行
items 时,两边都读到 stock=1、都写回 0 —— 一件货卖了两次,作者也收了两份货款。

断言**一律新开 session 重读**:conftest 的 `:memory:` 引擎走 StaticPool,所有
session 共用一条连接、读得到尚未 commit 的改动,事务边界会假绿,所以本模块自建
文件型 sqlite。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base
from app.models.shop import Item
from app.services import item_stock

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'stock.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def guard_on(monkeypatch):
    monkeypatch.setattr(settings, "item_stock_guard_enabled", True)
    return settings


@pytest.fixture
def guard_off(monkeypatch):
    monkeypatch.setattr(settings, "item_stock_guard_enabled", False)
    return settings


def _work(code="work_a", stock=3, active=True, price=15):
    return Item(code=code, kind="resident_work", name="陶罐", description="",
                price_sc=price, payload_json={"creator_slug": "maker", "stock": stock},
                stock=stock, active=active)


async def _seed(sessions, *items):
    async with sessions() as db:
        db.add_all(list(items))
        await db.commit()


async def _row(sessions, code="work_a") -> Item:
    async with sessions() as db:
        return (await db.execute(select(Item).where(Item.code == code))).scalar_one()


async def _load(db, code="work_a") -> Item:
    return (await db.execute(select(Item).where(Item.code == code))).scalar_one()


# --------------------------------------------------------------------------- #
# 1. 闸开:守卫语义                                                              #
# --------------------------------------------------------------------------- #

async def test_guard_on_decrements_the_column_and_mirrors_the_payload(
    sessions, guard_on,
):
    await _seed(sessions, _work(stock=3))
    async with sessions() as db:
        assert await item_stock.take_stock(db, await _load(db), 1) == 2
        await db.commit()

    row = await _row(sessions)
    assert row.stock == 2
    assert row.active is True
    assert row.payload_json["stock"] == 2       # 镜像同步(闸翻回去不丢账)


async def test_guard_on_deactivates_at_zero(sessions, guard_on):
    await _seed(sessions, _work(stock=1))
    async with sessions() as db:
        assert await item_stock.take_stock(db, await _load(db), 1) == 0
        await db.commit()

    row = await _row(sessions)
    assert row.stock == 0
    assert row.active is False


async def test_guard_on_returns_none_when_sold_out(sessions, guard_on):
    await _seed(sessions, _work(stock=1))
    async with sessions() as db:
        item = await _load(db)
        assert await item_stock.take_stock(db, item, 1) == 0
        assert await item_stock.take_stock(db, item, 1) is None   # 第二次抢不到
        await db.commit()

    assert (await _row(sessions)).stock == 0    # 绝不写成 -1


async def test_guard_on_returns_none_when_qty_exceeds_stock(sessions, guard_on):
    await _seed(sessions, _work(stock=2))
    async with sessions() as db:
        assert await item_stock.take_stock(db, await _load(db), 3) is None
        await db.commit()

    assert (await _row(sessions)).stock == 2    # 一件都不许扣


async def test_guard_on_takes_the_whole_qty_at_once(sessions, guard_on):
    await _seed(sessions, _work(stock=3))
    async with sessions() as db:
        assert await item_stock.take_stock(db, await _load(db), 3) == 0
        await db.commit()

    row = await _row(sessions)
    assert row.stock == 0
    assert row.active is False


async def test_guard_on_returns_none_for_inactive_item(sessions, guard_on):
    await _seed(sessions, _work(stock=3, active=False))
    async with sessions() as db:
        assert await item_stock.take_stock(db, await _load(db), 1) is None

    assert (await _row(sessions)).stock == 3


async def test_guard_on_self_heals_a_null_column_from_the_payload(sessions, guard_on):
    """暗上窗口里由旧镜像挂上架的行:列还是 NULL,第一次扣减先从 payload 自愈。"""
    item = _work(stock=3)
    item.stock = None
    await _seed(sessions, item)
    async with sessions() as db:
        assert await item_stock.take_stock(db, await _load(db), 1) == 2
        await db.commit()

    assert (await _row(sessions)).stock == 2


async def test_guard_on_is_flush_owned_and_rollback_undoes_it(sessions, guard_on):
    """不 commit:调用方拥有事务,rollback 之后库存必须原样。"""
    await _seed(sessions, _work(stock=3))
    async with sessions() as db:
        assert await item_stock.take_stock(db, await _load(db), 1) == 2
        await db.rollback()

    assert (await _row(sessions)).stock == 3


# --------------------------------------------------------------------------- #
# 2. 闸关:逐字节旧路径                                                          #
# --------------------------------------------------------------------------- #

async def test_guard_off_is_byte_for_byte_the_legacy_payload_path(sessions, guard_off):
    """闸关 = 旧行为:只改 payload、永不返回 None(旧路径没有"抢不到"这回事)。"""
    await _seed(sessions, _work(stock=1))
    async with sessions() as db:
        item = await _load(db)
        assert await item_stock.take_stock(db, item, 1) == 0
        assert await item_stock.take_stock(db, item, 1) == 0   # 旧路径照扣不误
        await db.commit()

    row = await _row(sessions)
    assert row.payload_json["stock"] == 0
    assert row.active is False


async def test_guard_off_never_touches_the_new_column(sessions, guard_off):
    """暗上判据:闸关时 items.stock 一个字节都不动。"""
    await _seed(sessions, _work(stock=3))
    async with sessions() as db:
        assert await item_stock.take_stock(db, await _load(db), 1) == 2
        await db.commit()

    assert (await _row(sessions)).stock == 3    # 列没被碰过


# --------------------------------------------------------------------------- #
# 3. 超卖复现:双 session                                                        #
# --------------------------------------------------------------------------- #

async def test_two_sessions_cannot_oversell_the_last_copy(sessions, guard_on):
    """两条连接各自读到 stock=1(cron 与玩家的真实时序),只有一条能扣成。

    玩家路径先 SELECT 出 item、扣款、commit,再把**那个已经读到手的对象**交给
    effect 扣库存(shop_service.py:107)——中间隔着一次 commit 的时间,足够 cron
    的商队/夜间消费把最后一件买走。
    """
    await _seed(sessions, _work(stock=1))
    async with sessions() as db1, sessions() as db2:
        item1 = await _load(db1)
        item2 = await _load(db2)          # 两边都读到 stock=1

        got1 = await item_stock.take_stock(db1, item1, 1)
        await db1.commit()
        got2 = await item_stock.take_stock(db2, item2, 1)
        await db2.commit()

    assert (got1, got2) == (0, None)      # 只卖出一件
    row = await _row(sessions)
    assert row.stock == 0
    assert row.active is False


async def test_two_sessions_oversell_when_the_guard_is_off(sessions, guard_off):
    """同一个交错,闸关时就是超卖 —— 这条钉住"旧行为确实有这个洞",
    也钉住闸关分支没被顺手改掉(它必须仍然是旧的)。"""
    await _seed(sessions, _work(stock=1))
    async with sessions() as db1, sessions() as db2:
        item1 = await _load(db1)
        item2 = await _load(db2)
        got1 = await item_stock.take_stock(db1, item1, 1)
        await db1.commit()
        got2 = await item_stock.take_stock(db2, item2, 1)
        await db2.commit()

    assert (got1, got2) == (0, 0)         # 两边都"卖成了":一件货卖了两次
