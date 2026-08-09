"""world_event 记忆的 importance 分档(REALISM_EVENT_MEMORY_*)。

`write_collective_memories` 一直是 `db.add(Memory(...))` 直写,绕过
`MemoryService.add_memory` 也就绕过了 `_normalize_importance`,importance 是硬编码
的 0.5 / 0.6。而检索候选池 `_fetch_event_candidates` 按 `importance DESC` 静态截前
30(`app/memory/service.py:308`),生产实测每位居民 top-30 第 30 名都在 0.95-1.0 ——
1311 条 world_event 记忆一条都进不去,**写了等于没写**。

修法是**分档**,不是整体抬高:1253/1311(96%)是天气,抬上去就是拿「今天多云」把
只有 30 个坑的候选池灌满,个人记忆被挤光,那比现状更糟。

- **琐事档**(天气 / 集市日节庆)→ 直写,逐字节不变;
- **实质档**(其余)→ 走 `add_memory` 参与分位归一。

T1 只钉两个旋钮的存在与默认值,以及 deploy 模板的 parity:`REALISM_` 不在
`GOVERNANCE_PREFIXES`(只有 `CIVIC_`/`REP_`/`POLIS_OFFICE_`)里,那条自动 parity
覆盖不到本批 —— 运维照 deploy 模板起的环境读不到的键 = 不存在的键。

T2 的两侧各有硬断言守着:

- **闸关侧是快照**:落库行的 importance / metadata / content 长度 / 返回值逐项咬死。
  只测「importance 还是 0.5」不够 —— 走 `add_memory` 同样能落 0.5,但会多一个
  `raw_importance` 键、并把 content 从 200 截到 80(E-28)。快照式断言才分得清
  「没改这条路」与「改了但恰好数字一样」。
- **闸开侧断言落库值,不是传入的 raw**:`_normalize_importance` 是**分位数**
  (`app/memory/service.py:243`),全窗口同为 0.9 时 raw=0.9 反而落 0.5。只 spy raw
  是假绿,所以每条断言都配一句「该行确实出现在 cap=30 的候选池里」。
"""
import random

import pytest
from sqlalchemy import select

from app.agent.map_data import get_location_by_id
from app.config import Settings, settings
from app.memory.service import EVENT_MEMORY_MAX_CHARS, MemoryService
from app.models.memory import Memory
from app.models.resident import Resident
from app.services import world_event_service as wes
from tests.test_civic_memory_broadcast import _HISTORY, _seed_history

#: (字段名, 保守默认值)。默认必须是「关 + 与现状逐字节一致」——开闸是另一次
#: 独立的部署变更(红线:行为开闸与代码变更不同车)。
KNOBS = [
    ("realism_event_memory_tiered", False),
    ("realism_event_memory_importance", 0.9),
]

#: 直写那条路的 content 上限(`world_event_service` 自己截的 200)。事件描述灌到这
#: 之上,落库长度就成了「走的哪条路」的指纹:直写 200,`add_memory` 81
#: (`EVENT_MEMORY_MAX_CHARS` + 省略号)。
_DIRECT_CONTENT_CAP = 200

#: `_fetch_event_candidates` 的真实池深(= `max(max_events*3, 30)`,
#: `app/memory/service.py:364`)。进不了这 30 条 = 写了等于没写。
_POOL_CAP = 30

#: `_HISTORY`(35 条:0.9×3 + 0.8×4 + 0.7×28)下 raw=0.9 的归一结果。
#: below=32、equal=3 → (32 + 0.5×3)/35 = 0.9571。这个数是**算出来再实测对上的**,
#: 写死是为了「哪天窗口口径动了当场红」——本批明确不动 `_normalize_importance`。
_NORMALIZED_AT_HISTORY = 0.9571

#: 同一份历史下候选池第 30 名的 importance。0.5 / 0.6 的直写值低于它 = 生产那条
#: 「写了等于没写」的复现条件;这个数存在的意义是让下面的「进池」断言不是废话。
_POOL_FLOOR_AT_HISTORY = 0.7


def test_tier_knobs_are_settings_fields_with_conservative_defaults():
    fields = Settings.model_fields
    for name, default in KNOBS:
        assert name in fields, f"旋钮 {name} 不在 Settings 里,REALISM_ 前缀的 env 会被拒"
        assert fields[name].default == default, (
            f"{name} 的默认值必须是关/保守,期望 {default!r},"
            f"实得 {fields[name].default!r}")


# ── 夹具 ────────────────────────────────────────────────────────────────

@pytest.fixture
def realism_on(monkeypatch):
    """生产 ``REALISM_ENABLED=true``。

    闸关那几条测试**也要开它**:归一化只在这个总闸下生效,关着测「没走
    add_memory」等于把被测差异先抹掉了 —— 那条路径下 raw 原样落库,两条路的
    importance 恰好长得一样。
    """
    monkeypatch.setattr(settings, "realism_enabled", True)


@pytest.fixture
def tiered_on(monkeypatch):
    monkeypatch.setattr(settings, "realism_event_memory_tiered", True)


@pytest.fixture
def gradient_on(monkeypatch):
    """生产 ``REALISM_INFO_GRADIENT_ENABLED=true``。"""
    monkeypatch.setattr(settings, "realism_info_gradient_enabled", True)


def _plaza_center() -> tuple[int, int]:
    loc = get_location_by_id("central_plaza")
    return loc.get("center") or (
        (loc["bounds"][0] + loc["bounds"][2]) // 2,
        (loc["bounds"][1] + loc["bounds"][3]) // 2)


async def _residents(db, n: int, tile: tuple[int, int]) -> list[str]:
    for i in range(n):
        db.add(Resident(id=f"r{i}", slug=f"r{i}", name=f"R{i}", creator_id="sys",
                        district="cafe", status="idle", tile_x=tile[0], tile_y=tile[1]))
    await db.commit()
    return [f"r{i}" for i in range(n)]


async def _rows(db, event_id: str | None = None) -> list[Memory]:
    rows = list((await db.execute(
        select(Memory).where(Memory.source == "world_event")
    )).scalars().all())
    if event_id is None:
        return rows
    return [m for m in rows if (m.metadata_json or {}).get("event_id") == event_id]


def _long_description(marker: str) -> str:
    """比两条路的两个上限都长的描述 —— 落库长度才当得了「走的哪条路」的指纹。"""
    return marker + "田里的作物成熟了，摊主们把新收的谷子摊在广场上。" * 20


# ── 闸关:逐字节不变 ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_gate_off_broadcast_path_is_byte_identical(db_session, realism_on):
    """闸关 + 梯度关(pre-P2 全知广播):落库形状逐项咬死。

    四项一个都不能少 —— 它们各自能抓到一种「顺手改了天气路」的走样:
    importance 抓档位、``metadata_json`` 全等抓 ``raw_importance``(走 add_memory
    才有的那个键)、content 长度抓 E-28 的 80 字截断、返回值抓收件人条数。
    """
    ids = await _residents(db_session, 20, (0, 0))
    content = _long_description("天气：")

    n = await wes.write_collective_memories(
        db_session, {"id": "w-1", "type": "weather",
                     "description": content, "payload_json": {}})

    assert n == 20
    rows = await _rows(db_session)
    assert {m.resident_id for m in rows} == set(ids)
    for m in rows:
        assert m.type == "event" and m.source == "world_event"
        assert m.importance == pytest.approx(0.5)
        assert m.metadata_json == {"first_hand": True, "event_id": "w-1"}
        assert m.content == content[:_DIRECT_CONTENT_CAP]


@pytest.mark.anyio
async def test_gate_off_gradient_path_is_byte_identical(
        db_session, realism_on, gradient_on):
    """闸关 + 梯度开(**生产今天的形状**):geo 0.6 / 随机样本 0.5,两档都不许动。"""
    cx, cy = _plaza_center()
    db_session.add(Resident(id="near", slug="near", name="N", creator_id="sys",
                            district="cafe", status="idle", tile_x=cx, tile_y=cy))
    for i in range(19):
        db_session.add(Resident(id=f"far{i}", slug=f"far{i}", name=f"F{i}",
                                creator_id="sys", district="cafe", status="idle",
                                tile_x=cx + 500, tile_y=cy + 500))
    await db_session.commit()
    content = _long_description("公开课：")

    n = await wes.write_collective_memories(
        db_session,
        {"id": "ev-1", "type": "festival", "description": content,
         "payload_json": {"location_id": "central_plaza"}},
        rng=random.Random(0))

    rows = await _rows(db_session)
    assert n == len(rows)
    landed = {m.resident_id: m for m in rows}
    assert landed["near"].importance == pytest.approx(0.6)   # geo 档
    assert all(m.importance == pytest.approx(0.5)            # 随机样本档
               for rid, m in landed.items() if rid != "near")
    for m in rows:
        assert m.metadata_json == {"first_hand": True, "event_id": "ev-1"}
        assert m.content == content[:_DIRECT_CONTENT_CAP]


@pytest.mark.anyio
async def test_gate_off_event_without_id_carries_only_first_hand(db_session, realism_on):
    """没有 id 的事件(``test_world_events_ops`` 就这么调)metadata 里不该凭空多键。"""
    await _residents(db_session, 2, (0, 0))
    assert await wes.write_collective_memories(
        db_session, {"title": "丰收节", "description": "田里的作物成熟了"}) == 2
    assert all(m.metadata_json == {"first_hand": True}
               for m in await _rows(db_session))


@pytest.mark.anyio
async def test_gate_off_reproduces_the_production_defect(
        db_session, realism_on, gradient_on):
    """闸关时实质事件**进不了候选池** —— 这条是被修缺陷本身的复现。

    没有它,下面那条「进池了」就没有对照:一个恒真的池子里谁都进得去。
    """
    cx, cy = _plaza_center()
    await _residents(db_session, 1, (cx, cy))
    await _seed_history(db_session, "r0", _HISTORY)

    await wes.write_collective_memories(
        db_session,
        {"id": "ev-off", "type": "news", "description": "镇上要修一座剧院",
         "payload_json": {"location_id": "central_plaza"}},
        rng=random.Random(0))

    mem = (await _rows(db_session, "ev-off"))[0]
    assert mem.importance == pytest.approx(0.6)
    assert mem.importance < _POOL_FLOOR_AT_HISTORY
    pool = await MemoryService(db_session)._fetch_event_candidates("r0", cap=_POOL_CAP)
    assert len(pool) == _POOL_CAP
    assert mem.id not in {m.id for m in pool}, "闸关时它就不该进池,否则这批改动没有缺陷可修"


# ── 闸开:琐事档仍旧直写 ────────────────────────────────────────────────

@pytest.mark.anyio
async def test_weather_stays_a_direct_write_when_tiered(
        db_session, realism_on, gradient_on, tiered_on):
    """96% 的量。抬上去 = 拿「今天多云」把 30 个坑灌满,比现状更糟。"""
    await _residents(db_session, 20, (0, 0))
    content = _long_description("天气：")

    n = await wes.write_collective_memories(
        db_session, {"id": "w-2", "type": "weather",
                     "description": content, "payload_json": {}})

    assert n == 20
    for m in await _rows(db_session):
        assert m.importance == pytest.approx(0.5)
        assert m.metadata_json == {"first_hand": True, "event_id": "w-2"}
        assert m.content == content[:_DIRECT_CONTENT_CAP]


@pytest.mark.anyio
async def test_market_day_festival_stays_trivial_when_tiered(
        db_session, realism_on, gradient_on, tiered_on):
    """集市日归琐事:它每周复现(``MARKET_DAY_WEEKDAY``),而 NPC 已经从事实层
    (``town_facts`` 的 ``today.is_market_day``)知道今天是不是集市日 —— 再占一个
    候选池的坑只会自我复制。判据与 ``shop_service._market_discount`` 同源。"""
    cx, cy = _plaza_center()
    await _residents(db_session, 1, (cx, cy))
    content = _long_description("集市日：")

    await wes.write_collective_memories(
        db_session,
        {"id": "m-1", "type": "festival", "description": content,
         "payload_json": {"market_day": True, "location_id": "central_plaza"}},
        rng=random.Random(0))

    mem = (await _rows(db_session, "m-1"))[0]
    assert mem.importance == pytest.approx(0.6)
    assert mem.metadata_json == {"first_hand": True, "event_id": "m-1"}
    assert mem.content == content[:_DIRECT_CONTENT_CAP]


# ── 闸开:实质档走归一化 ────────────────────────────────────────────────

@pytest.mark.anyio
@pytest.mark.parametrize("event_type,payload", [
    ("news", {}),                       # 一次性的叙事事件
    ("script", {}),                     # C3 剧本幕
    ("festival", {}),                   # 非集市日的节庆(儿童节 / 公开课)
    ("festival", {"market_day": False}),  # 键在但为假 —— 不能只看键存在
])
async def test_substantive_event_lands_in_the_top30_pool(
        db_session, realism_on, gradient_on, tiered_on, event_type, payload):
    """落库 importance 归一后 ≥0.95,且该行**确实在** cap=30 的候选池里。

    断言落库值不是传入的 raw:``_normalize_importance`` 是分位数,全窗口同为 0.9
    时 raw=0.9 反而落 0.5。``_HISTORY``(0.9×3 + 0.8×4 + 0.7×28)下 raw=0.9 归一到
    0.9571 —— 这个数配着「第 30 名是 0.7」才说明问题:0.5/0.6 的直写值进不去,
    归一后的这条排在池首。
    """
    cx, cy = _plaza_center()
    await _residents(db_session, 1, (cx, cy))
    await _seed_history(db_session, "r0", _HISTORY)
    content = _long_description("公告：")

    n = await wes.write_collective_memories(
        db_session,
        {"id": "sub-1", "type": event_type, "description": content,
         "payload_json": {**payload, "location_id": "central_plaza"}},
        rng=random.Random(0))

    assert n == 1
    mem = (await _rows(db_session, "sub-1"))[0]
    # ① 落库值(不是 raw)在最高档
    assert mem.importance == pytest.approx(_NORMALIZED_AT_HISTORY)
    assert mem.importance >= 0.95
    # ② raw 留痕 + 第一手元数据不丢(扩散探针与 gossip 靠 event_id 追链)
    assert mem.metadata_json["raw_importance"] == pytest.approx(
        settings.realism_event_memory_importance)
    assert mem.metadata_json["first_hand"] is True
    assert mem.metadata_json["event_id"] == "sub-1"
    # ③ 真的进了池子,而且排在第 30 名之上
    pool = await MemoryService(db_session)._fetch_event_candidates("r0", cap=_POOL_CAP)
    assert len(pool) == _POOL_CAP
    assert mem.id in {m.id for m in pool}
    assert pool[-1].importance == pytest.approx(_POOL_FLOOR_AT_HISTORY)
    # ④ 走的是 add_memory,所以吃 E-28 的 80 字截断(与直写的 200 不同)
    assert len(mem.content) == EVENT_MEMORY_MAX_CHARS + 1


@pytest.mark.anyio
async def test_tier_gate_does_not_move_the_recipient_set(
        db_session, realism_on, gradient_on, tiered_on, monkeypatch):
    """梯度管「谁知道」,分档管「多重要」,两件事不耦合。

    同一份 rng 种子、同一个事件形状,闸开闸关必须挑中**同一批人**。收件人算法本批
    明确不动,这条就是那句「不动」的兑现。
    """
    await _residents(db_session, 20, (0, 0))   # 无 location → 只剩随机样本那一支
    shape = {"type": "news", "description": "镇上要修一座剧院", "payload_json": {}}

    monkeypatch.setattr(settings, "realism_event_memory_tiered", False)
    n_off = await wes.write_collective_memories(
        db_session, {**shape, "id": "cmp-off"}, rng=random.Random(0))
    monkeypatch.setattr(settings, "realism_event_memory_tiered", True)
    n_on = await wes.write_collective_memories(
        db_session, {**shape, "id": "cmp-on"}, rng=random.Random(0))

    assert n_off == n_on == round(settings.realism_info_sample_frac * 20)
    assert ({m.resident_id for m in await _rows(db_session, "cmp-off")}
            == {m.resident_id for m in await _rows(db_session, "cmp-on")})


@pytest.mark.anyio
async def test_substantive_write_is_fail_open_and_returns_partial_count(
        db_session, realism_on, gradient_on, tiered_on, monkeypatch):
    """写到一半炸了:返回已写条数,不把 event_cron 的调用链带崩。

    这条比 civic 那条更要紧:``add_memory`` 自带 ``commit()``
    (``memory/service.py:95``),而调用点 ``tasks/event_cron.py:41`` 的**同一个
    session** 后面还要给 C4 商队 / C3 / E3 用。半截异常若原样抛出去,session 会停在
    ``PendingRollbackError``,下一步的第一条语句当场炸并被误算到那一步头上
    (``event_cron.py:60-62`` 已经踩过一次)。
    """
    await _residents(db_session, 20, (0, 0))
    real_add = MemoryService.add_memory
    calls = {"n": 0}

    async def _flaky(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("db went away")
        return await real_add(self, *args, **kwargs)

    monkeypatch.setattr(MemoryService, "add_memory", _flaky)
    n = await wes.write_collective_memories(
        db_session, {"id": "boom", "type": "news", "description": "镇上要修一座剧院",
                     "payload_json": {}}, rng=random.Random(0))

    assert n == 1
    assert len(await _rows(db_session, "boom")) == 1
    # session 还能用 —— 否则 C4/C3/E3 会替本函数背锅
    assert (await db_session.execute(select(Memory.id))).scalars().first() is not None
