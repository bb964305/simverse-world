"""M-A 收口 — 货币守恒契约 + 三闸关双轨口径 + nightly 全链集成。

Plan: `docs/plans/2026-08-09-M-A-npc-economy.md` Step 11;spec §6「货币守恒口径」
与 §7「验收」1-2。

前十步各自钉住了单个组件的事务边界,这一步钉的是**整体**:一晚跑完全链之后,镇
上的钱到底是多了还是少了,以及少的那部分是不是恰好等于设计上要它少的那部分。

三条契约:

1. **守恒**:`Δ(Σ居民余额 + 镇库) == 商队收购注入 + 摊位费 − 进口货 sink`(全整
   数,精确断言)。餐费/NPC 买作品/委托赏金全是内部转移,一分不增不减;唯二的外
   生面是商队(注入)与进口货(sink)。`town_tax_carry_milli` 是**递延税记账不是
   钱**(整数 milli-SC),不进货币总量,单独断言 `0 ≤ carry < 1000` 且等于逐笔
   `exact − cut` 的累计。
2. **关闸口径分双轨**(spec §7-2):三新闸全关 → 三 pass + caravan 钩子 + carry
   **零 DB 写入**(整库快照逐表比对);`_charge_meal` 是例外——它现状本就写库,
   口径是"与现状基线一致"而不是零写入,所以单独断言、不进快照。再补一条 vm212
   在产前提:`town_treasury_enabled=True` 且三新闸关时,玩家 gift/tip/
   resident_work 三条税路径逐字节不变(旧 `int()` 截断,不碰 carry 行)。
3. **接线活着**:gate 开跑一轮真 `run_nightly_jobs`,不炸且 #23 摘要出现在日志里。

断言**一律新开 session 重读**(理由同 test_caravan / test_npc_consumption):
conftest 的 `:memory:` 引擎走 StaticPool,所有 session 共用一条连接、读得到尚未
commit 的改动,守恒这种跨事务的总量断言会假绿,所以本模块自建文件型 sqlite
(nightly 全链那一节除外——它要的是整条链共用一个 session 工厂)。fixture 故意让
`Resident.id` ≠ `slug`:钱包按 slug 记账、委托与 memory 按 id 记录,串了就露馅。
"""
import asyncio
import random
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base
from app.models.commission import Commission
from app.models.resident import Resident
from app.models.resident_treasury import ResidentTreasury
from app.models.shop import Item
from app.models.system_config import SystemConfig
from app.models.town_treasury import TownTreasury
from app.services import caravan_service, coin_service, npc_trade_service, treasury_service

pytestmark = pytest.mark.anyio

# map_data.LOCATIONS: cafe bounds (53,14,62,26) —— 食客站在咖啡馆里才触发餐费转账。
CAFE_TILE = (57, 20)

# `flip_active_events` 交出来的集市日事件(world_event_service.py:105)。
MARKET = {"id": "evt-market-001", "title": "集市日",
          "payload_json": {"market_day": True}}

WORK_PRICE = 15
TEA_PRICE = 6
REWARD = 8


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def sessions(tmp_path):
    """Session factory on a file-backed sqlite — real per-session connections."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'conservation.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(params=[False, True], ids=["stock_guard_off", "stock_guard_on"])
def all_gates_on(monkeypatch, request):
    """M-A 三闸全开 + 镇库在产开着(vm212 现状),政策存储关 → 走 fallback 税率。

    M-A 加固:整套守恒契约在 `ITEM_STOCK_GUARD_ENABLED` 开/关**两态**下各跑一
    遍(fixture 参数化)。库存守卫只决定"这一件卖不卖得成",不该改变钱怎么流
    ——守恒式两态同口径,才说明加固没在账上留下副作用。
    """
    monkeypatch.setattr(settings, "item_stock_guard_enabled", request.param)
    monkeypatch.setattr(settings, "npc_economy_enabled", True)
    monkeypatch.setattr(settings, "npc_trade_enabled", True)
    monkeypatch.setattr(settings, "caravan_enabled", True)
    monkeypatch.setattr(settings, "tax_carry_enabled", True)
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "polis_policy_enabled", False)
    monkeypatch.setattr(settings, "town_tax_rate_sales", 0.1)
    monkeypatch.setattr(settings, "npc_trade_buy_prob", 1.0)
    monkeypatch.setattr(settings, "npc_trade_reserve_sc", 5)
    monkeypatch.setattr(settings, "npc_trade_max_buys_per_night", 2)
    monkeypatch.setattr(settings, "caravan_stall_fee_sc", 5)
    monkeypatch.setattr(settings, "caravan_budget_sc", 30)
    return settings


@pytest.fixture
def new_gates_off(monkeypatch):
    """vm212 暗上态:主闸可以开着,三个**新**闸全关,镇库照旧在产开着。"""
    monkeypatch.setattr(settings, "npc_economy_enabled", True)
    monkeypatch.setattr(settings, "npc_trade_enabled", False)
    monkeypatch.setattr(settings, "caravan_enabled", False)
    monkeypatch.setattr(settings, "tax_carry_enabled", False)
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "polis_policy_enabled", False)
    monkeypatch.setattr(settings, "town_tax_rate_sales", 0.1)
    monkeypatch.setattr(settings, "town_tax_rate_gift", 0.11)
    monkeypatch.setattr(settings, "realism_relations_enabled", False)
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


def _res(rid, slug, name, *, duty=None, tile=CAFE_TILE):
    """id 与 slug 故意不同形(id 是 uuid 形态的主键,钱包按 slug 记账)。"""
    meta = {"duty": {"key": duty, "perks": {}}} if duty else None
    return Resident(id=rid, slug=slug, name=name, district="free", status="idle",
                    resident_type="npc", tile_x=tile[0], tile_y=tile[1],
                    meta_json=meta)


def _work(code, creator_slug, name, price=WORK_PRICE, stock=3):
    return Item(code=code, kind="resident_work", name=name, description="",
                price_sc=price, payload_json={"creator_slug": creator_slug,
                                              "stock": stock}, active=True)


def _import(code="import_tea", name="茶叶", price=TEA_PRICE, stock=2):
    return Item(code=code, kind="import_good", name=name, description="",
                price_sc=price, payload_json={"caravan": True, "stock": stock},
                active=True)


def _commission(issuer_id, *, status="accepted", acceptor_resident_id=None,
                reward=REWARD, title="带个话"):
    from datetime import datetime, timedelta, UTC
    return Commission(issuer_resident_id=issuer_id, kind="chat_topic", title=title,
                      payload_json={}, reward_sc=reward, status=status,
                      acceptor_resident_id=acceptor_resident_id,
                      expires_at=datetime.now(UTC) + timedelta(hours=48))


async def _seed(sessions, residents=(), items=(), commissions=(), **balances: int):
    async with sessions() as db:
        db.add_all(list(residents) + list(items) + list(commissions))
        for slug, amount in balances.items():
            db.add(ResidentTreasury(resident_slug=slug, balance_sc=amount))
        await db.commit()


async def _supply(sessions) -> int:
    """货币总量 = Σ居民余额 + 镇库(新 session 重读,只认已落库的钱)。

    `town_tax_carry_milli` 是递延税记账**不是钱**,按 spec §6 不进这个和。
    """
    async with sessions() as db:
        residents = sum((await db.execute(
            select(ResidentTreasury.balance_sc))).scalars().all())
        return residents + await treasury_service.balance(db)


async def _stocks(sessions) -> dict[str, tuple[str, int, int]]:
    """`code -> (kind, price, stock)`。买走了什么、买了几件,只有库存说了算。"""
    async with sessions() as db:
        rows = (await db.execute(
            select(Item.code, Item.kind, Item.price_sc, Item.payload_json))).all()
    return {r.code: (r.kind, r.price_sc, int((r.payload_json or {}).get("stock", 0)))
            for r in rows}


def _sold(before: dict, after: dict, kind: str) -> int:
    """两次库存快照之间,某类标的的成交毛额(件数 × 单价)。"""
    total = 0
    for code, (k, price, stock) in before.items():
        if k != kind:
            continue
        total += (stock - after.get(code, (k, price, 0))[2]) * price
    return total


async def _carry(sessions) -> int:
    """递延税账上剩余尾数,单位 **milli-SC**(整数;1 SC = 1000)。"""
    async with sessions() as db:
        row = (await db.execute(select(SystemConfig.value).where(
            SystemConfig.key == treasury_service.TAX_CARRY_KEY))).scalar_one_or_none()
    return 0 if row is None else int(row)


async def _town(sessions) -> int:
    async with sessions() as db:
        return await treasury_service.balance(db)


async def _balance(sessions, slug: str) -> int:
    async with sessions() as db:
        return await coin_service.treasury_balance(db, slug)


async def _wallet(sessions, slug: str):
    async with sessions() as db:
        r = (await db.execute(select(Resident).where(Resident.slug == slug))).scalar_one()
    return (r.meta_json or {}).get("wallet")


async def _snapshot(sessions) -> dict[str, list[str]]:
    """整库快照:每张表的全部行(稳定序的 repr)。零 DB 写入只有这样才验得死——
    数一数行数会漏掉"就地改了一列"的那类泄漏。"""
    async with sessions() as db:
        return {t.name: sorted(repr(r) for r in (await db.execute(select(t))).all())
                for t in Base.metadata.sorted_tables}


async def _eat(sessions, slug: str) -> None:
    from app.agent.phases.execute.basic import _charge_meal

    async with sessions() as db:
        diner = (await db.execute(
            select(Resident).where(Resident.slug == slug))).scalar_one()
        await _charge_meal(db, diner)


class _AlwaysBuys(random.Random):
    """掷骰拍死成"总是买"——本文件测的是钱的去向,不是骰子(同 test_commission_
    npc_accept 把 `npc_trade_buy_prob` 拍成 1.0 的理由)。"""

    def random(self) -> float:
        return 0.0


# =========================================================================== #
# 1. 守恒契约:一晚全链跑完,钱只在商队与进口货两处进出                          #
# =========================================================================== #

async def test_a_full_night_only_gains_the_caravan_and_loses_the_imports(
        sessions, all_gates_on, feed_pushes):
    """小世界:3 个有钱买方、2 件作品、1 单委托、1 件进口货、cafe 一餐。

    顺序照 nightly + event_cron 的真实次序:结算 → 接单 → 消费 → 吃饭 → 商队。
    """
    await _seed(
        sessions,
        [_res("id-chen-0001", "chen", "陈铁生"),         # 发单人(买方之一)
         _res("id-lan-00001", "lan", "阿岚"),            # 承接人(买方之一)
         _res("id-diner-001", "diner", "食客"),          # 买方 + 今晚在咖啡馆吃饭
         _res("id-lin-00001", "lin", "林晚秋", duty="cafe_host"),
         _res("id-maker-001", "maker", "陶匠"),
         _res("id-maker-002", "carver", "木匠")],
        [_work("work_a", "maker", "陶罐"), _work("work_b", "carver", "木碗"),
         _import()],
        [_commission("id-chen-0001", acceptor_resident_id="id-lan-00001")],
        chen=20, lan=3, diner=30,
    )

    before = await _supply(sessions)
    stock_before = await _stocks(sessions)

    async with sessions() as db:
        settled = await npc_trade_service.run_commission_settle_pass(db)
    async with sessions() as db:
        accepted = await npc_trade_service.run_commission_accept_pass(
            db, rng=_AlwaysBuys())
    async with sessions() as db:
        bought = await npc_trade_service.run_consumption_pass(db, rng=_AlwaysBuys())

    # 商队摆摊会把进口货库存重置回 2,所以 sink 必须在到访之前结账。
    stock_after_trade = await _stocks(sessions)
    await _eat(sessions, "diner")
    async with sessions() as db:
        visit = await caravan_service.run_caravan_visit(db, MARKET)

    after = await _supply(sessions)

    # --- 这一晚确实每条流都跑了(不然守恒是空对空) -------------------------- #
    assert settled == {"settled": 1, "paid": REWARD, "reopened": 0}
    assert accepted == {"accepted": 0}, "唯一那单已在结算段完成,没有 open 单可接"
    assert bought["bought"] == 2, f"两笔夜间消费(全镇上限),实测 {bought!r}"
    assert visit["bought"] == 2 and visit["fee"] == 5, f"商队没干活:{visit!r}"
    assert await _balance(sessions, "lin") == settings.npc_meal_cost_sc, (
        "餐费必须已经转到店主手里,否则这一晚的转移面没被覆盖到")

    work_sold = _sold(stock_before, stock_after_trade, "resident_work")
    imported_sold = _sold(stock_before, stock_after_trade, "import_good")
    assert work_sold == WORK_PRICE, "夜间消费里恰好一件本地作品(内部转移)"
    assert imported_sold == TEA_PRICE, "夜间消费里恰好一件进口货(sink)"

    # --- 守恒:整数精确,不是约等于 ----------------------------------------- #
    assert after - before == visit["spent"] + visit["fee"] - imported_sold, (
        f"货币总量只该被商队注入({visit['spent']}+{visit['fee']})与进口 sink"
        f"({imported_sold})推动,其余全是内部转移。实测 Δ={after - before}")

    # --- carry 是递延税记账不是钱:不进总量,单独对账 ------------------------ #
    exact = settings.town_tax_rate_sales * (work_sold + visit["spent"])
    scale = treasury_service.CARRY_SCALE
    carry = await _carry(sessions)
    assert 0 <= carry < scale, f"尾数账只该留不足 1 SC 的零头,实测 {carry} milli"
    assert carry == pytest.approx(
        (exact - (bought["tax"] + visit["tax"])) * scale, abs=1), (
        "carry 必须逐笔等于 exact − cut 的累计——对不上就是有一笔税被吞了或多征了")
    assert await _town(sessions) == bought["tax"] + visit["tax"] + visit["fee"]


# =========================================================================== #
# 2a. 关闸轨一:三新闸全关 → 三 pass + caravan 钩子 + carry 零 DB 写入          #
# =========================================================================== #

@pytest.fixture
def market_day_round(sessions, monkeypatch, feed_pushes):
    """用**真 session**跑一轮 event_cron(驱动姿势沿用 test_caravan_hook:
    `asyncio.sleep` 抛 CancelledError 收尾),返回商队是否被调用过。

    这一节要验的是"关闸连 DB 都不碰",所以 session 不能是 MagicMock;同轮其余段
    落(天气/C3/E3/广播/集体记忆)全部换成 no-op,它们各自的写库不属于本步口径。
    """
    from app.services import debate_service, script_service
    from app.tasks import event_cron, weather

    async def _run(event=MARKET, phase="start"):
        seen: list[tuple] = []

        def _spy(result=None):
            async def _fn(*args, **kwargs):
                return result
            return _fn

        async def _visit(db, ev):
            seen.append((db, ev))
            return {"bought": 0, "spent": 0, "tax": 0, "fee": 0, "imported": 0}

        monkeypatch.setattr(event_cron, "async_session", lambda: sessions())
        monkeypatch.setattr(event_cron, "flip_active_events", _spy([(event, phase)]))
        monkeypatch.setattr(event_cron, "write_collective_memories", _spy(1))
        monkeypatch.setattr(event_cron, "beat", _spy())
        monkeypatch.setattr(event_cron.manager, "broadcast", _spy())
        monkeypatch.setattr(weather, "ensure_weather_event", _spy(None))
        monkeypatch.setattr(caravan_service, "run_caravan_visit", _visit)
        monkeypatch.setattr(script_service, "fire_due_scripts", _spy([]))
        monkeypatch.setattr(script_service, "settle_due_seasons", _spy([]))
        monkeypatch.setattr(script_service, "ensure_active_season", _spy(None))
        monkeypatch.setattr(debate_service, "drive_due_debates",
                            _spy({"live": 0, "settled": 0, "refunded": 0}))

        with patch("app.tasks.event_cron.asyncio.sleep",
                   AsyncMock(side_effect=asyncio.CancelledError())):
            with pytest.raises(asyncio.CancelledError):
                await event_cron.event_cron_loop()
        return seen

    return _run


async def test_new_gates_off_touch_nothing_at_all(sessions, new_gates_off,
                                                  feed_pushes, market_day_round):
    """暗上态的红线:代码全在库里,行为一行不动、一行不写。"""
    await _seed(
        sessions,
        [_res("id-chen-0001", "chen", "陈铁生"),
         _res("id-lan-00001", "lan", "阿岚"),
         _res("id-maker-001", "maker", "陶匠")],
        [_work("work_a", "maker", "陶罐"), _import()],
        [_commission("id-chen-0001", acceptor_resident_id="id-lan-00001"),
         _commission("id-chen-0001", status="open")],
        chen=50, lan=50, maker=50,
    )

    before = await _snapshot(sessions)

    async with sessions() as db:
        assert await npc_trade_service.run_commission_settle_pass(db) == {
            "settled": 0, "paid": 0, "reopened": 0}
        assert await npc_trade_service.run_commission_accept_pass(
            db, rng=_AlwaysBuys()) == {"accepted": 0}
        assert await npc_trade_service.run_consumption_pass(
            db, rng=_AlwaysBuys()) == {"bought": 0, "spent": 0, "tax": 0}
        # carry:分数税账关闸 = 逐字节旧 int() 截断,尾数直接蒸发,不留账行。
        assert await treasury_service.skim_tax_pending(db, 16, 0.05, "sales_tax:x") == 0
        assert await treasury_service.skim_tax(db, 16, 0.05, "sales_tax:x") == 0

    assert await market_day_round() == [], (
        "caravan_enabled 关时集市日开场不许触碰商队——判据与双闸都在 event_cron 的"
        "调用点上,关闸连 import 都不做")

    assert await _snapshot(sessions) == before, "三新闸全关 = 整库一个字节都不许动"
    assert feed_pushes == []


# =========================================================================== #
# 2b. 关闸轨二:`_charge_meal` 与现状基线一致(它现状本就写库,不进零写入快照)   #
# =========================================================================== #

async def test_gate_off_meal_keeps_writing_the_legacy_sink_debit(
        sessions, new_gates_off, feed_pushes):
    """spec §7-2 的例外条:餐费扣款是**既有**行为,关闸口径是"与现状一致"而不是
    "零写入"——把它塞进上面那张零变化快照里,等于要求 M-A 顺手废掉一个在产机制。
    """
    await _seed(
        sessions,
        [_res("id-lin-00001", "lin", "林晚秋", duty="cafe_host"),
         _res("id-diner-001", "diner", "食客")],
        diner=10,
    )

    await _eat(sessions, "diner")

    cost = settings.npc_meal_cost_sc
    assert await _balance(sessions, "diner") == 10 - cost   # sink:钱烧掉了
    assert await _balance(sessions, "lin") == 0             # 店主零收入(现状)
    assert await _wallet(sessions, "diner") == 10 - cost    # 缓存仍无条件刷
    assert await _supply(sessions) == 10 - cost, "关闸下餐费仍是纯 sink,总量减少"
    assert feed_pushes == []


# =========================================================================== #
# 2c. 关闸轨二(续):玩家三条税路径逐字节不变(vm212 主闸已在产开着)            #
# =========================================================================== #

async def test_gate_off_player_resident_work_tax_is_the_legacy_truncation(
        sessions, new_gates_off):
    from app.services import shop_effects

    await _seed(sessions, [_res("id-maker-001", "maker", "陶匠")],
                [_work("work_a", "maker", "陶罐")])

    async with sessions() as db:
        item = (await db.execute(
            select(Item).where(Item.code == "work_a"))).scalar_one()
        out = await shop_effects._resident_work_effect(db, "user-001", item, 1, {})

    # 15 × 0.1 = 1.5 → 旧 int() 截断成 1,尾数 0.5 蒸发(carry 关闸下不许记账)。
    assert out["sales_tax"] == 1 and out["earned"] == WORK_PRICE - 1
    assert await _town(sessions) == 1
    assert await _balance(sessions, "maker") == WORK_PRICE - 1
    assert await _carry(sessions) == 0
    async with sessions() as db:
        assert (await db.execute(select(SystemConfig).where(
            SystemConfig.key == treasury_service.TAX_CARRY_KEY))).scalar_one_or_none() is None


async def test_gate_off_player_gift_tax_is_the_legacy_truncation(sessions, new_gates_off):
    from app.models.user import User
    from app.services import shop_effects

    async with sessions() as db:
        buyer = User(name="buyer", email="gift-buyer@d2.com", soul_coin_balance=200)
        creator = User(name="creator", email="gift-creator@d2.com", soul_coin_balance=0)
        db.add_all([buyer, creator])
        await db.commit()
        resident = Resident(slug="xiaoming", name="小明", creator_id=creator.id,
                            district="central_plaza", status="idle", tile_x=1,
                            tile_y=1, persona_md="p")
        item = Item(code="gift_flower", kind="gift", name="一束花", description="",
                    price_sc=50, active=True, payload_json={"relationship_boost": 0.1})
        db.add_all([resident, item])
        await db.commit()

        out = await shop_effects._gift_effect(db, buyer.id, item, 1,
                                              {"resident_slug": "xiaoming"})

    # share = int(50 × 0.2) = 10 → 税 int(10 × 0.11) = 1(尾数 0.1 蒸发)。
    assert out["gift_tax"] == 1 and out["creator_share"] == 9
    assert await _town(sessions) == 1
    assert await _carry(sessions) == 0
    async with sessions() as db:
        assert (await db.get(User, creator.id)).soul_coin_balance == 9


async def test_gate_off_player_tip_tax_is_the_legacy_truncation(sessions, new_gates_off):
    from app.models.bulletin_post import BulletinPost
    from app.models.user import User
    from app.services import shop_effects

    async with sessions() as db:
        buyer = User(name="buyer", email="tip-buyer@d2.com", soul_coin_balance=200)
        creator = User(name="creator", email="tip-creator@d2.com", soul_coin_balance=0)
        db.add_all([buyer, creator])
        await db.commit()
        resident = Resident(slug="xiaoming", name="小明", creator_id=creator.id,
                            district="central_plaza", status="idle", tile_x=1,
                            tile_y=1, persona_md="p")
        item = Item(code="tip_50sc", kind="tip", name="打赏", description="",
                    price_sc=50, active=True, payload_json={})
        db.add_all([resident, item])
        await db.commit()
        post = BulletinPost(kind="notice", title="t", content_md="c",
                            author_resident_id=resident.id)
        db.add(post)
        await db.commit()

        out = await shop_effects._tip_effect(db, buyer.id, item, 1, {"post_id": post.id})

    # share = int(50 × 0.8) = 40 → 税 int(40 × 0.11) = 4(尾数 0.4 蒸发)。
    assert out["tip_tax"] == 4 and out["creator_share"] == 36
    assert await _town(sessions) == 4
    assert await _carry(sessions) == 0
    async with sessions() as db:
        assert (await db.get(User, creator.id)).soul_coin_balance == 36


# =========================================================================== #
# 3. nightly 全链 smoke:开闸跑一轮真 `run_nightly_jobs`                        #
# =========================================================================== #

@pytest.fixture
def nightly(db_engine, monkeypatch):
    """把整条夜间链的 session 工厂钉到本测试的 in-memory sqlite 上(三处 patch 沿用
    test_nightly_npc_trade.py:110 —— 模块级 / 调用时 import / dream_service 的绑死
    引用),否则整条链会打到共享全局 engine。"""
    import app.database as app_db

    from app.services import dream_service
    from app.tasks import nightly_cron

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(app_db, "async_session", factory)
    monkeypatch.setattr(nightly_cron, "async_session", factory)
    monkeypatch.setattr(dream_service, "async_session", factory)
    return nightly_cron, factory


async def test_nightly_chain_runs_a_real_trade_night_and_logs_the_summary(
        nightly, all_gates_on, feed_pushes, caplog):
    """真 pass、真 DB、整条夜间链:跑得完、钱动了、#23 摘要落进日志。

    开闸后线上就是靠这一行摘要对账的(handoff runbook 的验证项),摘要不出现 =
    看不见的经济。"""
    cron, factory = nightly
    async with factory() as db:
        db.add_all([
            _res("id-buyer-001", "buyer", "买主"),
            _res("id-maker-001", "maker", "陶匠"),
            _work("work_a", "maker", "陶罐"),
            ResidentTreasury(resident_slug="buyer", balance_sc=30),
            ResidentTreasury(resident_slug="maker", balance_sc=0),
        ])
        await db.commit()

    with caplog.at_level("INFO"):
        await cron.run_nightly_jobs()          # 不得抛

    async with factory() as db:
        assert await coin_service.treasury_balance(db, "buyer") == 30 - WORK_PRICE
        earned = await coin_service.treasury_balance(db, "maker")
        town = await treasury_service.balance(db)
    assert earned + town == WORK_PRICE, "作品售出是内部转移:作者 + 镇库 = 毛额"

    assert any("M-A" in r.message for r in caplog.records), (
        "#23 段的摘要必须出现在夜间日志里,否则开闸后线上无从核对")
    assert not any(r.levelname == "ERROR" and "M-A" in r.message
                   for r in caplog.records), "#23 段不许在正常路径上报错"
