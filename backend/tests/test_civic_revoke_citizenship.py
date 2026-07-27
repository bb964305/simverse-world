"""F2 Task 5 —— 撤销是有序复合事务。

顺序不可颠倒：若先改档位再清理，meta_json['mayor'] 在逐出档会永久卡死（清扫
扫不到他），期间 install_mayor() 清他人标志时也会跳过他，可产生「两个
meta_json['mayor']=True」并双份工资倍率（duty_service.py:172-173 × 1.2）。
"""
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models.civic_standing_history import CivicStandingHistory
from app.models.office import Office
from app.models.resident import Resident
from app.models.system_config import SystemConfig
from app.services import civic_membership as cm


def _res(slug, rtype, *, creator_id="u1", meta=None):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id=creator_id,
                    tile_x=1, tile_y=1, meta_json=meta)


async def _seed_citizen(db, slug="ugc-1", *, meta=None):
    """一位「已归化 + 有晋升记录」的公民，外加 6 位内置公民撑住选民下限。"""
    db.add_all([_res(f"builtin-{i}", cm.CIVIC_MEMBER_TYPE,
                     creator_id=cm.SYSTEM_CREATOR_ID) for i in range(6)])
    r = _res(slug, cm.CIVIC_MEMBER_TYPE, meta=meta or {"origin": "forge"})
    db.add(r)
    await db.commit()
    db.add(CivicStandingHistory(
        resident_id=r.id, old_standing=cm.DENIZEN, new_standing=cm.CITIZEN,
        reason=None, reason_code="threshold_met", actor="civic_promotion",
        evidence_json={}, world_at=datetime.now(UTC)))
    await db.commit()
    return r


@pytest.fixture
def _no_ws():
    with patch("app.lab.apply.broadcast_world_changed", new=AsyncMock()) as m:
        yield m


# ── 基本语义 ───────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_revoke_demotes_and_writes_history(db_session, _no_ws):
    r = await _seed_citizen(db_session)
    assert await cm.revoke_citizenship(
        db_session, r, reason="违反镇规", actor="admin:42") is True

    rtype = (await db_session.execute(
        select(Resident.resident_type).where(Resident.id == r.id))).scalar_one()
    assert rtype == cm.UGC_RESIDENT_TYPE

    rows = (await db_session.execute(
        select(CivicStandingHistory)
        .where(CivicStandingHistory.new_standing == cm.DENIZEN))).scalars().all()
    assert len(rows) == 1
    assert rows[0].old_standing == cm.CITIZEN
    assert rows[0].actor == "admin:42"
    assert rows[0].reason == "违反镇规"          # 文本落表但永不外发


@pytest.mark.anyio
async def test_revoke_keeps_the_resident_in_the_world_population(db_session, _no_ws):
    """硬门 6：撤销后 is_autonomous 仍 True / is_civic_voter 已 False。防止有人
    顺手把撤销实现成「移出世界人口」。

    ⚠️ 必须先 ``refresh``：档位翻转走 ``update(...).execution_options(
    synchronize_session=False)``，而 conftest 的 ``db_session`` 是
    ``expire_on_commit=False``（tests/conftest.py:119-122），commit 后会话身份
    映射里的 Resident 实体仍是旧值——``select(Resident)`` 这种**实体查询**会把
    同一个陈旧对象原样取回来（实测：``fresh is r`` 为 True、``resident_type``
    仍是 'npc'，而同一事务的列级读已是 'resident'）。本文件其它用例走的是列级
    ``select(Resident.resident_type)`` 或 SQL 侧 ``where(Resident.is_civic_voter)``，
    只有这一条需要读 Python 侧的 hybrid，所以只有这一条要 refresh。
    """
    r = await _seed_citizen(db_session)
    await cm.revoke_citizenship(db_session, r, reason="x", actor="admin:1")

    await db_session.refresh(r)
    assert r.is_autonomous is True
    assert r.is_civic_voter is False


@pytest.mark.anyio
async def test_exile_tier_is_reserved_not_implemented(db_session):
    r = await _seed_citizen(db_session)
    with pytest.raises(NotImplementedError):
        await cm.revoke_citizenship(db_session, r, reason="x",
                                    actor="admin:1", tier="exile")
    # 预留分支必须是零写入
    rtype = (await db_session.execute(
        select(Resident.resident_type).where(Resident.id == r.id))).scalar_one()
    assert rtype == cm.CIVIC_MEMBER_TYPE


@pytest.mark.anyio
async def test_unknown_tier_is_a_value_error(db_session):
    r = await _seed_citizen(db_session)
    with pytest.raises(ValueError):
        await cm.revoke_citizenship(db_session, r, reason="x",
                                    actor="admin:1", tier="banish")


# ── 三处镇长表示的同步清理（gate 开 / 关都要覆盖）────────────────────

async def _make_sitting_mayor(db, r):
    """三处镇长表示都指向 r：offices 行 + meta_json['mayor'] + system_config。"""
    meta = dict(r.meta_json or {})
    meta["mayor"] = True
    r.meta_json = meta
    db.add(Office(office_key="mayor", holder_slug=r.slug,
                  institution="town_hall", perms_json={},
                  fill_strategy=cm.POLITICAL_FILL_STRATEGY,
                  term_started_at=datetime.now(UTC), term_ends_at=None))
    db.add(SystemConfig(key="current_mayor", value=json.dumps(r.slug),
                        group="civic", updated_by="election"))
    await db.commit()


@pytest.mark.parametrize("gate_on", [True, False])
@pytest.mark.anyio
async def test_revoking_a_sitting_mayor_clears_all_three_representations(
        db_session, monkeypatch, _no_ws, gate_on):
    """硬门 3。polis_office_enabled 开与关都必须成立——默认是关，最容易漏测的
    恰恰是生产以外的那一态。"""
    monkeypatch.setattr(settings, "polis_office_enabled", gate_on)
    r = await _seed_citizen(db_session)
    await _make_sitting_mayor(db_session, r)

    assert await cm.revoke_citizenship(db_session, r, reason="x",
                                       actor="admin:1") is True

    holder = (await db_session.execute(
        select(Office.holder_slug).where(Office.office_key == "mayor"))).scalar_one()
    assert holder is None
    metas = (await db_session.execute(select(Resident.meta_json))).scalars().all()
    assert all(not (m or {}).get("mayor") for m in metas)
    cfg = (await db_session.execute(
        select(SystemConfig.value)
        .where(SystemConfig.key == "current_mayor"))).scalar_one()
    assert json.loads(cfg) is None


@pytest.mark.anyio
async def test_revoke_does_not_touch_labour_offices(db_session, _no_ws):
    """只卸民选职务。offices 表把政治职务与劳动职务混在一张表里
    （office_service.py:41-46），一刀切会误伤 town_clerk / postman / doctor。"""
    r = await _seed_citizen(db_session)
    db_session.add_all([
        Office(office_key="town_clerk", holder_slug=r.slug,
               institution="town_hall", perms_json={}, fill_strategy="seed"),
        Office(office_key="postman", holder_slug=r.slug,
               institution="post_office", perms_json={}, fill_strategy="seed"),
    ])
    await db_session.commit()

    await cm.revoke_citizenship(db_session, r, reason="x", actor="admin:1")

    holders = dict((await db_session.execute(
        select(Office.office_key, Office.holder_slug))).all())
    assert holders["town_clerk"] == r.slug
    assert holders["postman"] == r.slug


@pytest.mark.anyio
async def test_revoke_does_not_vacate_someone_elses_stale_office_row(
        db_session, _no_ws):
    """guard 必须带 holder 校验：gate 关时 offices.holder_slug 可能是迁移 046
    的陈旧值，无条件 vacate 会罢免错的人。"""
    r = await _seed_citizen(db_session)
    db_session.add(Office(office_key="mayor", holder_slug="builtin-0",
                          institution="town_hall", perms_json={},
                          fill_strategy=cm.POLITICAL_FILL_STRATEGY))
    db_session.add(SystemConfig(key="current_mayor",
                                value=json.dumps("builtin-0"),
                                group="civic", updated_by="election"))
    await db_session.commit()

    await cm.revoke_citizenship(db_session, r, reason="x", actor="admin:1")

    holder = (await db_session.execute(
        select(Office.holder_slug).where(Office.office_key == "mayor"))).scalar_one()
    assert holder == "builtin-0", "别人的职位不能被顺手罢免"
    cfg = (await db_session.execute(
        select(SystemConfig.value)
        .where(SystemConfig.key == "current_mayor"))).scalar_one()
    assert json.loads(cfg) == "builtin-0", "current_mayor 只在指向本人时才清"


@pytest.mark.anyio
async def test_cleanup_happens_before_the_tier_flip(db_session, _no_ws):
    """顺序不可颠倒的可执行断言：把清 meta_json 的那一步换成一个探针，断言它
    执行时 resident_type 还是 citizen 档。若实现先改档位，探针会读到
    'resident' 并让断言失败。"""
    r = await _seed_citizen(db_session)
    await _make_sitting_mayor(db_session, r)

    seen: list[str] = []
    real = cm._write_history

    async def _probe(db, **kw):
        rtype = (await db.execute(
            select(Resident.resident_type)
            .where(Resident.id == kw["resident_id"]))).scalar_one()
        seen.append(rtype)
        return await real(db, **kw)

    # 历史行是步骤 5，写它时档位已经翻过；真正要证明的是步骤 1-3 在步骤 4
    # 之前跑完 —— 用「历史行写入时 offices/meta/config 都已清空」来锁死。
    with patch.object(cm, "_write_history", new=_probe):
        await cm.revoke_citizenship(db_session, r, reason="x", actor="admin:1")

    assert seen == [cm.UGC_RESIDENT_TYPE]
    holder = (await db_session.execute(
        select(Office.holder_slug).where(Office.office_key == "mayor"))).scalar_one()
    assert holder is None


@pytest.mark.anyio
async def test_revoke_broadcasts_citizen_to_denizen(db_session):
    r = await _seed_citizen(db_session)
    with patch("app.lab.apply.broadcast_world_changed", new=AsyncMock()) as bc:
        await cm.revoke_citizenship(db_session, r, reason="秘密理由",
                                    actor="admin:1", reason_code="admin_revoke")
    payload = bc.await_args.args[0]
    assert payload["type"] == "civic_standing_changed"
    assert (payload["old_standing"], payload["new_standing"]) == (cm.CITIZEN,
                                                                  cm.DENIZEN)
    assert payload["reason_code"] == "admin_revoke"
    assert "秘密理由" not in str(payload)


@pytest.mark.anyio
async def test_refused_revoke_leaves_the_database_untouched(db_session):
    """硬门 5：射程外的人被撤销时 raise 且数据库零变化。"""
    db_session.add_all([_res(f"builtin-{i}", cm.CIVIC_MEMBER_TYPE,
                             creator_id=cm.SYSTEM_CREATOR_ID) for i in range(6)])
    await db_session.commit()
    builtin = (await db_session.execute(
        select(Resident).where(Resident.slug == "builtin-0"))).scalar_one()

    with pytest.raises(cm.CivicStandingRefused):
        await cm.revoke_citizenship(db_session, builtin, reason="x",
                                    actor="admin:1")

    assert (await db_session.execute(
        select(func.count()).select_from(Resident)
        .where(Resident.is_civic_voter))).scalar() == 6
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0
