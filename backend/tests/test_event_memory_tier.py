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
from tests.test_civic_memory_broadcast import _FAKE_EMB, _HISTORY, _seed_history

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

    W1 起 metadata 里多了一个 ``tier`` —— 它**与本文件这个闸门无关**(闸开闸关都写、
    值相同),快照因此照它的新形状重标,而不是放松成「包含」。tier 自己的不变量由
    ``tests/test_event_tier_marker.py`` 单独证。
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
        assert m.metadata_json == {"first_hand": True, "event_id": "w-1",
                                   "tier": "trivia"}
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
        assert m.metadata_json == {"first_hand": True, "event_id": "ev-1",
                                   "tier": "substantive"}
        assert m.content == content[:_DIRECT_CONTENT_CAP]


@pytest.mark.anyio
async def test_gate_off_event_without_id_carries_only_first_hand(db_session, realism_on):
    """没有 id 的事件(``test_world_events_ops`` 就这么调)metadata 里不该凭空多键。"""
    await _residents(db_session, 2, (0, 0))
    assert await wes.write_collective_memories(
        db_session, {"title": "丰收节", "description": "田里的作物成熟了"}) == 2
    assert all(m.metadata_json == {"first_hand": True, "tier": "substantive"}
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
        assert m.metadata_json == {"first_hand": True, "event_id": "w-2",
                                   "tier": "trivia"}
        assert m.content == content[:_DIRECT_CONTENT_CAP]


@pytest.mark.anyio
async def test_market_day_festival_stays_trivial_when_tiered(
        db_session, realism_on, gradient_on, tiered_on, monkeypatch):
    """集市日归琐事:它每周复现(``MARKET_DAY_WEEKDAY``),而 NPC 已经从事实层
    (``town_facts`` 的 ``today.is_market_day``)知道今天是不是集市日 —— 再占一个
    候选池的坑只会自我复制。判据与 ``shop_service._market_discount`` 同源。"""
    monkeypatch.setattr(settings, "market_day_venue", "market_hall")
    cx, cy = get_location_by_id("market_hall")["center"]
    await _residents(db_session, 1, (cx, cy))
    content = _long_description("集市日：")

    await wes.write_collective_memories(
        db_session,
        {"id": "m-1", "type": "festival", "description": content,
         "payload_json": {"market_day": True, "location_id": "central_plaza"}},
        rng=random.Random(0))

    mem = (await _rows(db_session, "m-1"))[0]
    assert mem.importance == pytest.approx(0.6)
    assert mem.metadata_json == {"first_hand": True, "event_id": "m-1",
                                 "tier": "trivia"}
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


# ── 实质档的写入侧 embedding(E1) ───────────────────────────────────────

@pytest.fixture
def embed_calls(monkeypatch):
    """写入侧 ``generate_embedding`` 的计数桩(与 civic 那份同构)。"""
    calls: list[str] = []

    async def _fake(text: str):
        calls.append(text)
        return list(_FAKE_EMB)

    monkeypatch.setattr(wes, "generate_embedding", _fake)
    return calls


@pytest.mark.anyio
async def test_substantive_write_embeds_once_per_event_not_per_recipient(
        db_session, realism_on, tiered_on, embed_calls):
    """梯度关 → 20 人全知情,``_write_substantive`` 写 20 条、embedding 只算 1 次。

    调用点是 ``tasks/event_cron.py:41`` 的同步一轮:每收件人一次 = 一次 flip 拖成
    20 次 ollama 往返,人口涨一倍就翻一倍。同一事件同一描述,本来就该复用。
    """
    await _residents(db_session, 20, (0, 0))

    n = await wes.write_collective_memories(
        db_session, {"id": "sub-emb", "type": "news",
                     "description": "镇上要修一座剧院", "payload_json": {}})

    assert n == 20
    rows = await _rows(db_session, "sub-emb")
    assert len(rows) == 20
    assert all(m.embedding == _FAKE_EMB for m in rows)
    assert embed_calls == ["镇上要修一座剧院"]   # 收件人 20 位,调用 1 次


@pytest.mark.anyio
async def test_trivial_direct_write_never_calls_the_embedding_service(
        db_session, realism_on, tiered_on, embed_calls):
    """琐事档不进候选池(96% 的量是天气),给它算 embedding 是纯开销。

    这条同时钉住实现位置:算 embedding 的那行必须在 ``_write_substantive`` 里,
    不能提到 ``write_collective_memories`` 的公共段 —— 提上去天气就跟着算了。
    """
    await _residents(db_session, 20, (0, 0))

    n = await wes.write_collective_memories(
        db_session, {"id": "w-emb", "type": "weather",
                     "description": "今天多云", "payload_json": {}})

    assert n == 20
    assert embed_calls == []
    assert all(m.embedding is None for m in await _rows(db_session, "w-emb"))


@pytest.mark.anyio
async def test_no_recipient_means_no_embedding_call(
        db_session, realism_on, gradient_on, tiered_on, embed_calls):
    """梯度筛完一个人都没有时,不该白算一次。

    1 位居民 + 事件没有 ``location_id`` → geo 支为空,随机样本
    ``round(0.2 × 1) = 0`` → 收件人集合是空的,一条都不会写。与 civic 侧「整轮被
    幂等键挡掉就不算」同一条口径:外部依赖只在真要落库时才碰。
    """
    await _residents(db_session, 1, (0, 0))

    n = await wes.write_collective_memories(
        db_session, {"id": "none-emb", "type": "news",
                     "description": "镇上要修一座剧院", "payload_json": {}},
        rng=random.Random(0))

    assert n == 0
    assert await _rows(db_session, "none-emb") == []
    assert embed_calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["raises", "returns_none", "returns_empty"])
async def test_substantive_write_embedding_failure_is_fail_open(
        db_session, realism_on, tiered_on, monkeypatch, failure):
    """embedding 炸了照旧写满 20 条、``embedding is None``、返回值不变。

    ``returns_none`` 是本仓测试环境(无 ollama)的真实形状;``raises`` 守的是
    「别把异常漏进 ``_write_substantive`` 那个带 rollback 的 except」—— 漏进去
    会把已写的行 rollback 掉,返回值从 20 掉成 0。``returns_empty`` 守的是空向量
    归一:``[]`` 落库以后**两条兜底都够不着**(不满足 backfill 的
    ``embedding.is_(None)``,也不满足零向量清理的 ``if mem.embedding``),而
    ``_cosine`` 对它照样返回 0 —— 永久卡死在一个既修不了也用不上的状态。
    """
    async def _boom(text: str):
        raise RuntimeError("ollama went away")

    async def _none(text: str):
        return None

    async def _empty(text: str):
        return []

    monkeypatch.setattr(wes, "generate_embedding",
                        {"raises": _boom, "returns_none": _none,
                         "returns_empty": _empty}[failure])
    await _residents(db_session, 20, (0, 0))

    n = await wes.write_collective_memories(
        db_session, {"id": "boom-emb", "type": "news",
                     "description": "镇上要修一座剧院", "payload_json": {}})

    assert n == 20
    rows = await _rows(db_session, "boom-emb")
    assert len(rows) == 20
    assert all(m.embedding is None for m in rows)


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


# ── 直写路径的实质事件也算 embedding(E1 补漏) ─────────────────────────

@pytest.fixture
def tiered_off(monkeypatch):
    """``REALISM_EVENT_MEMORY_TIERED=false`` —— **生产的永久形状**。

    设计反转(2026-08-10)钦定这个闸永久关闭:抬 importance 会让实质事件同时挤占
    个人臂又吃专用道坑位,双重占坑。所以「实质事件走哪条写入路径」这个问题在生产
    只有一个答案 —— ``write_collective_memories`` 末尾那个直写循环。

    显式 monkeypatch 而不是靠 ``Settings`` 默认值:这几条测的就是「闸关那条路」,
    机器上的 ``backend/.env`` 若哪天写了 true,它们会在闸开那条路上恒绿。
    """
    monkeypatch.setattr(settings, "realism_event_memory_tiered", False)


@pytest.mark.anyio
async def test_direct_write_substantive_embeds_once_per_event_not_per_recipient(
        db_session, realism_on, tiered_off, embed_calls):
    """闸关(生产形状)下的实质事件也要带 embedding 落库。

    E1 那批只把 embedding 加在 ``_write_substantive``
    (``world_event_service.py:205``),而那条路**只在 ``REALISM_EVENT_MEMORY_TIERED``
    开着时才走**。闸永久关闭 → 实质事件实际走的是直写循环,落库
    ``embedding IS NULL``。

    后果不是「少一点相关度」而是**必被截断**:``_cosine`` 对 NULL 返回 0.0
    (``memory/service.py:33``),这条记忆在 ``0.45·rel + 0.30·recency +
    0.25·importance`` 里自弃 45% 的权重,得分上界 ``0.30×1 + 0.25×0.5 = 0.425``
    —— 而饱和居民第 10 名个人记忆是 0.4562。专用道把它送进候选池,scored top-10
    再把它扔掉。

    「一次算、收件人共用」与 ``_write_substantive`` 同口径:调用点
    ``tasks/event_cron.py:41`` 是同步一轮,每收件人一次 = 一次 flip 拖成 N 次
    ollama 往返,人口涨一倍就翻一倍。
    """
    await _residents(db_session, 20, (0, 0))

    n = await wes.write_collective_memories(
        db_session, {"id": "direct-emb", "type": "news",
                     "description": "镇上要修一座剧院", "payload_json": {}})

    assert n == 20
    rows = await _rows(db_session, "direct-emb")
    assert len(rows) == 20
    # 直写路 = 不走 add_memory,所以 metadata 里**没有** raw_importance、content
    # 不吃 E-28 的 80 字截断 —— 这两条钉住「补的是 embedding,没顺手换条路」
    assert all(m.importance == pytest.approx(0.5) for m in rows)
    assert all(m.metadata_json == {"first_hand": True, "event_id": "direct-emb",
                                   "tier": "substantive"} for m in rows)
    assert all(m.embedding == _FAKE_EMB for m in rows)
    assert embed_calls == ["镇上要修一座剧院"]   # 收件人 20 位,调用 1 次


@pytest.mark.anyio
@pytest.mark.parametrize("event_id,event_type,payload,description", [
    ("w-direct", "weather", {}, "今天多云"),
    ("m-direct", "festival", {"market_day": True}, "今天是集市日"),
])
async def test_direct_write_trivia_never_calls_the_embedding_service(
        db_session, realism_on, tiered_off, embed_calls,
        event_id, event_type, payload, description):
    """琐事档一次都不许算 —— 96% 的量,白算是纯浪费。

    生产 1311 条 world_event 记忆里 1253 条是天气,而琐事档**刻意**不进候选池
    (``TRIVIAL_EVENT_TYPES`` 的注释),给它算 embedding 就是每天给 ollama 加 5-6
    次零收益的往返。这条同时钉住实现位置:算 embedding 的那行必须在
    ``tier == TIER_SUBSTANTIVE`` 的判据后面,不能提到收件人算完之后的公共段 ——
    提上去天气就跟着算了。
    """
    await _residents(db_session, 20, (0, 0))

    n = await wes.write_collective_memories(
        db_session, {"id": event_id, "type": event_type,
                     "description": description, "payload_json": payload})

    assert n == 20
    rows = await _rows(db_session, event_id)
    assert len(rows) == 20
    assert all((m.metadata_json or {}).get("tier") == wes.TIER_TRIVIA for m in rows)
    assert all(m.embedding is None for m in rows)
    assert embed_calls == []


@pytest.mark.anyio
async def test_direct_write_with_no_recipient_never_calls_the_embedding_service(
        db_session, realism_on, gradient_on, tiered_off, embed_calls):
    """梯度筛完一个人都没有时不白算一次(与闸开那条路同口径)。

    1 位居民 + 事件没有 ``location_id`` → geo 支为空,随机样本 ``round(0.2×1) = 0``
    → 收件人集合是空的,一条都不会写。外部依赖只在真要落库时才碰。
    """
    await _residents(db_session, 1, (0, 0))

    n = await wes.write_collective_memories(
        db_session, {"id": "none-direct", "type": "news",
                     "description": "镇上要修一座剧院", "payload_json": {}},
        rng=random.Random(0))

    assert n == 0
    assert await _rows(db_session, "none-direct") == []
    assert embed_calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["raises", "returns_none", "returns_empty"])
async def test_direct_write_embedding_failure_is_fail_open(
        db_session, realism_on, tiered_off, monkeypatch, failure):
    """embedding 三种失败态都照旧写满 20 条、``embedding is None``、返回值不变。

    ``returns_none`` 是本仓测试环境(无 ollama)的真实形状;``raises`` 守的是
    「别让一次 embedding 抖动把整轮广播吞成 0」——直写循环外面就是
    ``event_cron.py:40-43`` 那个**没有 rollback** 的 except;``returns_empty``
    守的是空向量归一:``[]`` 灌进 PG 的 ``vector(1024)`` 会在 INSERT 当场抛,
    sqlite 下落成 ``'[]'`` 则 backfill 与零向量清理**两条兜底都够不着**,而
    ``_cosine`` 对它照样返回 0 —— 永久卡死。
    """
    async def _boom(text: str):
        raise RuntimeError("ollama went away")

    async def _none(text: str):
        return None

    async def _empty(text: str):
        return []

    monkeypatch.setattr(wes, "generate_embedding",
                        {"raises": _boom, "returns_none": _none,
                         "returns_empty": _empty}[failure])
    await _residents(db_session, 20, (0, 0))

    n = await wes.write_collective_memories(
        db_session, {"id": "direct-boom", "type": "news",
                     "description": "镇上要修一座剧院", "payload_json": {}})

    assert n == 20
    rows = await _rows(db_session, "direct-boom")
    assert len(rows) == 20
    assert all(m.embedding is None for m in rows), \
        "空向量没有归一成 None —— backfill 与零向量清理两条兜底都够不着它"
