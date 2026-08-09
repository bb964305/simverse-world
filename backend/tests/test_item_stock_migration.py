"""迁移 056:items.stock 列化(暗上边界)。

暗上的判据不是"没写数据",而是"**没有读者**":056 加列 + 从 payload_json 回填
初值,而三处扣减在 ITEM_STOCK_GUARD_ENABLED 翻开之前一行都不读这列。回填是新
列的初值(从同一行推导),不是数据修复,也不删 payload 里的 stock —— 旧镜像回滚
后照样按 payload 跑。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.shop import Item

pytestmark = pytest.mark.anyio

MIGRATION = (Path(__file__).resolve().parent.parent / "alembic" / "versions"
             / "056_add_item_stock.py")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_056", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)          # type: ignore[union-attr]
    return module


async def _fresh(tmp_path, name: str):
    """建库 + 把 stock 清成 NULL —— 那才是迁移跑之前的形态。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, class_=AsyncSession,
                                      expire_on_commit=False)


def test_migration_chains_onto_the_measured_head():
    """单头,且挂在本 worktree 实测的链头 055 上。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    script = ScriptDirectory.from_config(Config(str(ini)))
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic multi-head: {heads}"
    rev = script.get_revision("056_add_item_stock")
    assert rev is not None
    assert rev.down_revision == "055_add_commission_acceptor"
    # alembic 自建的 alembic_version.version_num 是 varchar(32)——超长的 revision
    # id 会在写版本号那一句上炸(055 那次已实测复现)。
    assert len("056_add_item_stock") <= 32


def test_item_model_has_a_nullable_stock_column():
    """nullable 是故意的:consumable/gift/decor/tip 根本没有库存概念,
    NULL = 不计库存;只有 resident_work / import_good 有值。"""
    col = Item.__table__.columns["stock"]
    assert isinstance(col.type, sa.Integer)
    assert col.nullable is True


async def test_backfill_copies_payload_stock_into_the_column(tmp_path):
    """回填:payload 里有 stock 的行搬进新列,没有的行保持 NULL。"""
    module = _load_migration()
    engine, sessions = await _fresh(tmp_path, "mig.db")

    async with sessions() as db:
        db.add_all([
            Item(code="work_a", kind="resident_work", name="陶罐", price_sc=15,
                 payload_json={"creator_slug": "maker", "stock": 3}, active=True),
            Item(code="import_tea", kind="import_good", name="茶叶", price_sc=6,
                 payload_json={"caravan": True, "stock": 2}, active=True),
            Item(code="gift_flower", kind="gift", name="一束花", price_sc=15,
                 payload_json={"relationship_boost": 0.1}, active=True),
            Item(code="junk", kind="resident_work", name="脏值", price_sc=1,
                 payload_json={"stock": "three"}, active=True),
        ])
        await db.commit()
        await db.execute(sa.text("UPDATE items SET stock = NULL"))
        await db.commit()

    async with engine.begin() as conn:
        filled = await conn.run_sync(lambda c: module._backfill_stock(c))
    assert filled == 2

    async with sessions() as db:
        got = dict((await db.execute(sa.select(Item.code, Item.stock))).all())
    assert got == {"work_a": 3, "import_tea": 2, "gift_flower": None, "junk": None}
    await engine.dispose()


async def test_backfill_leaves_the_payload_mirror_alone(tmp_path):
    """回填不许动 payload —— 回滚回旧镜像后它还得按 payload 跑。"""
    module = _load_migration()
    engine, sessions = await _fresh(tmp_path, "mig_mirror.db")
    async with sessions() as db:
        db.add(Item(code="work_a", kind="resident_work", name="陶罐", price_sc=15,
                    payload_json={"creator_slug": "maker", "stock": 3}, active=True))
        await db.commit()
        await db.execute(sa.text("UPDATE items SET stock = NULL"))
        await db.commit()

    async with engine.begin() as conn:
        await conn.run_sync(lambda c: module._backfill_stock(c))

    async with sessions() as db:
        row = (await db.execute(
            sa.select(Item).where(Item.code == "work_a"))).scalar_one()
    assert row.payload_json == {"creator_slug": "maker", "stock": 3}
    await engine.dispose()


async def test_backfill_is_idempotent(tmp_path):
    """回填跑两遍结果一样(部署重跑 / 回滚再上都不该越滚越怪)。"""
    module = _load_migration()
    engine, sessions = await _fresh(tmp_path, "mig2.db")
    async with sessions() as db:
        db.add(Item(code="work_a", kind="resident_work", name="陶罐", price_sc=15,
                    payload_json={"creator_slug": "maker", "stock": 3}, active=True))
        await db.commit()
        await db.execute(sa.text("UPDATE items SET stock = NULL"))
        await db.commit()

    async with engine.begin() as conn:
        await conn.run_sync(lambda c: module._backfill_stock(c))
        await conn.run_sync(lambda c: module._backfill_stock(c))

    async with sessions() as db:
        assert (await db.execute(
            sa.select(Item.stock).where(Item.code == "work_a"))).scalar_one() == 3
    await engine.dispose()
