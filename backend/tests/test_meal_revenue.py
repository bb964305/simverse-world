"""M-A C1 — 餐费入账(食客→经营者转账,赊账与缓存语义不回归)。

Plan: `docs/plans/2026-08-09-M-A-npc-economy.md` Step 4;spec §4 C1。

现状 `_charge_meal`(execute/basic.py:35)把 2 SC 直接烧掉——纯 sink,林晚秋/周大河
这两个零工资职业连营业额都没有。这里钉的是:`npc_trade_enabled` 开 → 餐费转给店
主;关 → 与现状逐字节一致。两种口径下"无条件刷食客钱包缓存"和"余额不足走赊账"
都不许回归。

断言**一律新开 session 重读**(理由同 test_coin_transfer):conftest 的 `:memory:`
引擎走 StaticPool,所有 session 共用一条连接、读得到尚未 commit 的改动,转账的事
务边界会假绿,所以本模块自建文件型 sqlite。fixture 故意让 `Resident.id` ≠ `slug`
——钱包按 slug 记账、memory 按 id 记录,两者串了就会露馅。
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agent.map_data import LOCATIONS
from app.agent.phases.execute.basic import _charge_meal
from app.config import settings
from app.database import Base
from app.models.memory import Memory
from app.models.resident import Resident
from app.models.resident_treasury import ResidentTreasury
from app.services import coin_service, relation_service, treasury_service

pytestmark = pytest.mark.anyio

# map_data.LOCATIONS: cafe bounds (53,14,62,26)、tavern bounds (72,13,83,26)。
CAFE_TILE = (57, 20)
TAVERN_TILE = (75, 20)
SQUARE_TILE = (100, 40)  # 非 dining —— location_category 返 None
CANTEEN_TILE = (4, 4)    # 地图西北角空地 —— 任何既有地点的 bounds 都不覆盖


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def sessions(tmp_path):
    """Session factory on a file-backed sqlite — real per-session connections."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'meal.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def trade_on(monkeypatch):
    """C1 闸开(EAT 主闸本就开着,这里一并钉死)。"""
    monkeypatch.setattr(settings, "npc_economy_enabled", True)
    monkeypatch.setattr(settings, "npc_trade_enabled", True)
    return settings


@pytest.fixture
def trade_off(monkeypatch):
    monkeypatch.setattr(settings, "npc_economy_enabled", True)
    monkeypatch.setattr(settings, "npc_trade_enabled", False)
    return settings


@pytest.fixture
def feed_pushes(monkeypatch):
    """记录 feed 推送——push 自开 session 打全局引擎,测试里必须换掉。"""
    from app.services import feed_service

    calls: list[tuple] = []

    async def _fake(resident_slug, kind, payload=None):
        calls.append((resident_slug, kind, payload or {}))

    monkeypatch.setattr(feed_service, "push", _fake)
    return calls


def _res(rid, slug, name, duty=None, *, tile=CAFE_TILE, district="cafe"):
    """id 与 slug 故意不同形(id 是 uuid 形态的主键,钱包按 slug 记账)。"""
    meta = {"duty": {"key": duty, "perks": {}}} if duty else None
    return Resident(id=rid, slug=slug, name=name, district=district, status="idle",
                    resident_type="npc", tile_x=tile[0], tile_y=tile[1], meta_json=meta)


async def _seed(sessions, residents, **balances: int) -> None:
    async with sessions() as db:
        db.add_all(residents)
        for slug, amount in balances.items():
            db.add(ResidentTreasury(resident_slug=slug, balance_sc=amount))
        await db.commit()


async def _balance(sessions, slug: str) -> int:
    """新 session 重读 —— 只承认已落库的钱。"""
    async with sessions() as db:
        return await coin_service.treasury_balance(db, slug)


async def _wallet(sessions, slug: str):
    """新 session 重读 meta_json 里的钱包缓存(prompt 读的那份)。"""
    async with sessions() as db:
        r = (await db.execute(select(Resident).where(Resident.slug == slug))).scalar_one()
        return (r.meta_json or {}).get("wallet")


async def _eat(sessions, slug: str) -> None:
    """在自己的 session 里跑一遍 _charge_meal(食客对象由该 session 加载)。"""
    async with sessions() as db:
        diner = (await db.execute(select(Resident).where(Resident.slug == slug))).scalar_one()
        await _charge_meal(db, diner)


# --------------------------------------------------------------------------- #
# 1-2. 闸开 → 餐费转给经营者                                                    #
# --------------------------------------------------------------------------- #

async def test_meal_pays_the_cafe_host(sessions, trade_on, feed_pushes):
    await _seed(
        sessions,
        [_res("id-lin-0001", "lin", "林晚秋", "cafe_host"),
         _res("id-diner-001", "diner", "食客")],
        diner=10,
    )

    await _eat(sessions, "diner")

    cost = settings.npc_meal_cost_sc
    assert await _balance(sessions, "diner") == 10 - cost
    assert await _balance(sessions, "lin") == cost           # 不再是 sink
    assert await _wallet(sessions, "diner") == 10 - cost
    assert await _wallet(sessions, "lin") == cost            # 店主缓存也刷
    assert [(slug, kind) for slug, kind, _ in feed_pushes] == [("lin", "meal_income")]


async def test_meal_pays_the_tavern_hub(sessions, trade_on, feed_pushes):
    await _seed(
        sessions,
        [_res("id-zhou-0001", "zhou", "周大河", "tavern_hub",
              tile=TAVERN_TILE, district="tavern"),
         _res("id-diner-001", "diner", "食客", tile=TAVERN_TILE, district="tavern")],
        diner=10,
    )

    await _eat(sessions, "diner")

    cost = settings.npc_meal_cost_sc
    assert await _balance(sessions, "diner") == 10 - cost
    assert await _balance(sessions, "zhou") == cost
    assert [(slug, kind) for slug, kind, _ in feed_pushes] == [("zhou", "meal_income")]


# --------------------------------------------------------------------------- #
# 3. 穷人保障:转账失败 → 赊账分支原样保留                                       #
# --------------------------------------------------------------------------- #

async def test_broke_diner_still_eats_on_credit(sessions, trade_on, feed_pushes):
    await _seed(
        sessions,
        [_res("id-lin-0001", "lin", "林晚秋", "cafe_host"),
         _res("id-broke-001", "broke", "穷食客")],
        broke=1,  # < 餐费 → transfer False
    )

    await _eat(sessions, "broke")

    assert await _balance(sessions, "broke") == 1            # 一分没扣
    assert await _balance(sessions, "lin") == 0              # 店主也没进账
    assert await _wallet(sessions, "broke") == 1             # 缓存仍无条件刷
    async with sessions() as db:
        mems = (await db.execute(
            select(Memory).where(Memory.resident_id == "id-broke-001")
        )).scalars().all()
        assert any("赊" in m.content for m in mems)
        pair = await relation_service.get_pair(db, "id-broke-001", "id-lin-0001")
        assert pair is not None and pair.familiarity > 0
    assert feed_pushes == []                                 # 赊账不发营收 feed


# --------------------------------------------------------------------------- #
# 4. 经营者缺失 / 经营者就是食客 → 回退旧 sink                                   #
# --------------------------------------------------------------------------- #

async def test_missing_host_falls_back_to_the_sink_debit(sessions, trade_on, feed_pushes):
    await _seed(sessions, [_res("id-diner-001", "diner", "食客")], diner=10)

    await _eat(sessions, "diner")

    cost = settings.npc_meal_cost_sc
    assert await _balance(sessions, "diner") == 10 - cost
    async with sessions() as db:  # 钱真的蒸发了,没被塞给任何人
        total = sum((await db.execute(select(ResidentTreasury.balance_sc))).scalars().all())
    assert total == 10 - cost
    assert feed_pushes == []


async def test_host_eating_at_her_own_shop_falls_back_to_the_sink(sessions, trade_on, feed_pushes):
    """自己吃自己店 —— transfer 会 raise CoinError,必须提前退回 sink 路径。"""
    await _seed(sessions, [_res("id-lin-0001", "lin", "林晚秋", "cafe_host")], lin=10)

    await _eat(sessions, "lin")

    cost = settings.npc_meal_cost_sc
    assert await _balance(sessions, "lin") == 10 - cost      # 不是 10(fail-open 吞掉)
    assert await _wallet(sessions, "lin") == 10 - cost
    assert feed_pushes == []


# --------------------------------------------------------------------------- #
# 5. 闸关 → 与现状逐字节一致                                                    #
# --------------------------------------------------------------------------- #

async def test_gate_off_keeps_the_sink_debit(sessions, trade_off, feed_pushes):
    await _seed(
        sessions,
        [_res("id-lin-0001", "lin", "林晚秋", "cafe_host"),
         _res("id-diner-001", "diner", "食客")],
        diner=10,
    )

    await _eat(sessions, "diner")

    cost = settings.npc_meal_cost_sc
    assert await _balance(sessions, "diner") == 10 - cost
    assert await _balance(sessions, "lin") == 0              # 店主零收入(现状)
    assert await _wallet(sessions, "diner") == 10 - cost
    assert feed_pushes == []


async def test_gate_off_broke_diner_keeps_the_credit_branch(sessions, trade_off, feed_pushes):
    await _seed(
        sessions,
        [_res("id-lin-0001", "lin", "林晚秋", "cafe_host"),
         _res("id-broke-001", "broke", "穷食客")],
        broke=0,
    )

    await _eat(sessions, "broke")

    assert await _balance(sessions, "broke") == 0
    assert await _wallet(sessions, "broke") == 0
    async with sessions() as db:
        mems = (await db.execute(
            select(Memory).where(Memory.resident_id == "id-broke-001")
        )).scalars().all()
        assert any("赊" in m.content for m in mems)
        pair = await relation_service.get_pair(db, "id-broke-001", "id-lin-0001")
        assert pair is not None and pair.familiarity > 0


# --------------------------------------------------------------------------- #
# 6. 转账中途抛 → 回滚后 fail-open,不留悬挂 debit                               #
# --------------------------------------------------------------------------- #

async def test_transfer_blowup_rolls_back_and_stays_fail_open(sessions, trade_on,
                                                              feed_pushes, monkeypatch):
    await _seed(
        sessions,
        [_res("id-lin-0001", "lin", "林晚秋", "cafe_host"),
         _res("id-diner-001", "diner", "食客")],
        diner=10,
    )

    async def _boom(*a, **kw):
        raise RuntimeError("credit leg exploded")

    monkeypatch.setattr(coin_service, "treasury_credit_pending", _boom)

    async with sessions() as db:
        diner = (await db.execute(select(Resident).where(Resident.slug == "diner"))).scalar_one()
        await _charge_meal(db, diner)   # fail-open:不许把异常抛给 tick
        await db.commit()               # 后续无关 commit 不得把半笔 debit 带落库

    assert await _balance(sessions, "diner") == 10
    assert await _balance(sessions, "lin") == 0
    assert feed_pushes == []


# --------------------------------------------------------------------------- #
# 7. P1-S7 守恒:餐费要么转给店主,要么进镇库 —— 一分都不许蒸发                   #
# --------------------------------------------------------------------------- #

@pytest.fixture
def caps_on(monkeypatch):
    """P1 能力闸 + 镇库闸(镇库是守恒去向的对手方,两道都得开才有 tax 路径)。"""
    monkeypatch.setattr(settings, "location_capabilities_enabled", True)
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    return settings


@pytest.fixture
def canteen():
    """第三个 dining 地点:声明了 dining 却没写 host_duty —— 今天生产里不存在,
    所以旧代码那条「静默错付给 tavern_hub」的分支不可达。"""
    slug = "t_canteen_revenue"
    assert slug not in LOCATIONS, slug
    LOCATIONS[slug] = {"name": "临时食堂", "type": "public",
                       "bounds": (2, 2, 6, 6), "center": CANTEEN_TILE,
                       "entrance": CANTEEN_TILE,
                       "capabilities": {"dining": {}}}
    yield slug
    LOCATIONS.pop(slug, None)


async def _total_sc(sessions) -> int:
    """闭环货币总量 = 全体居民钱包 + 镇库。新 session 重读,只认已落库的钱。"""
    async with sessions() as db:
        wallets = sum(
            (await db.execute(select(ResidentTreasury.balance_sc))).scalars().all())
        return wallets + await treasury_service.balance(db)


async def test_meal_to_the_host_conserves_the_money_supply(sessions, trade_on,
                                                           caps_on, feed_pushes):
    """闸开 + 店主在岗:转账守恒,总量逐分不变(treasury_transfer 有对手方)。"""
    await _seed(
        sessions,
        [_res("id-lin-0001", "lin", "林晚秋", "cafe_host"),
         _res("id-diner-001", "diner", "食客")],
        diner=10,
    )
    before = await _total_sc(sessions)

    await _eat(sessions, "diner")

    cost = settings.npc_meal_cost_sc
    assert await _total_sc(sessions) == before == 10
    assert await _balance(sessions, "diner") == 10 - cost
    assert await _balance(sessions, "lin") == cost
    async with sessions() as db:
        assert await treasury_service.balance(db) == 0   # 没走镇库


async def test_meal_without_host_duty_goes_to_the_town_not_the_void(
        sessions, trade_on, caps_on, canteen, feed_pushes):
    """闸开 + 没写 host_duty:餐费进镇库 —— treasury_debit 是纯销毁,不许走那条。"""
    await _seed(
        sessions,
        [_res("id-diner-001", "diner", "食客", tile=CANTEEN_TILE,
              district="canteen")],
        diner=10,
    )
    before = await _total_sc(sessions)

    await _eat(sessions, "diner")

    cost = settings.npc_meal_cost_sc
    assert await _balance(sessions, "diner") == 10 - cost
    async with sessions() as db:
        assert await treasury_service.balance(db) == cost  # 进了镇库,不是蒸发
    assert await _total_sc(sessions) == before == 10       # 总量逐分守恒
    assert await _wallet(sessions, "diner") == 10 - cost
    assert feed_pushes == []                               # 没有店主就没有营收 feed
