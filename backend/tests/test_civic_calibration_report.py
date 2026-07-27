"""F2 Task 6b —— 只读标定报告（spec §4.2 的「F2 第一步」）。

阈值必须由实测分布决定，不能拍脑袋——rep_credit_min_score = -0.3 变成装饰性
闸门正是因为它是拍出来的。本脚本复用 civic_promotion 的 snapshot 与判定函数，
保证「标定读数」与「夜间任务判据」逐字同源。
"""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import world_clock
from app.models.civic_standing_history import CivicStandingHistory
from app.models.resident import Resident
from app.models.resident_relation import ResidentRelation
from app.services import civic_membership as cm
from scripts.civic_calibration_report import (
    collect_calibration,
    percentiles,
    render_calibration,
)


def _res(slug, rtype, *, creator_id="u1", meta=None, created_days_ago=100):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id=creator_id, tile_x=1,
                    tile_y=1, meta_json=meta,
                    created_at=datetime.now(UTC)
                    - timedelta(days=created_days_ago))


def _builtin(slug):
    return _res(slug, cm.CIVIC_MEMBER_TYPE, creator_id=cm.SYSTEM_CREATOR_ID,
                meta={"origin": "preset"})


def _ugc(slug, rtype=cm.UGC_RESIDENT_TYPE, **kw):
    return _res(slug, rtype, meta={"origin": "forge"}, **kw)


async def _edge(db, a, b, fam):
    x, y = sorted([a.id, b.id])
    db.add(ResidentRelation(party_a=x, party_b=y, familiarity=fam))
    await db.commit()


# ── 分位数 ─────────────────────────────────────────────────────────────

def test_percentiles_of_an_empty_sample_is_empty():
    assert percentiles([]) == {}


def test_percentiles_use_nearest_rank():
    d = percentiles([1.0, 2.0, 3.0, 4.0, 5.0], qs=(0, 50, 100))
    assert (d["p0"], d["p50"], d["p100"]) == (1.0, 3.0, 5.0)


# ── 交付物：空读数必须自己喊 ───────────────────────────────────────────

@pytest.mark.anyio
async def test_an_empty_world_reports_that_calibration_is_still_pending(db_session):
    """本机 dev 库是空的。空读数 ≠ 标定完成——报告必须自己写出「待生产数据
    复标」，这行就是交给收口会话的交付物。"""
    data = await collect_calibration(db_session)
    assert data["ugc"]["count"] == 0
    assert data["needs_production_recalibration"] is True
    out = render_calibration(data)
    assert "待生产数据复标" in out


# ── 表①：在镇世界日分布锚在公民时钟上 ─────────────────────────────────

@pytest.mark.anyio
async def test_world_days_are_anchored_on_the_civic_clock_not_created_at(db_session):
    """与 build_snapshot 同源：锚 created_at 会让 T2 降权过的存量看起来「早就
    够老了」，标出来的 MIN_WORLD_DAYS 直接失真。"""
    old = _ugc("ugc-old", created_days_ago=365)
    db_session.add_all([_builtin("b1"), old])
    await db_session.commit()
    db_session.add(CivicStandingHistory(
        resident_id=old.id, old_standing=cm.CITIZEN, new_standing=cm.DENIZEN,
        reason=None, reason_code="ops_backfill", actor="ops_backfill_t2",
        evidence_json={},
        world_at=world_clock.now_world().astimezone(UTC) - timedelta(days=2)))
    await db_session.commit()

    data = await collect_calibration(db_session)
    assert data["ugc"]["count"] == 1
    assert data["ugc"]["world_days"]["p50"] < 5


# ── 表②：只数对锚定公民的边 ───────────────────────────────────────────

@pytest.mark.anyio
async def test_top_familiarity_only_counts_edges_to_anchored_citizens(db_session):
    """门槛②的同伴取自锚定公民集。把 denizen 之间的边算进来，标出来的 θ 会
    被一群互相熟识、零条内置边的「脱锚公民团」带偏。"""
    b1, u1, u2 = _builtin("b1"), _ugc("u1"), _ugc("u2")
    db_session.add_all([b1, u1, u2])
    await db_session.commit()
    await _edge(db_session, u1, b1, 0.5)
    await _edge(db_session, u1, u2, 0.9)      # denizen ↔ denizen，不算

    data = await collect_calibration(db_session)
    assert data["familiarity"]["per_resident_top"]["u1"] == [0.5]
    assert data["familiarity"]["per_resident_top"]["u2"] == []


@pytest.mark.anyio
async def test_kth_best_edge_is_the_statistic_that_decides_theta(db_session,
                                                                 monkeypatch):
    """一位居民通过门槛② 当且仅当他对锚定公民的第 k 高 familiarity ≥ θ
    （k = MIN_PEERS）。报告必须直接给这一档的分布。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_PEERS", "2")
    b1, b2, b3, u1 = _builtin("b1"), _builtin("b2"), _builtin("b3"), _ugc("u1")
    db_session.add_all([b1, b2, b3, u1])
    await db_session.commit()
    for peer, fam in ((b1, 0.7), (b2, 0.5), (b3, 0.15)):
        await _edge(db_session, u1, peer, fam)

    data = await collect_calibration(db_session)
    assert data["familiarity"]["per_resident_top"]["u1"] == [0.7, 0.5, 0.15]
    assert data["familiarity"]["kth_best"]["values"] == [0.5]
    assert data["familiarity"]["kth_best"]["k"] == 2


# ── 表③ 与候选面判据 ───────────────────────────────────────────────────

@pytest.mark.anyio
async def test_citizen_counts_split_builtin_from_naturalised(db_session):
    naturalised = _ugc("n1", cm.CIVIC_MEMBER_TYPE)
    db_session.add_all([_builtin("b1"), _builtin("b2"), naturalised, _ugc("u1")])
    await db_session.commit()

    data = await collect_calibration(db_session)
    assert data["citizens"] == {"total": 3, "builtin": 2, "naturalised": 1}


@pytest.mark.anyio
async def test_a_full_sweep_is_flagged_red(db_session, monkeypatch):
    """标定的判据是「使晋升面**非空且非全量**」。全量 = 阈值写松了，报红。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_WORLD_DAYS", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_PEERS", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_FAMILIARITY", "0.1")
    b1, u1 = _builtin("b1"), _ugc("u1")
    db_session.add_all([b1, u1])
    await db_session.commit()
    await _edge(db_session, u1, b1, 0.9)

    data = await collect_calibration(db_session)
    assert data["candidate_face"]["size"] == 1
    assert data["candidate_face"]["total_ugc"] == 1
    assert data["candidate_face"]["verdict"] == "full"
    assert data["needs_production_recalibration"] is True
    assert "🔴" in render_calibration(data)


@pytest.mark.anyio
async def test_a_partial_face_is_the_target_shape(db_session, monkeypatch):
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_WORLD_DAYS", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_PEERS", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_FAMILIARITY", "0.4")
    b1, u1, u2 = _builtin("b1"), _ugc("u1"), _ugc("u2")
    db_session.add_all([b1, u1, u2])
    await db_session.commit()
    await _edge(db_session, u1, b1, 0.9)
    await _edge(db_session, u2, b1, 0.1)

    data = await collect_calibration(db_session)
    assert data["candidate_face"]["verdict"] == "partial"
    assert data["candidate_face"]["slugs"] == ["u1"]
    out = render_calibration(data)
    assert "🔴" not in out


# ── 门槛归因：哪道闸真的在拒人，哪道是装饰 ─────────────────────────────

@pytest.mark.anyio
async def test_gate_attribution_names_which_gate_did_the_rejecting(db_session,
                                                                   monkeypatch):
    """光有分布读不出「是谁在拒」。四个人各代表一种被拒方式，归因必须逐一对上。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_WORLD_DAYS", "100")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_PEERS", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_FAMILIARITY", "0.4")
    b1 = _builtin("b1")
    young = _ugc("u-young", created_days_ago=1)          # 仅世界日不够
    lonely = _ugc("u-lonely")                            # 仅 θ 不够
    both = _ugc("u-both", created_days_ago=1)            # 两道都不过
    ok = _ugc("u-ok")
    db_session.add_all([b1, young, lonely, both, ok])
    await db_session.commit()
    await _edge(db_session, young, b1, 0.9)
    await _edge(db_session, lonely, b1, 0.1)
    await _edge(db_session, ok, b1, 0.9)

    data = await collect_calibration(db_session)
    ga = data["gate_attribution"]
    assert ga["passed"] == data["candidate_face"]["size"] == 1
    assert ga["rejected_by"] == {"world_days": 2, "peers": 2, "banned": 0}
    assert ga["blocked"] == {"world_days_only": 1, "peers_only": 1, "both": 1}
    assert ga["peers_breakdown"] == {"too_few_anchored_edges": 1,
                                     "kth_best_below_theta": 1}
    assert ga["decorative_gates"] == []


@pytest.mark.anyio
async def test_a_gate_that_rejects_nobody_is_named_decorative(db_session,
                                                              monkeypatch):
    """``rep_credit_min_score = -0.3`` 的失效模式：闸门在，拒绝面 0/13。
    verdict=partial **不足以**证明三道闸都生效——必须逐闸给出拒绝面。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_WORLD_DAYS", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_PEERS", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_FAMILIARITY", "0.4")
    b1, u1, u2 = _builtin("b1"), _ugc("u1"), _ugc("u2")
    db_session.add_all([b1, u1, u2])
    await db_session.commit()
    await _edge(db_session, u1, b1, 0.9)
    await _edge(db_session, u2, b1, 0.1)

    data = await collect_calibration(db_session)
    assert data["candidate_face"]["verdict"] == "partial"     # 形状是对的……
    ga = data["gate_attribution"]
    assert ga["rejected_by"]["world_days"] == 0               # ……但两道闸空转
    assert ga["peers_breakdown"]["too_few_anchored_edges"] == 0
    assert ga["decorative_gates"] == ["min_world_days", "min_peers"]
    out = render_calibration(data)
    assert "装饰性" in out
    assert "🔴" not in out


# ── 实测扫描：门槛值由分布推出，不得预填 ───────────────────────────────

@pytest.mark.anyio
async def test_sweep_candidates_are_measured_so_an_empty_world_yields_none(
        db_session):
    """候选阈值全部来自实测分布。库里没有读数 → 一个候选值都不许出现，
    否则「标定」就退化成把常数抄进报告。"""
    data = await collect_calibration(db_session)
    assert data["sweep"]["candidates"] == {
        "min_world_days": [], "min_peers": [], "min_familiarity": []}
    assert data["sweep"]["grid"] == []
    assert data["sweep"]["partial"] == []


@pytest.mark.anyio
async def test_sweep_names_the_measured_triples_that_make_the_face_partial(
        db_session, monkeypatch):
    """本任务的交付物：当前阈值给出全量（= 写松了）时，报告要直接答出
    「哪几组实测取值能让晋升面非空且非全量」，而不是只说一句「写松了」。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_WORLD_DAYS", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_PEERS", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_FAMILIARITY", "0.05")
    b1 = _builtin("b1")
    u1, u2, u3 = _ugc("u1"), _ugc("u2"), _ugc("u3")
    db_session.add_all([b1, u1, u2, u3])
    await db_session.commit()
    for who, fam in ((u1, 0.9), (u2, 0.5), (u3, 0.1)):
        await _edge(db_session, who, b1, fam)

    data = await collect_calibration(db_session)
    assert data["candidate_face"]["verdict"] == "full"
    sweep = data["sweep"]
    assert sweep["candidates"]["min_familiarity"] == [0.1, 0.5, 0.9]
    assert sweep["candidates"]["min_peers"] == [1]
    assert len(sweep["grid"]) == len(sweep["candidates"]["min_world_days"]) * 3
    assert [(r["min_familiarity"], r["size"]) for r in sweep["partial"]] == [
        (0.9, 1), (0.5, 2)]
    # 每一行都能被照抄进 env：报告里的 size 就是用报告里的三个数字算出来的
    assert all(r["verdict"] == "partial" and 0 < r["size"] < 3
               for r in sweep["partial"])
    assert "可用取值" in render_calibration(data)


# ── 只读 ───────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_the_report_is_read_only(db_session):
    """标定是只读动作。写一行都不许——它跑在生产库上。"""
    b1, u1 = _builtin("b1"), _ugc("u1")
    db_session.add_all([b1, u1])
    await db_session.commit()
    await _edge(db_session, u1, b1, 0.5)
    before = (await db_session.execute(
        select(Resident.slug, Resident.resident_type, Resident.meta_json))).all()

    await collect_calibration(db_session)

    after = (await db_session.execute(
        select(Resident.slug, Resident.resident_type, Resident.meta_json))).all()
    assert after == before
    assert not db_session.dirty and not db_session.new and not db_session.deleted
