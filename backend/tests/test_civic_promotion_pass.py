"""F2 Task 12 —— 晋升 pass 的三态与四道数值闸门。

shadow 的准确定位：首夜爆炸半径不可预演，它是**带全部防呆的实跑演练 + 名单
落盘**，不是「规模在开闸前无人知晓」（只读标定本来就能测出候选规模）。

结构性收口（Task 6 评审硬要求，逐字引用）：「``select_promotions`` is only a
DECISION function; the actual DB write happens when Task 12 calls
``civic_membership.grant_citizenship_batch``. The gate therefore blocks
"calling ``select_promotions(mode=on)``", NOT "any write". Nothing
structurally forces Task 12 through the gate at all. FIX SHAPE: give Task 12
a dedicated entry point with ``mode='on'`` hardcoded (e.g.
``select_promotions_for_write(...)``) rather than letting it reuse the
shadow-defaulting signature.」—— 本文件「写路径的结构性收口」一节用 spy /
桩两种手法直接证明这个洞已经补上，而不是只靠 DB 状态断言侧面印证。
"""
import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models.civic_standing_history import CivicStandingHistory
from app.models.resident import Resident
from app.models.resident_relation import ResidentRelation
from app.services import civic_membership as cm
from app.services.config_service import ConfigService
from app.tasks import civic_promotion as cp

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _res(slug, rtype, *, creator_id="u1", meta=None, days=200):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id=creator_id, tile_x=1,
                    tile_y=1, meta_json=meta,
                    created_at=datetime.now(UTC) - timedelta(days=days))


async def _world(db, *, builtins=4, denizens=1, edges_per=2):
    """一个「全员达标」的小世界：denizens 与前 edges_per 位内置公民都够熟。"""
    bs = [_res(f"b{i}", cm.CIVIC_MEMBER_TYPE, creator_id=cm.SYSTEM_CREATOR_ID,
               meta={"origin": "preset"}) for i in range(builtins)]
    us = [_res(f"u{i}", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"})
          for i in range(denizens)]
    db.add_all(bs + us)
    await db.commit()
    for u in us:
        for b in bs[:edges_per]:
            a_id, b_id = sorted([u.id, b.id])
            db.add(ResidentRelation(party_a=a_id, party_b=b_id,
                                    familiarity=0.6))
    await db.commit()
    return bs, us


@pytest.fixture(autouse=True)
def _thresholds(monkeypatch):
    """全部旋钮显式置成默认值，用例不依赖 env 的外部状态。

    ⚠️ `CIVIC_PROMOTION_BREAKER_MIN_ABS` 必须显式设：小世界夹具（4 位内置公民）
    的比例项只有 4 × 0.20 = 0.8，没有绝对下限的话**任何一个候选都会触发熔断**，
    on 态与 shadow 态的用例全部废掉（shadow 分支在熔断 return 之后，会变成空跑）。
    """
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_WORLD_DAYS", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_PEERS", "2")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_FAMILIARITY", "0.2")
    monkeypatch.setenv("CIVIC_PEER_SEASONING_WORLD_DAYS", "28")
    monkeypatch.setenv("CIVIC_PROMOTION_MAX_PER_RUN", "5")
    monkeypatch.setenv("CIVIC_PROMOTION_BREAKER_FRACTION", "0.20")
    monkeypatch.setenv("CIVIC_PROMOTION_BREAKER_MIN_ABS", "3")
    monkeypatch.delenv("CIVIC_AUTO_DEMOTION_ENABLED", raising=False)
    yield


# ── off ────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_default_mode_with_no_env_set_is_off_and_a_noop(db_session,
                                                               monkeypatch):
    """默认态（完全不碰 ``CIVIC_PROMOTION_MODE``）必须是 off——合并本分支
    不得改变生产行为一个字节。"""
    monkeypatch.delenv("CIVIC_PROMOTION_MODE", raising=False)
    await _world(db_session)

    result = await cp.run_promotion_pass(db_session)
    assert result["mode"] == cp.MODE_OFF
    assert result["promoted"] == 0
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0
    assert await ConfigService(db_session).get(cp.RUN_SUMMARY_KEY) is None


@pytest.mark.anyio
async def test_off_is_a_zero_read_zero_write_noop(db_session, monkeypatch):
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "off")
    await _world(db_session)

    result = await cp.run_promotion_pass(db_session)
    assert result["mode"] == cp.MODE_OFF
    assert result["promoted"] == 0
    assert result["candidates"] == []
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0
    assert await ConfigService(db_session).get(cp.RUN_SUMMARY_KEY) is None


@pytest.mark.anyio
async def test_unknown_mode_degrades_to_off(db_session, monkeypatch):
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "ON!!")
    await _world(db_session)
    assert (await cp.run_promotion_pass(db_session))["mode"] == cp.MODE_OFF


# ── shadow ─────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_shadow_computes_the_list_but_writes_no_politics(
        db_session, monkeypatch):
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "shadow")
    _, us = await _world(db_session, denizens=2)

    result = await cp.run_promotion_pass(db_session)
    assert result["mode"] == cp.MODE_SHADOW
    # 必须真的走到 shadow 分支：熔断的 return 在 `if mode == MODE_SHADOW` 之前，
    # 一旦熔断先响，本用例会以「promoted == 0」侥幸全绿而 shadow 分支一行没跑
    assert result["refused"] is None
    assert result["promoted"] == 0
    assert sorted(result["candidates"]) == ["u0", "u1"]
    assert result["evidence"]["u0"]["peers"] == 2
    assert result["evidence"]["u0"]["world_days"] > 0

    # 政治层零写入
    types = (await db_session.execute(
        select(Resident.resident_type).where(
            Resident.slug.in_(["u0", "u1"])))).scalars().all()
    assert set(types) == {cm.UGC_RESIDENT_TYPE}
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0


@pytest.mark.anyio
async def test_shadow_records_the_run_summary_for_the_probe(db_session,
                                                            monkeypatch):
    """shadow 不产生历史行，探针没有别的载体——运行摘要是 shadow 的唯一写。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "shadow")
    await _world(db_session, denizens=1)
    result = await cp.run_promotion_pass(db_session)
    assert result["refused"] is None, "熔断先响的话本用例是空跑（摘要两条路都写）"

    summary = await ConfigService(db_session).get(cp.RUN_SUMMARY_KEY)
    assert summary["mode"] == cp.MODE_SHADOW
    assert summary["candidates"] == ["u0"]
    assert summary["promoted"] == 0
    assert summary["refused"] is None
    assert "world_at" in summary


# ── on ─────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_on_promotes_and_leaves_a_history_row_each(db_session, monkeypatch):
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    _, us = await _world(db_session, denizens=2)

    result = await cp.run_promotion_pass(db_session)
    assert result["refused"] is None
    assert result["promoted"] == 2
    voters = (await db_session.execute(
        select(Resident.slug).where(Resident.is_civic_voter))).scalars().all()
    assert {"u0", "u1"} <= set(voters)
    rows = (await db_session.execute(
        select(CivicStandingHistory))).scalars().all()
    assert len(rows) == 2
    assert {r.actor for r in rows} == {cp.PROMOTION_ACTOR}
    assert {r.reason_code for r in rows} == {cp.PROMOTION_REASON_CODE}
    assert all(r.evidence_json.get("peers") == 2 for r in rows)


@pytest.mark.anyio
async def test_running_twice_is_idempotent(db_session, monkeypatch):
    """已晋升的人不再进候选面（select_promotions 只收 denizen 档）。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    await _world(db_session, denizens=1)
    assert (await cp.run_promotion_pass(db_session))["promoted"] == 1
    assert (await cp.run_promotion_pass(db_session))["promoted"] == 0


@pytest.mark.anyio
async def test_pass_never_demotes(db_session, monkeypatch):
    """夜间任务只升，永不自动降——门槛②读的 familiarity 有周衰减，接成降级
    判据等于让公民权跟着社交波动飘。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    bs, _ = await _world(db_session, builtins=4, denizens=0)
    naturalised = _res("naturalised", cm.CIVIC_MEMBER_TYPE,
                       meta={"origin": "forge"})
    db_session.add(naturalised)
    await db_session.commit()

    result = await cp.run_promotion_pass(db_session)
    assert result.get("demoted", 0) == 0
    rtype = (await db_session.execute(
        select(Resident.resident_type)
        .where(Resident.slug == "naturalised"))).scalar_one()
    assert rtype == cm.CIVIC_MEMBER_TYPE


@pytest.mark.anyio
async def test_auto_demotion_flag_raises_instead_of_running_unhedged(
        db_session, monkeypatch):
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    monkeypatch.setenv("CIVIC_AUTO_DEMOTION_ENABLED", "true")
    await _world(db_session)
    with pytest.raises(NotImplementedError, match="滞后"):
        await cp.run_promotion_pass(db_session)


# ── 写路径的结构性收口（Task 6 评审硬要求）───────────────────────────────
#
# select_promotions 默认 mode=MODE_SHADOW（观测态，任何门槛值都放行）。如果
# 写路径直接复用这个签名（"忘记传 mode='on'"），占位门槛就会悄悄流进
# grant_citizenship_batch——闸门形同虚设。下面四个测试逐一证明：写路径的
# 唯一入口 select_promotions_for_write 把 mode="on" 硬编码、不对外暴露 mode
# 形参，且是 grant_citizenship_batch 唯一可能被触达的路。

def test_select_promotions_for_write_hardcodes_mode_on(monkeypatch):
    """专用写路径入口不暴露 mode 形参——用占位门槛调用必炸，调用方没有
    "忘记传 mode='on'" 的余地。对照：同样的占位门槛喂给 select_promotions()
    的默认（shadow-defaulting）签名不炸。"""
    monkeypatch.delenv("CIVIC_THRESHOLDS_CALIBRATED", raising=False)
    snap = cp.PromotionSnapshot(now_world=datetime(2026, 8, 1, tzinfo=UTC),
                                facts=(), familiarity=())
    placeholder_kw = dict(min_world_days=cp.PLACEHOLDER_THRESHOLDS[0],
                          min_peers=cp.PLACEHOLDER_THRESHOLDS[1],
                          min_familiarity=cp.PLACEHOLDER_THRESHOLDS[2],
                          seasoning_days=28.0)

    with pytest.raises(cp.UncalibratedThresholds):
        cp.select_promotions_for_write(snap, **placeholder_kw)

    assert cp.select_promotions(snap, **placeholder_kw) == ()


@pytest.mark.anyio
async def test_on_mode_raises_uncalibrated_thresholds_end_to_end(db_session,
                                                                  monkeypatch):
    """占位门槛端到端拒绝：mode=on 时，就算世界里已经有合法候选，整条 pass
    必须在**任何写入之前**炸出来——run summary 也不写（``_record_run`` 从
    未被调用到），不是「拒绝了但摘要两条路都写」那种半吊子。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    monkeypatch.delenv("CIVIC_THRESHOLDS_CALIBRATED", raising=False)
    # 覆盖 autouse 夹具，改回与 PLACEHOLDER_THRESHOLDS 逐字相同
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_WORLD_DAYS", "30")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_PEERS", "3")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_FAMILIARITY", "0.20")
    await _world(db_session, builtins=4, denizens=1, edges_per=3)

    with pytest.raises(cp.UncalibratedThresholds):
        await cp.run_promotion_pass(db_session)

    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0
    assert await ConfigService(db_session).get(cp.RUN_SUMMARY_KEY) is None


@pytest.mark.anyio
async def test_write_entrypoint_unreachable_when_gate_off_or_shadow(
        db_session, monkeypatch):
    """结构性证明：能触达 grant_citizenship_batch 的唯一候选来源是
    select_promotions_for_write。用 spy 顶替它：off/shadow 两态下调用次数
    必须是 0——不是「调用了但没生效」，是从未进入那段代码路径；on 态下必须
    恰好调用一次。同一个 spy、同一个世界，跨三态观察，证明 shadow 与 on 在
    「有没有走到写路径入口」这件事上是结构性不同，不是行为上凑巧不同。"""
    calls = []
    real_entry = cp.select_promotions_for_write

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_entry(*args, **kwargs)

    monkeypatch.setattr(cp, "select_promotions_for_write", spy)
    await _world(db_session, denizens=2)

    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "off")
    off_result = await cp.run_promotion_pass(db_session)
    assert off_result["mode"] == cp.MODE_OFF
    assert calls == []

    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "shadow")
    shadow_result = await cp.run_promotion_pass(db_session)
    assert shadow_result["mode"] == cp.MODE_SHADOW
    assert shadow_result["promoted"] == 0
    assert calls == [], "shadow 态绝不能触达写路径的专用入口"

    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    on_result = await cp.run_promotion_pass(db_session)
    assert on_result["promoted"] == 2
    assert len(calls) == 1, "on 态必须、且只能经这一个入口进候选集"


@pytest.mark.anyio
async def test_write_path_ids_come_from_dedicated_entrypoint_not_shadow_list(
        db_session, monkeypatch):
    """把 select_promotions_for_write 换成返回真子集的桩，证明喂给
    grant_citizenship_batch 的 id 就是这个专用入口的返回值——不是从
    select_promotions() 的默认（shadow-defaulting）调用算出的候选表旁路
    进来的（世界里有 2 个合法候选，桩只放行 1 个，DB 必须只写 1 行）。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    _, us = await _world(db_session, denizens=2)
    only_first = (us[0].id,)
    monkeypatch.setattr(cp, "select_promotions_for_write",
                        lambda *a, **kw: only_first)

    result = await cp.run_promotion_pass(db_session)
    assert result["promoted"] == 1
    rows = (await db_session.execute(
        select(CivicStandingHistory))).scalars().all()
    assert len(rows) == 1
    assert rows[0].resident_id == only_first[0]


# ── 写路径与探针（Task 11）的接线 ─────────────────────────────────────────

@pytest.mark.anyio
async def test_on_mode_writes_history_shaped_for_the_probes_leak_check(
        db_session, monkeypatch):
    """写路径产生的历史行必须让 Task 11 探针（burnin_report.py）的泄漏判据
    认出「合法晋升」——显式跑一遍探针而不是假设写形状正确。"""
    from scripts.burnin_report import fetch_civic_standing_snapshot

    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    await _world(db_session, denizens=2)

    result = await cp.run_promotion_pass(db_session)
    assert result["promoted"] == 2

    snapshot = await fetch_civic_standing_snapshot(db_session)
    assert snapshot["available"] is True
    assert snapshot["leaked"] == []
    assert snapshot["cross"]["ugc_citizen_promoted"] == 2
    assert snapshot["cross"]["ugc_citizen_unrecorded"] == 0


# ── 数值闸门 ───────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_circuit_breaker_refuses_the_whole_batch(db_session, monkeypatch):
    """候选集 > max(下限 3, 当前公民数 × 20%) → 整批拒绝并告警，**不截断**。
    截断会掩盖「阈值写反」这类全量误判。

    世界规模刻意开到 20 位内置公民，让**比例项**（20 × 0.20 = 4）压过绝对下限
    （3）——这样本用例测的是比例语义，不是下限语义。5 > 4 → 熔断。
    """
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    monkeypatch.setenv("CIVIC_PROMOTION_MAX_PER_RUN", "100")
    await _world(db_session, builtins=20, denizens=5)   # 5 > max(3, 20 × 0.20)

    result = await cp.run_promotion_pass(db_session)
    assert result["refused"] == "circuit_breaker"
    assert result["promoted"] == 0
    assert len(result["candidates"]) == 5
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0


@pytest.mark.anyio
async def test_breaker_floor_keeps_a_small_town_promotable(db_session, monkeypatch):
    """熔断的绝对下限：小镇规模下比例项 < 3 时以下限为准，合法小批量照常放行。

    没有下限的话，4 位内置公民 × 0.20 = 0.8，**一个候选都过不去**——熔断恒响、
    单夜上限恒不生效，两道闸门语义互相吞掉（生产 11 位公民时阈值 ≈2.2，
    MAX_PER_RUN=5 永远够不着）。
    """
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    await _world(db_session, builtins=4, denizens=3)    # 3 ≤ max(3, 0.8)

    result = await cp.run_promotion_pass(db_session)
    assert result["refused"] is None
    assert result["promoted"] == 3


@pytest.mark.anyio
async def test_breaker_floor_can_be_disabled(db_session, monkeypatch):
    """置 0 即退化成纯比例判定（世界规模足够大之后的口径）。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    monkeypatch.setenv("CIVIC_PROMOTION_BREAKER_MIN_ABS", "0")
    await _world(db_session, builtins=4, denizens=3)    # 3 > 4 × 0.20 = 0.8

    result = await cp.run_promotion_pass(db_session)
    assert result["refused"] == "circuit_breaker"
    assert result["promoted"] == 0


@pytest.mark.anyio
async def test_max_per_run_truncates_deterministically(db_session, monkeypatch):
    """单夜上限是确定性截断（候选已按 id 排序），余量下夜再来——整批拒绝会让
    合法积压永久卡死。截断发生在熔断判定**之后**。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    monkeypatch.setenv("CIVIC_PROMOTION_MAX_PER_RUN", "2")
    monkeypatch.setenv("CIVIC_PROMOTION_BREAKER_FRACTION", "10")   # 熔断不挡
    await _world(db_session, builtins=4, denizens=3)

    first = await cp.run_promotion_pass(db_session)
    assert first["promoted"] == 2
    assert len(first["candidates"]) == 3
    assert first["refused"] is None
    second = await cp.run_promotion_pass(db_session)
    assert second["promoted"] == 1


@pytest.mark.anyio
async def test_breaker_is_evaluated_on_the_full_candidate_set(db_session,
                                                              monkeypatch):
    """先熔断再截断。反过来会让熔断永远打不响（截断后的集合恒 ≤ 上限）。

    同上，世界开到 20 位内置公民让比例项（4）压过绝对下限（3）：5 个候选被
    cap=1 截断后只剩 1 个，若顺序反了就永远 1 ≤ 4，熔断这辈子打不响。
    """
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    monkeypatch.setenv("CIVIC_PROMOTION_MAX_PER_RUN", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_BREAKER_FRACTION", "0.20")
    await _world(db_session, builtins=20, denizens=5)

    result = await cp.run_promotion_pass(db_session)
    assert result["refused"] == "circuit_breaker"
    assert result["promoted"] == 0


# ── 本线不改 nightly_cron ───────────────────────────────────────────────

def test_this_line_does_not_wire_the_cron():
    """共享文件线内不改，接线延到收口。位置写死在 close_due_polls 之后、
    run_npc_voting 之前（≈nightly_cron.py:245）——见 civic_promotion 模块
    docstring。"""
    src = (BACKEND_ROOT / "app" / "tasks" / "nightly_cron.py").read_text(
        encoding="utf-8")
    assert "civic_promotion" not in src, (
        "F2 本批不改 nightly_cron.py；接线是收口 §8 第 2 项")
