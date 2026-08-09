"""M-A C4 — 外来商队(at-most-once 到访,收购/摊位费/进口货,玩家目录隔离)。

Plan: `docs/plans/2026-08-09-M-A-npc-economy.md` Step 9;spec §4 C4。

商队是这套经济里唯一的外生买方(贸易顺差 = 铸币,是设计不是缺陷)。所以这一段的
钉子全在**只准来一次**上:幂等标记 `caravan_last_event_id` 必须在任何资金动作之
前先写先 commit——中途崩溃宁可丢半次到访,绝不重复收费/重复收购。

断言**一律新开 session 重读**(理由同 test_npc_consumption):conftest 的
`:memory:` 引擎走 StaticPool,所有 session 共用一条连接、读得到尚未 commit 的改动,
"标记先落库"这种事务边界会假绿,所以本模块自建文件型 sqlite。fixture 故意让
`Resident.id` ≠ `slug`——钱包按 slug 记账、memory 按 id 记录,串了就露馅。
"""
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
from app.services import caravan_service, coin_service, treasury_service

pytestmark = pytest.mark.anyio

# 集市日事件:`flip_active_events` 交出来的就是这样的 dict(world_event_service:105)。
EVENT = {"id": "evt-market-001", "title": "集市日",
         "payload_json": {"market_day": True}}
OTHER_EVENT = {"id": "evt-market-002", "title": "又一个集市日",
               "payload_json": {"market_day": True}}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def sessions(tmp_path):
    """Session factory on a file-backed sqlite — real per-session connections."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'caravan.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def caravan_on(monkeypatch):
    """C4 闸开(镇库默认关——摊位费与销售税各自有闸,分开验)。"""
    monkeypatch.setattr(settings, "npc_economy_enabled", True)
    monkeypatch.setattr(settings, "caravan_enabled", True)
    monkeypatch.setattr(settings, "caravan_stall_fee_sc", 5)
    monkeypatch.setattr(settings, "caravan_budget_sc", 30)
    monkeypatch.setattr(settings, "town_treasury_enabled", False)
    monkeypatch.setattr(settings, "polis_policy_enabled", False)
    return settings


@pytest.fixture
def tax_on(monkeypatch):
    """镇库 + 分数税账两闸开(销售税率 0.1)——摊位费也只在镇库开时才收。"""
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


async def _seed(sessions, residents=(), items=(), **balances: int) -> None:
    async with sessions() as db:
        db.add_all(list(residents) + list(items))
        for slug, amount in balances.items():
            db.add(ResidentTreasury(resident_slug=slug, balance_sc=amount))
        await db.commit()


async def _run(sessions, event=EVENT) -> dict:
    async with sessions() as db:
        return await caravan_service.run_caravan_visit(db, event)


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


async def _items(sessions, kind: str) -> list[Item]:
    async with sessions() as db:
        return list((await db.execute(
            select(Item).where(Item.kind == kind).order_by(Item.code)
        )).scalars().all())


async def _memories(sessions, resident_id: str) -> list[str]:
    async with sessions() as db:
        rows = (await db.execute(
            select(Memory.content).where(Memory.resident_id == resident_id)
        )).scalars().all()
    return list(rows)


async def _town(sessions) -> int:
    async with sessions() as db:
        return await treasury_service.balance(db)


async def _marker(sessions):
    async with sessions() as db:
        return (await db.execute(select(SystemConfig).where(
            SystemConfig.key == caravan_service.LAST_VISIT_KEY))).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# 1. 闸关(任一)→ no-op 零写入                                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("off_key", ["caravan_enabled", "npc_economy_enabled"])
async def test_gate_off_is_a_noop_with_zero_writes(sessions, caravan_on, tax_on,
                                                   feed_pushes, monkeypatch, off_key):
    monkeypatch.setattr(settings, off_key, False)
    await _seed(sessions, [_res("id-maker-001", "maker", "作者")],
                [_work("work_a", "maker", "陶罐")])

    assert await _run(sessions) == {
        "bought": 0, "spent": 0, "tax": 0, "fee": 0, "imported": 0}

    assert await _marker(sessions) is None
    assert await _town(sessions) == 0
    assert await _balance(sessions, "maker") == 0
    assert (await _item(sessions, "work_a")).payload_json["stock"] == 3
    assert await _items(sessions, "import_good") == []
    assert feed_pushes == []


# --------------------------------------------------------------------------- #
# 2. 首访:标记先落库 → 摊位费 → 收购 → 进口货                                   #
# --------------------------------------------------------------------------- #

async def test_first_visit_marks_pays_buys_and_stocks_imports(sessions, caravan_on,
                                                              tax_on, feed_pushes):
    """预算 30、两件 15 → 各买 1。税:15×0.1=1.5 → 首件征 1(carry 0.5),次件
    1.5+0.5=2.0 → 征 2(carry 0)。摊位费 5 另计。"""
    await _seed(
        sessions,
        [_res("id-maker-001", "maker", "陶匠"), _res("id-maker-002", "carver", "木匠")],
        [_work("work_a", "maker", "陶罐"), _work("work_b", "carver", "木碗"),
         # 上一次到访留下的空货架:复活模式(active=True + payload 重赋值)。
         Item(code="import_tea", kind="import_good", name="茶叶", description="",
              price_sc=6, payload_json={"caravan": True, "stock": 0}, active=False)],
    )

    summary = await _run(sessions)

    assert summary == {"bought": 2, "spent": 30, "tax": 3, "fee": 5, "imported": 3}
    assert (await _marker(sessions)).value == EVENT["id"]
    assert await _balance(sessions, "maker") == 14          # 15 − 1
    assert await _balance(sessions, "carver") == 13         # 15 − 2
    assert await _town(sessions) == 8                       # 摊位费 5 + 税 3
    assert await _wallet(sessions, "maker") == 14           # 作者钱包缓存刷新
    assert await _wallet(sessions, "carver") == 13

    assert (await _item(sessions, "work_a")).payload_json["stock"] == 2  # 重赋值模式
    assert (await _item(sessions, "work_b")).payload_json["stock"] == 2

    imports = await _items(sessions, "import_good")
    assert [i.code for i in imports] == ["import_cloth", "import_tea", "import_trinket"]
    assert all(i.active and i.payload_json == {"caravan": True, "stock": 2}
               for i in imports)
    tea = next(i for i in imports if i.code == "import_tea")
    assert tea.price_sc == 6                                 # 复活的那件也回到定价

    assert any("商队" in m for m in await _memories(sessions, "id-maker-001"))
    assert sorted((slug, kind) for slug, kind, _ in feed_pushes) == [
        ("carver", "caravan_purchase"), ("maker", "caravan_purchase")]


# --------------------------------------------------------------------------- #
# 3. 同一个 event 二访 → 零写入(at-most-once)                                   #
# --------------------------------------------------------------------------- #

async def test_second_visit_of_the_same_event_writes_nothing(sessions, caravan_on,
                                                             tax_on, feed_pushes):
    await _seed(sessions, [_res("id-maker-001", "maker", "陶匠")],
                [_work("work_a", "maker", "陶罐")])
    first = await _run(sessions)
    feed_pushes.clear()

    assert await _run(sessions) == {
        "bought": 0, "spent": 0, "tax": 0, "fee": 0, "imported": 0}

    assert first["bought"] == 1
    assert await _balance(sessions, "maker") == 14
    assert await _town(sessions) == 6                        # 摊位费只收了一次
    assert (await _item(sessions, "work_a")).payload_json["stock"] == 2
    assert feed_pushes == []

    # 下一个集市日(新 event id)照常再来一次。
    assert (await _run(sessions, OTHER_EVENT))["bought"] == 1
    assert (await _marker(sessions)).value == OTHER_EVENT["id"]


# --------------------------------------------------------------------------- #
# 4. 玩家目录隔离:进口货不上架、买不到                                           #
# --------------------------------------------------------------------------- #

async def test_import_goods_are_invisible_to_players(sessions):
    from app.services.shop_service import get_catalog

    await _seed(sessions, items=[
        Item(code="gift_flower", kind="gift", name="一束花", description="",
             price_sc=15, payload_json={}, active=True),
        Item(code="import_tea", kind="import_good", name="茶叶", description="",
             price_sc=6, payload_json={"caravan": True, "stock": 2}, active=True),
    ])

    async with sessions() as db:
        assert [i["code"] for i in await get_catalog(db)] == ["gift_flower"]


async def test_players_cannot_purchase_an_import_good(sessions):
    from app.services.shop_service import ShopError, purchase

    await _seed(sessions, items=[
        Item(code="import_tea", kind="import_good", name="茶叶", description="",
             price_sc=6, payload_json={"caravan": True, "stock": 2}, active=True),
    ])

    async with sessions() as db:
        with pytest.raises(ShopError):
            await purchase(db, "user-001", "import_tea")
        # 拒得比扣款早:库存一件没动。
        item = (await db.execute(
            select(Item).where(Item.code == "import_tea"))).scalar_one()
        assert item.payload_json["stock"] == 2


# --------------------------------------------------------------------------- #
# 5. 买不动也不许崩:费与进口货照常                                              #
# --------------------------------------------------------------------------- #

async def test_budget_below_unit_price_still_pays_fee_and_stocks(sessions, caravan_on,
                                                                 tax_on, feed_pushes):
    await _seed(sessions, [_res("id-maker-001", "maker", "陶匠")],
                [_work("work_pricey", "maker", "大件", price=50)])

    assert await _run(sessions) == {
        "bought": 0, "spent": 0, "tax": 0, "fee": 5, "imported": 3}
    assert await _balance(sessions, "maker") == 0
    assert await _town(sessions) == 5
    assert (await _item(sessions, "work_pricey")).payload_json["stock"] == 3
    assert len(await _items(sessions, "import_good")) == 3
    assert feed_pushes == []


async def test_no_works_on_sale_still_pays_fee_and_stocks(sessions, caravan_on, tax_on,
                                                          feed_pushes):
    await _seed(sessions, [_res("id-maker-001", "maker", "陶匠")])

    assert await _run(sessions) == {
        "bought": 0, "spent": 0, "tax": 0, "fee": 5, "imported": 3}
    assert await _town(sessions) == 5


async def test_town_treasury_off_skips_the_stall_fee(sessions, caravan_on, feed_pushes):
    """摊位费与销售税都吊在 `town_treasury_enabled` 上——镇库没开就只有收购。"""
    await _seed(sessions, [_res("id-maker-001", "maker", "陶匠")],
                [_work("work_a", "maker", "陶罐")])

    assert await _run(sessions) == {
        "bought": 1, "spent": 15, "tax": 0, "fee": 0, "imported": 3}
    assert await _balance(sessions, "maker") == 15            # 不抽税,全额到手
    assert await _town(sessions) == 0


# --------------------------------------------------------------------------- #
# 6. 单件失败 → rollback 该件继续;标记已落 → 不会重复收费                        #
# --------------------------------------------------------------------------- #

async def test_failed_purchase_rolls_back_and_never_recharges_on_retry(
        sessions, caravan_on, tax_on, feed_pushes, monkeypatch):
    await _seed(
        sessions,
        [_res("id-maker-001", "maker", "陶匠"), _res("id-maker-002", "carver", "木匠")],
        [_work("work_a", "maker", "陶罐"), _work("work_b", "carver", "木碗")],
    )

    real = coin_service.treasury_credit_pending
    calls = {"n": 0}

    async def _flaky(db, slug, amount, reason=""):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("credit leg exploded")
        return await real(db, slug, amount, reason)

    monkeypatch.setattr(coin_service, "treasury_credit_pending", _flaky)

    assert await _run(sessions) == {
        "bought": 1, "spent": 15, "tax": 1, "fee": 5, "imported": 3}
    assert await _balance(sessions, "maker") == 0             # 炸掉的那件零污染
    assert await _balance(sessions, "carver") == 14
    assert (await _item(sessions, "work_a")).payload_json["stock"] == 3
    assert (await _item(sessions, "work_b")).payload_json["stock"] == 2
    assert await _town(sessions) == 6                         # 摊位费 5 + 次件税 1
    assert await _memories(sessions, "id-maker-001") == []

    # 标记在任何资金动作之前就落了库:重跑同一个 event 不会再收一次摊位费。
    assert await _run(sessions) == {
        "bought": 0, "spent": 0, "tax": 0, "fee": 0, "imported": 0}
    assert await _town(sessions) == 6
    assert await _balance(sessions, "carver") == 14


async def test_a_crash_mid_visit_never_double_charges_on_the_next_pass(
        sessions, caravan_on, tax_on, feed_pushes, monkeypatch):
    """到访中途硬崩(异常穿出去由 event_cron 兜):标记已经在**任何资金动作之前**
    落了库,所以下一轮同一个 event 是彻底的 no-op —— 宁可丢半次到访(少摆一次
    摊),也绝不重复收摊位费/重复收购。"""
    await _seed(sessions, [_res("id-maker-001", "maker", "陶匠")],
                [_work("work_a", "maker", "陶罐")])

    async def _boom(db):
        raise RuntimeError("caravan visit exploded mid-way")

    monkeypatch.setattr(caravan_service, "_stock_import_goods", _boom)

    with pytest.raises(RuntimeError):
        async with sessions() as db:
            await caravan_service.run_caravan_visit(db, EVENT)

    assert (await _marker(sessions)).value == EVENT["id"]
    assert await _town(sessions) == 6                        # 摊位费 5 + 收购税 1
    assert await _balance(sessions, "maker") == 14
    assert await _items(sessions, "import_good") == []       # 崩在摆摊这步

    # 重跑同一个 event:在标记那一步就掉头,一分钱都不动(_boom 都碰不到)。
    assert await _run(sessions) == {
        "bought": 0, "spent": 0, "tax": 0, "fee": 0, "imported": 0}
    assert await _town(sessions) == 6
    assert await _balance(sessions, "maker") == 14
    assert (await _item(sessions, "work_a")).payload_json["stock"] == 2


async def test_the_marker_lands_before_any_money_moves(sessions, caravan_on, tax_on,
                                                       feed_pushes, monkeypatch):
    """标记写失败 = 这次到访一分钱都还没动过。

    钉的是**顺序**:摊位费若抢在标记之前落库,这一崩就会在下一轮被重收一次。所以
    标记那步炸掉时,镇库必须是干净的,重跑才恰好收一次费。"""
    await _seed(sessions, [_res("id-maker-001", "maker", "陶匠")],
                [_work("work_a", "maker", "陶罐")])

    real = treasury_service.kv_upsert_pending
    calls = {"n": 0}

    async def _flaky(db, key, value, *, group="town", updated_by):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("marker write exploded")
        return await real(db, key, value, group=group, updated_by=updated_by)

    monkeypatch.setattr(treasury_service, "kv_upsert_pending", _flaky)

    with pytest.raises(RuntimeError):
        async with sessions() as db:
            await caravan_service.run_caravan_visit(db, EVENT)

    assert await _marker(sessions) is None
    assert await _town(sessions) == 0                        # 摊位费还没敢收
    assert await _balance(sessions, "maker") == 0

    # 重跑:这才是这一场集市唯一的一次到访,费只收一次。
    assert (await _run(sessions))["fee"] == 5
    assert await _town(sessions) == 6                        # 摊位费 5 + 收购税 1
    assert await _balance(sessions, "maker") == 14


# --------------------------------------------------------------------------- #
# 7. 作者已被清号 → 孤儿作品顺手下架,不付款                                      #
# --------------------------------------------------------------------------- #

async def test_orphan_work_is_delisted_instead_of_bought(sessions, caravan_on, tax_on,
                                                         feed_pushes):
    """item 生命周期与居民解耦(vm212 有存量孤儿):买了没人收钱,直接摘掉。"""
    await _seed(sessions, items=[_work("work_ghost", "ghost", "无主之作")])

    assert (await _run(sessions))["bought"] == 0
    assert (await _item(sessions, "work_ghost")).active is False
    assert await _town(sessions) == 5                         # 只有摊位费
    assert feed_pushes == []


async def test_shop_keeper_restock_skips_import_goods(sessions):
    """终审修复:何巧云补货/调价只看本店商品——商队进口货不补、不调价、不发公告
    (进口货对玩家目录不可见,「到货公告」会把玩家引向一件搜不到也买不到的商品)。"""
    from app.services import duty_service

    async with sessions() as db:
        keeper = _res("r-keeper", "he-qiaoyun", "何巧云")
        db.add(keeper)
        db.add(Item(code="import_tea", kind="import_good", name="茶叶", description="",
                    price_sc=6, payload_json={"caravan": True, "stock": 2}, active=True))
        await db.commit()
        out = await duty_service._work_shop_keeper(db, keeper)
        assert out is None

    async with sessions() as db:
        tea = (await db.execute(select(Item).where(Item.code == "import_tea"))).scalar_one()
        assert tea.price_sc == 6
