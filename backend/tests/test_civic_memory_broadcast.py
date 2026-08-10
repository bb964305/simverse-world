"""S6 —— 镇务记忆广播通道 ``broadcast_civic_memory``。

事实层(S2-S5)管「小镇现在是什么样」,这一层管「刚才镇上发生了什么」。生产
实测:何巧云当选那天全镇只有她自己留下一条记忆,其余 13 人零条。

五条硬约束在本文件里各有断言守着:

- **收件人 = ``is_autonomous``**(K15):npc 与 UGC ``resident`` 都收,只有玩家
  分身不收。镇务公告是公共信息,把知情面绑在投票权(``is_civic_voter``,只有
  npc)上是把两个正交概念耦合起来。
- **幂等**:``metadata_json["civic_event"] = "<kind>:<ref>"``,夜间任务补跑
  (``nightly: catching up`` 真实触发过)不得重复灌记忆。
- **落档可检索(M3/K14)**:``_fetch_event_candidates`` 静态截前 30 条,进不了
  这个池子的记忆等于没写。而 ``_normalize_importance`` 是**分位数** —— 只
  spy 传入的 raw 会假绿(全窗口同为 0.9 时 raw=0.9 落 0.5),必须断言**落库值**。
  这条断言本身还有一层假绿:测试镇的 ``_HISTORY`` 池底才 0.7,而生产跑够久的
  居民池底顶到 1.0 —— 见 ``_SATURATED_HISTORY``,以及那条按
  ``REALISM_POOL_CIVIC_RESERVE`` 闸位分叉的参数化。
- **写入侧带 embedding(E1)**:``_cosine`` 对 NULL 向量返回 0.0,不带向量落库
  的记忆在检索打分里恒定丢掉 45% 的权重。且必须**每事件算一次**跨收件人复用,
  不是每人一次。
- **fail-open**:广播是镇务流程的副作用,写不进去只能记 warning,绝不能把结票
  或公告本身带崩。embedding 失败同理 —— 退回 NULL,不挡落库。
"""
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select

from app.config import settings
from app.memory.service import MemoryService
from app.models.memory import Memory
from app.models.resident import Resident
from app.services import civic_memory
from app.services.civic_memory import broadcast_civic_memory

#: 一位居民的历史事件分布(35 条,压过 ``_fetch_event_candidates`` 的 cap=30)。
#: 30 条在 0.7 以上意味着:广播若按 write_collective_memories 那样直写 0.5,
#: 或按低一档的 notice 分位落下去,都会被挤出候选池 —— 那两条断言才咬得动。
_HISTORY = [0.9] * 3 + [0.8] * 4 + [0.7] * 28

#: **饱和史** —— 生产 jiang-lin / zhao-qiwen 的形状(8355 活跃 event、36 条落库
#: 1.0、S0 候选池第 30 名恰好 **1.0**)。``_HISTORY`` 那种 0.7 池底是**测试镇独有
#: 的稀薄史**,真镇子跑够久之后池底就顶到 1.0,而那正是「进得了池」这条断言开始
#: 说谎的地方。
#:
#: ``_seed_history`` 按下标定序(下标 0 最新),所以这份列表分两段:
#:
#: - **前 100 条 = 归一化窗口**(``realism_importance_window=100``)。一条 0.95 +
#:   99 条 0.5 → 结果档 raw 0.9 落 ``(99 + 0.5×0)/100 = 0.99``,复刻生产实测的
#:   0.99-1.00。窗口里放的是普通 ``agent_action`` 的 raw(0.5 一档),因为居民的近
#:   期记忆本来就是琐事,不是高档记忆;
#: - **后 30 条 = 池底**,落库 1.0 且早于窗口 —— 它们只参与 ``importance DESC``
#:   的候选池排序,不参与归一化(超出窗口)。这两件事在生产里本来就是解耦的:
#:   落库 1.0 的那 36 条是**当年**打赢了各自窗口的老记忆,今天的窗口里全是 0.5。
#:
#: 于是 0.99 < 1.0,新镇务记忆差 **0.01** 永远排在第 31 位 —— 直到
#: ``REALISM_POOL_CIVIC_RESERVE`` 开闸,专用道绕开 importance 排序把它放进去。
_SATURATED_HISTORY = [0.95] + [0.5] * 99 + [1.0] * 30

#: 上面那份饱和史下,结果档 raw 0.9 的归一落点。写死是为了「窗口口径一动当场红」。
_NORMALIZED_UNDER_SATURATION = 0.99

#: 桩向量。宽度对齐 ``vector(1024)`` 列(sqlite 下落 JSON,但形状照生产写)。
_FAKE_EMB = [0.1] * 1024


@pytest.fixture
def broadcast_on(monkeypatch):
    """开广播总闸(S1 的六个闸门默认全关)。"""
    monkeypatch.setattr(settings, "civic_memory_broadcast_enabled", True)


async def _town(db) -> dict[str, Resident]:
    """生产名册(11 npc / 3 UGC / 5 player)的最小复刻:每类各一人。"""
    people = {
        "npc": Resident(slug="he-qiaoyun", name="何巧云", resident_type="npc"),
        "ugc": Resident(slug="bai-xing", name="白杏", resident_type="resident"),
        "player": Resident(slug="p-chen-tiesheng", name="陈铁生", resident_type="player"),
    }
    for r in people.values():
        db.add(r)
    await db.commit()
    return people


async def _civic_rows(db) -> list[Memory]:
    return list((await db.execute(
        select(Memory).where(Memory.source == "civic")
    )).scalars().all())


async def _seed_history(db, resident_id: str, values: list[float]) -> None:
    """按 raw 分布铺一段事件史。``raw_importance`` 必须写进 metadata ——
    ``_normalize_importance`` 就是从那里取值,免得归一化在自己身上复利。"""
    now = datetime.now(UTC)
    for i, raw in enumerate(values):
        mem = Memory(resident_id=resident_id, type="event", content=f"往事 {i}",
                     importance=raw, source="agent_action",
                     metadata_json={"raw_importance": raw})
        mem.created_at = now - timedelta(minutes=i + 1)
        db.add(mem)
    await db.commit()


# ── 总闸 ────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_gate_off_writes_nothing(db_session):
    await _town(db_session)
    assert await broadcast_civic_memory(
        db_session, "何巧云当选了小镇镇长。", kind="civic", ref="poll_result:p-1") == 0
    assert await _civic_rows(db_session) == []


# ── 收件人口径(K15) ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_recipients_are_autonomous_including_ugc(db_session, broadcast_on):
    """npc 与 UGC 各收 1 条,玩家分身不收。"""
    people = await _town(db_session)
    assert await broadcast_civic_memory(
        db_session, "何巧云当选了小镇镇长。", kind="civic", ref="poll_result:p-1") == 2

    rows = await _civic_rows(db_session)
    assert len(rows) == 2
    assert {m.resident_id for m in rows} == {people["npc"].id, people["ugc"].id}


@pytest.mark.anyio
async def test_exclude_resident_id_skips_that_person(db_session, broadcast_on):
    """赢家已有第一人称版本,不该再收一条第三人称的。"""
    people = await _town(db_session)
    assert await broadcast_civic_memory(
        db_session, "何巧云当选了小镇镇长。", kind="civic", ref="poll_result:p-1",
        exclude_resident_id=people["npc"].id) == 1

    rows = await _civic_rows(db_session)
    assert [m.resident_id for m in rows] == [people["ugc"].id]


# ── 幂等 ────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_same_kind_and_ref_is_idempotent(db_session, broadcast_on):
    people = await _town(db_session)
    assert await broadcast_civic_memory(
        db_session, "何巧云当选了小镇镇长。", kind="civic", ref="poll_result:p-1") == 2
    assert await broadcast_civic_memory(
        db_session, "何巧云当选了小镇镇长。", kind="civic", ref="poll_result:p-1") == 0
    assert len(await _civic_rows(db_session)) == 2

    # 另一件镇务事件(ref 不同)照写不误 —— 幂等键管的是「同一件事」,不是「同一类事」。
    assert await broadcast_civic_memory(
        db_session, "镇上要修一座剧院。", kind="civic", ref="poll_result:p-2") == 2
    rows = await _civic_rows(db_session)
    assert len(rows) == 4
    assert {m.resident_id for m in rows} == {people["npc"].id, people["ugc"].id}


@pytest.mark.anyio
async def test_idempotency_resumes_a_half_written_round(db_session, broadcast_on):
    """上一轮只写了一半(进程被打断):补跑必须补齐余下的人,而不是整轮跳过。"""
    people = await _town(db_session)
    await broadcast_civic_memory(
        db_session, "何巧云当选了小镇镇长。", kind="civic", ref="poll_result:p-1",
        exclude_resident_id=people["ugc"].id)

    assert await broadcast_civic_memory(
        db_session, "何巧云当选了小镇镇长。", kind="civic", ref="poll_result:p-1") == 1
    assert len(await _civic_rows(db_session)) == 2


# ── 落库形状 ────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_landed_row_carries_civic_event_and_source(db_session, broadcast_on):
    people = await _town(db_session)
    await broadcast_civic_memory(
        db_session, "何巧云当选了小镇镇长。", kind="civic", ref="poll_result:p-1")

    mem = next(m for m in await _civic_rows(db_session)
               if m.resident_id == people["npc"].id)
    assert mem.type == "event"
    assert mem.source == "civic"
    assert mem.content == "何巧云当选了小镇镇长。"
    assert mem.metadata_json["civic_event"] == "civic:poll_result:p-1"


@pytest.mark.anyio
async def test_importance_defaults_to_gate_value_and_is_overridable(db_session, broadcast_on):
    """缺省 = ``civic_memory_importance``(结果类);征询/日常公告由调用方显式降档。"""
    await _town(db_session)
    await broadcast_civic_memory(
        db_session, "镇上正在议一件事。", kind="civic", ref="poll_open:p-1",
        importance=settings.civic_memory_notice_importance)
    await broadcast_civic_memory(
        db_session, "何巧云当选了小镇镇长。", kind="civic", ref="poll_result:p-1")

    by_event = {m.metadata_json["civic_event"]: m for m in await _civic_rows(db_session)}
    assert by_event["civic:poll_open:p-1"].importance == pytest.approx(
        settings.civic_memory_notice_importance)
    assert by_event["civic:poll_result:p-1"].importance == pytest.approx(
        settings.civic_memory_importance)


# ── 落档可检索(M3/K14) ─────────────────────────────────────────────────

@pytest.mark.anyio
@pytest.mark.parametrize("history,normalized,reserve,pool_floor,seated", [
    # 稀薄史:窗口就是那 35 条,raw 0.9 的 below=32 / equal=3 → (32+1.5)/35 = 0.9571,
    # 池底 0.7(第 30 名)。0.9571 > 0.7,靠 importance 就进得去 —— 闸开闸关都一样。
    pytest.param(_HISTORY, 0.9571, 0, 0.7, True, id="thin-closed"),
    pytest.param(_HISTORY, 0.9571, 2, 0.7, True, id="thin-open"),
    # 饱和史 + 闸关:窗口 below=99 / equal=0 → 0.99,池底 1.0。**差 0.01 进不去**。
    # 这一格就是被修缺陷本身的复现,现在是正向断言(seated=False)而不再是 xfail。
    pytest.param(_SATURATED_HISTORY, _NORMALIZED_UNDER_SATURATION, 0, 1.0, False,
                 id="saturated-closed"),
    # 饱和史 + 闸开:专用道绕开 importance 排序把它放进来。**这一格是本批的核心
    # 验收** —— 它证明的正是 jiang-lin / zhao-qiwen 那 0.01。池底随之变成 0.99,
    # 也就是这条镇务记忆自己:它坐在池尾,说明它是被留位放进来的,不是靠压过谁。
    pytest.param(_SATURATED_HISTORY, _NORMALIZED_UNDER_SATURATION, 2,
                 _NORMALIZED_UNDER_SATURATION, True, id="saturated-open"),
])
async def test_broadcast_survives_the_top30_candidate_pool(
        db_session, broadcast_on, monkeypatch, history, normalized, reserve,
        pool_floor, seated):
    """生产 ``REALISM_ENABLED=true``,写进去的 0.9 会被分位数改写。断言**落库值**
    仍在最高档,且该行**是否**出现在 cap=30 的候选池里逐格写死 —— 进不了池子
    等于没写。

    两份历史 × 两个闸位:

    - ``thin``(``_HISTORY``,池底 0.7)是**测试镇**的形状 —— 两个闸位都绿,这正是
      保留位「对已经进得去的人不改变任何东西」的兑现;
    - ``saturated``(``_SATURATED_HISTORY``,池底 1.0)是**真镇子跑够久之后**的形状。
      生产实测 4 位居民里 jiang-lin(8355 event)与 zhao-qiwen(8042)的 S0 池第 30
      名恰好是 1.0,而结果档 raw 0.9 只归一到 0.99 —— 差 0.01,永远排在第 31 位。

    换句话说:只有 ``thin`` 那一份时,「镇务记忆进得了池」这条断言**在测试里恒绿、
    在生产对 2/4 人是假的**。任何池方案的验收不先补这条饱和分支都是假绿。

    **为什么摘掉了 ``xfail(strict=True)``**(而不是删用例):那个 mark 钉的是「差
    0.01 进不去」,``REALISM_POOL_CIVIC_RESERVE`` 落地后 ``saturated`` 该转绿,
    strict 会让它以 XPASS 失败 —— 这正是它当初存在的目的。但它还有一层更隐蔽的
    毛病:开闸后这一格其实是**红在池底断言上**(0.99 != 1.0)而不是红在「进不去」
    上,xfail 照样把它算作预期失败 —— 缺陷已经被修好了,哨兵却还在安静地报「符合
    预期」。所以正确的收尾是把闸位提成参数:闸关那格继续用正向断言复现缺陷,闸开
    那格断言转绿,两格都不再有任何 mark 兜底。
    """
    monkeypatch.setattr(settings, "realism_enabled", True)
    monkeypatch.setattr(settings, "realism_pool_civic_reserve", reserve)
    people = await _town(db_session)
    rid = people["npc"].id
    await _seed_history(db_session, rid, history)

    assert await broadcast_civic_memory(
        db_session, "何巧云当选了小镇镇长。", kind="civic", ref="poll_result:p-1") == 2

    mem = next(m for m in await _civic_rows(db_session) if m.resident_id == rid)
    assert mem.metadata_json["raw_importance"] == pytest.approx(0.9)  # raw 留痕
    assert mem.importance >= 0.8                                      # 落库值才算数
    assert mem.importance == pytest.approx(normalized)                # 归一落点写死
    pool = await MemoryService(db_session)._fetch_event_candidates(rid, cap=30)
    # 保留位**不扩池**:四格都必须是 30 条,没填满的坑退还给个人臂。
    assert len(pool) == 30
    assert len({m.id for m in pool}) == 30                            # 且零重复
    # 池底一并咬死 —— 少了它,「进得去」可能是归一化算错(把它抬过了池底)换来的,
    # 那是另一个 bug 冒充修好了这个。
    assert pool[-1].importance == pytest.approx(pool_floor)
    assert (mem.id in {m.id for m in pool}) is seated, (
        f"reserve={reserve} 时镇务记忆{'没能' if seated else '竟然'}进池"
        f"(归一 {mem.importance} / 池底 {pool[-1].importance})")


# ── 写入侧 embedding(E1) ───────────────────────────────────────────────

@pytest.fixture
def embed_calls(monkeypatch):
    """把写入侧的 ``generate_embedding`` 换成计数桩,返回「被拿去算的文本」列表。

    测试环境没有 ollama:真调用会连接失败,而 ``generate_embeddings_batch`` 自己
    把异常吞成 ``None``(``memory/embedding.py:133-135``)—— 那恰好是 fail-open
    的形状。所以「落库非空」与「只算一次」这两条都只能靠桩来钉,不能靠真调用。
    """
    calls: list[str] = []

    async def _fake(text: str):
        calls.append(text)
        return list(_FAKE_EMB)

    monkeypatch.setattr(civic_memory, "generate_embedding", _fake)
    return calls


@pytest.mark.anyio
async def test_broadcast_embeds_the_content_for_every_recipient(
        db_session, broadcast_on, embed_calls):
    """相关度那 0.45 分的前提:落库行的 ``embedding`` 不能是 NULL。

    ``_cosine``(``memory/service.py:33-34``)第一行就 ``if not a or not b:
    return 0.0`` —— NULL 等于这条记忆恒定放弃打分里 45% 的权重,拿 0.55 去打
    别人 1.0 的仗。
    """
    await _town(db_session)
    assert await broadcast_civic_memory(
        db_session, "何巧云当选了小镇镇长。", kind="civic", ref="poll_result:p-1") == 2

    rows = await _civic_rows(db_session)
    assert len(rows) == 2
    assert all(m.embedding == _FAKE_EMB for m in rows)


@pytest.mark.anyio
async def test_embedding_is_computed_once_per_event_not_once_per_recipient(
        db_session, broadcast_on, embed_calls):
    """生产 14 位收件人收的是**同一段 content** —— embedding 只该算一次。

    每人一次 = 每条镇务公告 14 次 ollama 往返,而且全挂在结票/公告的同步路径上。
    这条断言的数字是 **1,不是收件人数**。
    """
    await _town(db_session)
    assert await broadcast_civic_memory(
        db_session, "何巧云当选了小镇镇长。", kind="civic", ref="poll_result:p-1") == 2

    assert embed_calls == ["何巧云当选了小镇镇长。"]
    # 同一个向量跨收件人复用,不是各算各的。
    assert len({tuple(m.embedding) for m in await _civic_rows(db_session)}) == 1


@pytest.mark.anyio
async def test_idempotent_rerun_does_not_recompute_the_embedding(
        db_session, broadcast_on, embed_calls):
    """整轮被幂等键挡掉时一次都不该算 —— ``nightly: catching up`` 是天天跑的。"""
    await _town(db_session)
    assert await broadcast_civic_memory(
        db_session, "何巧云当选了小镇镇长。", kind="civic", ref="poll_result:p-1") == 2
    assert len(embed_calls) == 1

    assert await broadcast_civic_memory(
        db_session, "何巧云当选了小镇镇长。", kind="civic", ref="poll_result:p-1") == 0
    assert len(embed_calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["raises", "returns_none"])
async def test_embedding_failure_is_fail_open(
        db_session, broadcast_on, monkeypatch, failure):
    """embedding 服务抖动绝不能挡住镇务记忆落库:照旧写、``embedding is None``、
    返回条数不变(= 改前的行为)。``returns_none`` 就是本仓测试环境的真实形状。"""
    async def _boom(text: str):
        raise RuntimeError("ollama went away")

    async def _none(text: str):
        return None

    monkeypatch.setattr(civic_memory, "generate_embedding",
                        _boom if failure == "raises" else _none)

    await _town(db_session)
    assert await broadcast_civic_memory(
        db_session, "何巧云当选了小镇镇长。", kind="civic", ref="poll_result:p-1") == 2

    rows = await _civic_rows(db_session)
    assert len(rows) == 2
    assert all(m.embedding is None for m in rows)


# ── fail-open ───────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_write_failure_returns_partial_count_without_raising(db_session, broadcast_on, monkeypatch):
    """写到一半炸了:返回已写条数,不把结票/公告的调用链带崩。"""
    await _town(db_session)
    real_add = MemoryService.add_memory
    calls = {"n": 0}

    async def _flaky(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("db went away")
        return await real_add(self, *args, **kwargs)

    monkeypatch.setattr(MemoryService, "add_memory", _flaky)
    assert await broadcast_civic_memory(
        db_session, "何巧云当选了小镇镇长。", kind="civic", ref="poll_result:p-1") == 1
    assert len(await _civic_rows(db_session)) == 1
