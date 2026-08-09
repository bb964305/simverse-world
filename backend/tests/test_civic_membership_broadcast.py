"""S9 —— 人口变动广播(B4:必须在事务提交之后)。

镇上多一位公民、少一位公民,是所有人都会听说的事;今天两个写入口只发一条易失
的 WS 事件(``_emit_standing_changed``,不落任何表),没有人的脑子里留下痕迹。
这一步把它接上 S6 的广播通道。

**位置是本步唯一真正的硬约束**:``_write_history`` 返回 ``None`` 且不 commit
(「由调用方决定事务边界」),而 ``add_memory`` 自带 commit(K16)。广播只要往前
挪进步骤 5-6 之间,就会在 ``_assert_demotion_invariants`` 之前把复合事务劈成
两半 —— 断言失败时前半段已经落地,再也整体回滚不掉。所以本文件里最重的一条
断言不是「广播写了几条」,而是**不变量抛异常时 memories 零新增**。
"""
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models.civic_standing_history import CivicStandingHistory
from app.models.memory import Memory
from app.models.resident import Resident
from app.services import civic_membership as cm


def _res(slug, rtype, *, name=None, creator_id="u1", meta=None):
    return Resident(slug=slug, name=name or slug, district="town_hall",
                    status="idle", resident_type=rtype, creator_id=creator_id,
                    tile_x=1, tile_y=1, meta_json=meta)


async def _seed_citizen(db, slug="ugc-1", *, name="白杏"):
    """一位「已归化 + 有晋升记录」的公民,外加 6 位内置公民撑住选民下限
    (同 ``tests/test_civic_revoke_citizenship.py`` 的 ``_seed_citizen``)。
    再加一位玩家分身:收件人口径 K15 要在两个写入口上都成立。"""
    db.add_all([_res(f"builtin-{i}", cm.CIVIC_MEMBER_TYPE,
                     creator_id=cm.SYSTEM_CREATOR_ID) for i in range(6)])
    db.add(_res("p-chen-tiesheng", cm.PLAYER_RESIDENT_TYPE, name="陈铁生"))
    r = _res(slug, cm.CIVIC_MEMBER_TYPE, name=name, meta={"origin": "forge"})
    db.add(r)
    await db.commit()
    db.add(CivicStandingHistory(
        resident_id=r.id, old_standing=cm.DENIZEN, new_standing=cm.CITIZEN,
        reason=None, reason_code="threshold_met", actor="civic_promotion",
        evidence_json={}, world_at=datetime.now(UTC)))
    await db.commit()
    return r


async def _seed_denizen(db, slug="ugc-1", *, name="白杏"):
    """一位待晋升的 denizen + 两位内置公民 + 一位玩家分身。"""
    db.add_all([_res(f"builtin-{i}", cm.CIVIC_MEMBER_TYPE,
                     creator_id=cm.SYSTEM_CREATOR_ID) for i in range(2)])
    db.add(_res("p-chen-tiesheng", cm.PLAYER_RESIDENT_TYPE, name="陈铁生"))
    r = _res(slug, cm.UGC_RESIDENT_TYPE, name=name, meta={"origin": "forge"})
    db.add(r)
    await db.commit()
    return r


async def _civic_rows(db) -> list[Memory]:
    return list((await db.execute(
        select(Memory).where(Memory.source == "civic")
    )).scalars().all())


async def _autonomous_ids(db) -> set[str]:
    return set((await db.execute(
        select(Resident.id).where(Resident.is_autonomous))).scalars().all())


@pytest.fixture
def _no_ws():
    with patch("app.lab.apply.broadcast_world_changed", new=AsyncMock()) as m:
        yield m


@pytest.fixture
def broadcast_on(monkeypatch):
    """开广播总闸(S1 的六个闸门默认全关)。"""
    monkeypatch.setattr(settings, "civic_memory_broadcast_enabled", True)


# ── 两个写入口各广播一轮 ───────────────────────────────────────────────

@pytest.mark.anyio
async def test_grant_broadcasts_to_every_autonomous_resident(
        db_session, _no_ws, broadcast_on):
    """晋升:全镇各留一条,新公民自己也在收件人里(他没有第一人称版本)。"""
    r = await _seed_denizen(db_session)
    assert await cm.grant_citizenship(
        db_session, r, reason="满足门槛", actor="civic_promotion",
        reason_code="threshold_met") is True

    rows = await _civic_rows(db_session)
    assert {m.resident_id for m in rows} == await _autonomous_ids(db_session)
    assert len(rows) == 3, "2 位内置 + 新公民本人;玩家分身不收(K15)"
    mem = rows[0]
    assert mem.type == "event"
    assert mem.metadata_json["civic_event"] == (
        f"civic_standing:{r.id}:{cm.CITIZEN}:threshold_met")
    assert "白杏" in mem.content, "记忆里说人名,不说 slug"
    assert r.slug not in mem.content


@pytest.mark.anyio
async def test_revoke_broadcasts_to_every_autonomous_resident(
        db_session, _no_ws, broadcast_on):
    """撤销:被降档的人仍在世界人口里,所以他自己也收得到。"""
    r = await _seed_citizen(db_session)
    assert await cm.revoke_citizenship(
        db_session, r, reason="秘密理由", actor="admin:1",
        reason_code="admin_revoke") is True

    rows = await _civic_rows(db_session)
    assert {m.resident_id for m in rows} == await _autonomous_ids(db_session)
    assert len(rows) == 7, "6 位内置 + 被降档者本人;玩家分身不收(K15)"
    mem = rows[0]
    assert mem.metadata_json["civic_event"] == (
        f"civic_standing:{r.id}:{cm.DENIZEN}:admin_revoke")
    assert "白杏" in mem.content
    assert "秘密理由" not in mem.content, (
        "reason 自由文本永不外发 —— 与 _emit_standing_changed 的 payload 同一条纪律")


@pytest.mark.anyio
async def test_gate_off_writes_nothing_on_either_entrypoint(db_session, _no_ws):
    """闸关时两个写入口逐字节回到今天:零记忆、零查询。"""
    r = await _seed_denizen(db_session)
    await cm.grant_citizenship(db_session, r, reason="x", actor="admin:1")
    await db_session.refresh(r)
    db_session.add_all([_res(f"pad-{i}", cm.CIVIC_MEMBER_TYPE,
                             creator_id=cm.SYSTEM_CREATOR_ID) for i in range(4)])
    await db_session.commit()
    await cm.revoke_citizenship(db_session, r, reason="x", actor="admin:1")

    assert await _civic_rows(db_session) == []


# ── B4:广播不得进入复合事务的断言窗口 ─────────────────────────────────

@pytest.mark.anyio
async def test_failed_demotion_invariant_leaves_zero_memories(
        db_session, _no_ws, broadcast_on):
    """步骤 6 的自查抛异常 → 整体回滚,memories 必须零新增。

    这条是 B4 的可证伪形态:把广播挪到步骤 5-6 之间(``_write_history`` 之后、
    ``_assert_demotion_invariants`` 之前),``add_memory`` 自带的 commit 会先把
    半截事务落地,这里的两条断言同时红 —— 记忆写进去了,档位却回滚了。

    ``rid`` 必须在调用**之前**取:步骤 6 那条失败路径走顶层 ``db.rollback()``,
    会 expire 整个 identity map,事后读 ``r.id`` 是一次隐式 lazy-reload,在没有
    greenlet 上下文的地方直接炸 ``MissingGreenlet``(revoke_citizenship 的
    docstring「异常路径调用方契约」写的就是这条)。

    ⚠️ 这里断言的是**步骤 5 的历史行被回滚**,不是「档位翻回 citizen」——
    pysqlite 的 legacy 事务行为在 ``SAVEPOINT`` 之前会隐式 COMMIT,所以步骤
    1-4 那个 ``begin_nested()`` 里的档位翻转在 sqlite 上**扛得过**顶层
    ``db.rollback()``(实测:回滚后仍是 ``resident``,而 savepoint 之后写的行
    确实没了)。这是测试库的性质,不是被测代码的性质,写在这里免得后人把它
    当成 bug 去"修"。真 PG 上整段都会回滚。
    """
    r = await _seed_citizen(db_session)
    rid = r.id
    boom = AsyncMock(side_effect=RuntimeError("invariant broken"))
    with patch.object(cm, "_assert_demotion_invariants", new=boom):
        with pytest.raises(RuntimeError):
            await cm.revoke_citizenship(db_session, r, reason="x",
                                        actor="admin:1")

    assert await _civic_rows(db_session) == []
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory)
        .where(CivicStandingHistory.resident_id == rid,
               CivicStandingHistory.new_standing == cm.DENIZEN))).scalar() == 0, (
        "步骤 5 的历史行必须随回滚一起消失 —— 广播若抢在它前面落地,就是"
        "「没人降档,全镇却都记得他降了档」")


@pytest.mark.anyio
async def test_refused_grant_writes_no_memory(db_session, _no_ws, broadcast_on):
    """防呆拒绝是真正的 no-op:没发生的事不该有人记得。"""
    db_session.add(_res("npc-1", cm.CIVIC_MEMBER_TYPE,
                        creator_id=cm.SYSTEM_CREATOR_ID))
    await db_session.commit()
    already = (await db_session.execute(
        select(Resident).where(Resident.slug == "npc-1"))).scalar_one()

    with pytest.raises(cm.CivicStandingRefused):
        await cm.grant_citizenship(db_session, already, reason="x",
                                   actor="admin:1")
    assert await _civic_rows(db_session) == []


# ── fail-open ───────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_broadcast_failure_never_breaks_the_write_entrypoint(
        db_session, _no_ws, broadcast_on):
    """广播是副作用:写不进去只记 warning,档位与历史行照样落地。"""
    r = await _seed_citizen(db_session)
    with patch("app.services.civic_memory.broadcast_civic_memory",
               new=AsyncMock(side_effect=RuntimeError("db went away"))):
        assert await cm.revoke_citizenship(db_session, r, reason="x",
                                           actor="admin:1") is True

    rtype = (await db_session.execute(
        select(Resident.resident_type).where(Resident.id == r.id))).scalar_one()
    assert rtype == cm.UGC_RESIDENT_TYPE
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory)
        .where(CivicStandingHistory.new_standing == cm.DENIZEN))).scalar() == 1
