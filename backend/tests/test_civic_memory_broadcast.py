"""S6 —— 镇务记忆广播通道 ``broadcast_civic_memory``。

事实层(S2-S5)管「小镇现在是什么样」,这一层管「刚才镇上发生了什么」。生产
实测:何巧云当选那天全镇只有她自己留下一条记忆,其余 13 人零条。

四条硬约束在本文件里各有断言守着:

- **收件人 = ``is_autonomous``**(K15):npc 与 UGC ``resident`` 都收,只有玩家
  分身不收。镇务公告是公共信息,把知情面绑在投票权(``is_civic_voter``,只有
  npc)上是把两个正交概念耦合起来。
- **幂等**:``metadata_json["civic_event"] = "<kind>:<ref>"``,夜间任务补跑
  (``nightly: catching up`` 真实触发过)不得重复灌记忆。
- **落档可检索(M3/K14)**:``_fetch_event_candidates`` 静态截前 30 条,进不了
  这个池子的记忆等于没写。而 ``_normalize_importance`` 是**分位数** —— 只
  spy 传入的 raw 会假绿(全窗口同为 0.9 时 raw=0.9 落 0.5),必须断言**落库值**。
- **fail-open**:广播是镇务流程的副作用,写不进去只能记 warning,绝不能把结票
  或公告本身带崩。
"""
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select

from app.config import settings
from app.memory.service import MemoryService
from app.models.memory import Memory
from app.models.resident import Resident
from app.services.civic_memory import broadcast_civic_memory

#: 一位居民的历史事件分布(35 条,压过 ``_fetch_event_candidates`` 的 cap=30)。
#: 30 条在 0.7 以上意味着:广播若按 write_collective_memories 那样直写 0.5,
#: 或按低一档的 notice 分位落下去,都会被挤出候选池 —— 那两条断言才咬得动。
_HISTORY = [0.9] * 3 + [0.8] * 4 + [0.7] * 28


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
async def test_broadcast_survives_the_top30_candidate_pool(db_session, broadcast_on, monkeypatch):
    """生产 ``REALISM_ENABLED=true``,写进去的 0.9 会被分位数改写。断言**落库值**
    仍在最高档,且该行确实出现在 cap=30 的候选池里 —— 进不了池子等于没写。"""
    monkeypatch.setattr(settings, "realism_enabled", True)
    people = await _town(db_session)
    rid = people["npc"].id
    await _seed_history(db_session, rid, _HISTORY)

    assert await broadcast_civic_memory(
        db_session, "何巧云当选了小镇镇长。", kind="civic", ref="poll_result:p-1") == 2

    mem = next(m for m in await _civic_rows(db_session) if m.resident_id == rid)
    assert mem.metadata_json["raw_importance"] == pytest.approx(0.9)  # raw 留痕
    assert mem.importance >= 0.8                                      # 落库值才算数
    pool = await MemoryService(db_session)._fetch_event_candidates(rid, cap=30)
    assert len(pool) == 30                                            # 池子确实满了
    assert mem.id in {m.id for m in pool}


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
