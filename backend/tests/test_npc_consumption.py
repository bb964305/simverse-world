"""M-A C2 — NPC 夜间消费 pass(零 LLM 规则购买,单事务成交 + 孤儿作品下架)。

Plan: `docs/plans/2026-08-09-M-A-npc-economy.md` Step 5;spec §4 C2。

需求端现状全外包给玩家:玩家 07-31 断流后作品市场归零。这里钉的是"居民自己会
买东西"——规则引擎选货(好感 + 稳定口味哈希,零 LLM),成交走**单事务**(debit +
skim + credit + 库存),memory/feed 一律在 commit 之后 fail-open。

断言**一律新开 session 重读**(理由同 test_coin_transfer / test_meal_revenue):
conftest 的 `:memory:` 引擎走 StaticPool,所有 session 共用一条连接、读得到尚未
commit 的改动,单事务的边界会假绿,所以本模块自建文件型 sqlite。fixture 故意让
`Resident.id` ≠ `slug`——钱包按 slug 记账、memory/关系按 id 记录,串了就露馅。
"""
import random

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base
from app.models.memory import Memory
from app.models.resident import Resident
from app.models.resident_treasury import ResidentTreasury
from app.models.shop import Item
from app.models.system_config import SystemConfig
from app.services import coin_service, npc_trade_service, relation_service, treasury_service

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def sessions(tmp_path):
    """Session factory on a file-backed sqlite — real per-session connections."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'consume.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def trade_on(monkeypatch):
    """C2 闸开。掷骰概率拍成 1.0 —— 本模块测的是选货与事务,不是骰子。"""
    monkeypatch.setattr(settings, "npc_economy_enabled", True)
    monkeypatch.setattr(settings, "npc_trade_enabled", True)
    monkeypatch.setattr(settings, "npc_trade_buy_prob", 1.0)
    monkeypatch.setattr(settings, "npc_trade_reserve_sc", 5)
    monkeypatch.setattr(settings, "npc_trade_max_buys_per_night", 2)
    monkeypatch.setattr(settings, "town_treasury_enabled", False)
    monkeypatch.setattr(settings, "polis_policy_enabled", False)
    return settings


@pytest.fixture
def tax_on(monkeypatch):
    """镇库 + 分数税账两闸开(销售税率默认 0.1)。"""
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "tax_carry_enabled", True)
    monkeypatch.setattr(settings, "town_tax_rate_sales", 0.1)
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


def _res(rid, slug, name):
    """id 与 slug 故意不同形(id 是 uuid 形态的主键,钱包按 slug 记账)。"""
    return Resident(id=rid, slug=slug, name=name, district="free", status="idle",
                    resident_type="npc")


def _work(code, creator_slug, name, price=15, stock=3):
    return Item(code=code, kind="resident_work", name=name, description="",
                price_sc=price, payload_json={"creator_slug": creator_slug, "stock": stock},
                active=True)


def _import(code, name, price=6, stock=2):
    return Item(code=code, kind="import_good", name=name, description="",
                price_sc=price, payload_json={"caravan": True, "stock": stock}, active=True)


async def _seed(sessions, residents=(), items=(), **balances: int) -> None:
    async with sessions() as db:
        db.add_all(list(residents) + list(items))
        for slug, amount in balances.items():
            db.add(ResidentTreasury(resident_slug=slug, balance_sc=amount))
        await db.commit()


async def _run(sessions, seed: int = 42) -> dict:
    async with sessions() as db:
        return await npc_trade_service.run_consumption_pass(db, rng=random.Random(seed))


async def _balance(sessions, slug: str) -> int:
    """新 session 重读 —— 只承认已落库的钱。"""
    async with sessions() as db:
        return await coin_service.treasury_balance(db, slug)


async def _wallet(sessions, slug: str):
    """新 session 重读 meta_json 里的钱包缓存(prompt 读的那份)。"""
    async with sessions() as db:
        r = (await db.execute(select(Resident).where(Resident.slug == slug))).scalar_one()
        return (r.meta_json or {}).get("wallet")


async def _item(sessions, code: str) -> Item:
    async with sessions() as db:
        return (await db.execute(select(Item).where(Item.code == code))).scalar_one()


async def _memories(sessions, resident_id: str) -> list[str]:
    async with sessions() as db:
        rows = (await db.execute(
            select(Memory.content).where(Memory.resident_id == resident_id)
        )).scalars().all()
    return list(rows)


async def _town(sessions) -> int:
    async with sessions() as db:
        return await treasury_service.balance(db)


async def _carry(sessions):
    async with sessions() as db:
        return (await db.execute(select(SystemConfig).where(
            SystemConfig.key == treasury_service.TAX_CARRY_KEY))).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# 1. 闸关(任一)→ no-op 零写入                                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("off_key", ["npc_trade_enabled", "npc_economy_enabled"])
async def test_gate_off_is_a_noop_with_zero_writes(sessions, trade_on, feed_pushes,
                                                   monkeypatch, off_key):
    monkeypatch.setattr(settings, off_key, False)
    await _seed(
        sessions,
        [_res("id-buyer-001", "buyer", "买主"), _res("id-maker-001", "maker", "作者")],
        [_work("work_a", "maker", "陶罐"), _work("work_ghost", "ghost", "无主之作")],
        buyer=30,
    )

    assert await _run(sessions) == {"bought": 0, "spent": 0, "tax": 0}

    assert await _balance(sessions, "buyer") == 30
    assert await _balance(sessions, "maker") == 0
    assert (await _item(sessions, "work_a")).payload_json["stock"] == 3
    assert (await _item(sessions, "work_ghost")).active is True   # 孤儿也不动
    assert await _memories(sessions, "id-buyer-001") == []
    assert feed_pushes == []


# --------------------------------------------------------------------------- #
# 2. 正常成交:钱、税、库存、缓存、memory/feed 一次到位                           #
# --------------------------------------------------------------------------- #

async def test_trade_moves_money_taxes_stock_caches_and_narrates(sessions, trade_on,
                                                                 tax_on, feed_pushes):
    """两个候选卖家 —— 正好感的那个被选中(好感 0.9 压过 [0,0.5) 的口味哈希)。"""
    await _seed(
        sessions,
        [_res("id-buyer-001", "buyer", "买主"),
         _res("id-liked-001", "liked", "旧friend"),
         _res("id-other-001", "other", "生人")],
        [_work("work_liked", "liked", "陶罐"), _work("work_other", "other", "木碗")],
        buyer=30,
    )
    async with sessions() as db:
        await relation_service.bump(db, "id-buyer-001", "id-liked-001", d_affinity=0.9)

    summary = await _run(sessions)

    # 15 × 0.1 = 1.5 → 税 1 入镇库、尾数 0.5 记在 carry 账上。
    assert summary == {"bought": 1, "spent": 15, "tax": 1}
    assert await _balance(sessions, "buyer") == 15
    assert await _balance(sessions, "liked") == 14
    assert await _balance(sessions, "other") == 0            # 好感低的没卖出去
    assert await _town(sessions) == 1
    assert float((await _carry(sessions)).value) == pytest.approx(0.5)

    liked_item = await _item(sessions, "work_liked")
    assert liked_item.payload_json["stock"] == 2             # 重赋值模式生效
    assert liked_item.active is True
    assert (await _item(sessions, "work_other")).payload_json["stock"] == 3

    assert await _wallet(sessions, "buyer") == 15            # 双方缓存都刷
    assert await _wallet(sessions, "liked") == 14

    assert any("陶罐" in m for m in await _memories(sessions, "id-buyer-001"))
    assert any("被人买走了" in m for m in await _memories(sessions, "id-liked-001"))
    assert sorted((slug, kind) for slug, kind, _ in feed_pushes) == [
        ("buyer", "npc_purchase"), ("liked", "npc_purchase")]


# --------------------------------------------------------------------------- #
# 3. 保留金地板                                                                 #
# --------------------------------------------------------------------------- #

async def test_reserve_floor_keeps_the_poor_from_buying(sessions, trade_on, feed_pushes):
    """18 < 15 + 5 —— 保留金兼作贫困线,买不起就不买(不是赊账)。"""
    await _seed(
        sessions,
        [_res("id-buyer-001", "buyer", "买主"), _res("id-maker-001", "maker", "作者")],
        [_work("work_a", "maker", "陶罐")],
        buyer=18,
    )

    assert (await _run(sessions))["bought"] == 0
    assert await _balance(sessions, "buyer") == 18
    assert (await _item(sessions, "work_a")).payload_json["stock"] == 3
    assert feed_pushes == []


# --------------------------------------------------------------------------- #
# 4. 全镇每晚上限 / 每人一笔                                                     #
# --------------------------------------------------------------------------- #

async def test_town_wide_cap_stops_the_third_buyer(sessions, trade_on, feed_pushes):
    await _seed(
        sessions,
        [_res("id-b1", "b1", "买主甲"), _res("id-b2", "b2", "买主乙"),
         _res("id-b3", "b3", "买主丙"), _res("id-maker-001", "maker", "作者")],
        [_work("work_a", "maker", "陶罐")],
        b1=30, b2=30, b3=30,
    )

    assert (await _run(sessions))["bought"] == 2             # cap = 2
    spent = [30 - await _balance(sessions, s) for s in ("b1", "b2", "b3")]
    assert sorted(spent) == [0, 15, 15]
    assert (await _item(sessions, "work_a")).payload_json["stock"] == 1


async def test_one_buy_per_buyer_per_night(sessions, trade_on, feed_pushes, monkeypatch):
    monkeypatch.setattr(settings, "npc_trade_max_buys_per_night", 5)
    await _seed(
        sessions,
        [_res("id-buyer-001", "buyer", "买主"), _res("id-maker-001", "maker", "作者")],
        [_work("work_a", "maker", "陶罐"), _work("work_b", "maker", "木碗")],
        buyer=100,
    )

    assert (await _run(sessions))["bought"] == 1
    assert await _balance(sessions, "buyer") == 85


async def test_zero_probability_buys_nothing(sessions, trade_on, feed_pushes, monkeypatch):
    """掷骰是 rng 注入的 —— 概率 0 时一笔都不许成交。"""
    monkeypatch.setattr(settings, "npc_trade_buy_prob", 0.0)
    await _seed(
        sessions,
        [_res("id-buyer-001", "buyer", "买主"), _res("id-maker-001", "maker", "作者")],
        [_work("work_a", "maker", "陶罐")],
        buyer=30,
    )

    assert (await _run(sessions))["bought"] == 0
    assert await _balance(sessions, "buyer") == 30


# --------------------------------------------------------------------------- #
# 5. 不买自己的作品                                                             #
# --------------------------------------------------------------------------- #

async def test_never_buys_own_work(sessions, trade_on, feed_pushes):
    await _seed(
        sessions,
        [_res("id-maker-001", "maker", "作者")],
        [_work("work_a", "maker", "陶罐")],
        maker=100,
    )

    assert (await _run(sessions))["bought"] == 0
    assert await _balance(sessions, "maker") == 100
    assert (await _item(sessions, "work_a")).payload_json["stock"] == 3


# --------------------------------------------------------------------------- #
# 6. 最后一件售罄 → 下架                                                        #
# --------------------------------------------------------------------------- #

async def test_last_copy_sells_out_and_delists(sessions, trade_on, feed_pushes):
    await _seed(
        sessions,
        [_res("id-buyer-001", "buyer", "买主"), _res("id-maker-001", "maker", "作者")],
        [_work("work_a", "maker", "陶罐", stock=1)],
        buyer=30,
    )

    assert (await _run(sessions))["bought"] == 1
    item = await _item(sessions, "work_a")
    assert item.payload_json["stock"] == 0
    assert item.active is False


# --------------------------------------------------------------------------- #
# 7. 进口货是 sink;本地作品优先                                                 #
# --------------------------------------------------------------------------- #

async def test_import_good_is_a_sink_without_tax(sessions, trade_on, tax_on, feed_pushes):
    await _seed(
        sessions,
        [_res("id-buyer-001", "buyer", "买主")],
        [_import("import_tea", "茶叶")],
        buyer=30,
    )

    assert await _run(sessions) == {"bought": 1, "spent": 6, "tax": 0}
    assert await _balance(sessions, "buyer") == 24
    async with sessions() as db:   # 钱出镇即消失,没进任何人的口袋
        total = sum((await db.execute(select(ResidentTreasury.balance_sc))).scalars().all())
    assert total == 24
    assert await _town(sessions) == 0                        # 进口不抽税
    assert await _carry(sessions) is None
    assert (await _item(sessions, "import_tea")).payload_json["stock"] == 1
    assert await _wallet(sessions, "buyer") == 24
    assert any("商队" in m for m in await _memories(sessions, "id-buyer-001"))
    assert [(slug, kind) for slug, kind, _ in feed_pushes] == [("buyer", "npc_purchase")]


async def test_resident_work_outranks_the_import_good(sessions, trade_on, feed_pushes,
                                                      monkeypatch):
    """cap 拍成 1:这里只看"买主这一笔挑了谁",不让刚收款的作者再接着买一笔。"""
    monkeypatch.setattr(settings, "npc_trade_max_buys_per_night", 1)
    await _seed(
        sessions,
        [_res("id-buyer-001", "buyer", "买主"), _res("id-maker-001", "maker", "作者")],
        [_work("work_a", "maker", "陶罐"), _import("import_tea", "茶叶")],
        buyer=30,
    )

    assert (await _run(sessions))["spent"] == 15
    assert (await _item(sessions, "work_a")).payload_json["stock"] == 2
    assert (await _item(sessions, "import_tea")).payload_json["stock"] == 2


# --------------------------------------------------------------------------- #
# 8. 作者已被清号 → 孤儿作品顺手下架                                             #
# --------------------------------------------------------------------------- #

async def test_orphan_work_is_delisted_and_skipped(sessions, trade_on, feed_pushes):
    """item 生命周期与居民解耦(vm212 有存量孤儿):买了也没人收钱,直接下架。"""
    await _seed(
        sessions,
        [_res("id-buyer-001", "buyer", "买主")],
        [_work("work_ghost", "ghost", "无主之作")],
        buyer=30,
    )

    assert (await _run(sessions))["bought"] == 0
    assert await _balance(sessions, "buyer") == 30
    assert (await _item(sessions, "work_ghost")).active is False
    assert feed_pushes == []


# --------------------------------------------------------------------------- #
# 9. 单笔炸掉 → rollback 后继续,已成交的不受污染                                 #
# --------------------------------------------------------------------------- #

async def test_failed_trade_rolls_back_and_the_pass_carries_on(sessions, trade_on,
                                                               feed_pushes, monkeypatch):
    await _seed(
        sessions,
        [_res("id-b1", "b1", "买主甲"), _res("id-b2", "b2", "买主乙"),
         _res("id-maker-001", "maker", "作者")],
        [_work("work_a", "maker", "陶罐")],
        b1=30, b2=30,
    )

    real = coin_service.treasury_credit_pending
    calls = {"n": 0}

    async def _flaky(db, slug, amount, reason=""):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("credit leg exploded")
        return await real(db, slug, amount, reason)

    monkeypatch.setattr(coin_service, "treasury_credit_pending", _flaky)

    assert await _run(sessions) == {"bought": 1, "spent": 15, "tax": 0}
    assert await _balance(sessions, "b1") == 30              # 悬挂的 debit 没落库
    assert await _balance(sessions, "b2") == 15
    assert await _balance(sessions, "maker") == 15
    assert (await _item(sessions, "work_a")).payload_json["stock"] == 2   # 只扣一次
    assert await _memories(sessions, "id-b1") == []
    assert sorted((slug, kind) for slug, kind, _ in feed_pushes) == [
        ("b2", "npc_purchase"), ("maker", "npc_purchase")]
