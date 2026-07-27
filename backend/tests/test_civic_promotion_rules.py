"""F2 Task 6 —— 晋升判定的 snapshot 语义与纯函数。

整个 pass 是 snapshot 语义：pass 开始一次性冻结输入，中途绝不重读选民集，
否则结果依赖数据库行序、同一状态多次运行得到不同不动点。判定做成纯函数，
测试用 random.shuffle 打乱内存中的居民列表再跑，断言输出集合恒等——不要试图
在 Postgres 上控制行序。
"""
import random
from datetime import UTC, datetime, timedelta

import pytest

from app import world_clock
from app.models.civic_standing_history import CivicStandingHistory
from app.models.resident import Resident
from app.models.resident_relation import ResidentRelation
from app.services import civic_membership as cm
from app.tasks import civic_promotion as cp

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _fact(rid, *, rtype=cm.UGC_RESIDENT_TYPE, builtin=False, ugc=True,
          age_days=100.0, promoted_days_ago=None, banned=False):
    return cp.ResidentFact(
        resident_id=rid, slug=rid, resident_type=rtype, is_builtin=builtin,
        is_ugc=ugc, anchor_world=NOW - timedelta(days=age_days),
        promoted_world=(None if promoted_days_ago is None
                        else NOW - timedelta(days=promoted_days_ago)),
        banned=banned,
    )


def _snap(facts, edges=()):
    return cp.PromotionSnapshot(now_world=NOW, facts=tuple(facts),
                                familiarity=tuple(edges))


def _builtin(rid):
    return _fact(rid, rtype=cm.CIVIC_MEMBER_TYPE, builtin=True, ugc=False)


# ── 锚定公民集 ─────────────────────────────────────────────────────────

def test_anchor_set_is_builtins_plus_seasoned_naturalised_citizens():
    snap = _snap([
        _builtin("b1"), _builtin("b2"),
        # 归化且已过考察期
        _fact("n1", rtype=cm.CIVIC_MEMBER_TYPE, promoted_days_ago=40),
        # 归化但考察期未满
        _fact("n2", rtype=cm.CIVIC_MEMBER_TYPE, promoted_days_ago=5),
        # 还在 denizen 档
        _fact("u1"),
    ])
    assert cp.anchored_citizen_ids(snap, seasoning_days=28.0) == frozenset(
        {"b1", "b2", "n1"})


def test_anchor_set_excludes_a_builtin_that_is_not_currently_a_citizen():
    """锚定集只收当前在 citizen 档的人——档位是活的，出身是冻结的。"""
    snap = _snap([_fact("b1", rtype=cm.UGC_RESIDENT_TYPE, builtin=True, ugc=False)])
    assert cp.anchored_citizen_ids(snap, seasoning_days=28.0) == frozenset()


def test_anchor_set_is_not_the_live_voter_set():
    """若同伴集合就是 is_civic_voter 本身，转移函数自指 → 级联升降 + 脱锚
    公民团（某人的 N 位同伴全是刚晋升的 UGC、零条内置边）。"""
    snap = _snap([
        _fact("fresh1", rtype=cm.CIVIC_MEMBER_TYPE, promoted_days_ago=0),
        _fact("fresh2", rtype=cm.CIVIC_MEMBER_TYPE, promoted_days_ago=0),
        _fact("fresh3", rtype=cm.CIVIC_MEMBER_TYPE, promoted_days_ago=0),
        _fact("u1"),
    ])
    anchors = cp.anchored_citizen_ids(snap, seasoning_days=28.0)
    assert anchors == frozenset(), "刚晋升的人不得立刻成为别人的晋升同伴"
    assert cp.select_promotions(
        snap, min_world_days=1.0, min_peers=1, min_familiarity=0.1,
        seasoning_days=28.0) == ()


# ── 两个门槛 ───────────────────────────────────────────────────────────

def test_both_conditions_must_hold():
    edges = [("u1", "b1", 0.5), ("u1", "b2", 0.5), ("u1", "b3", 0.5)]
    facts = [_builtin("b1"), _builtin("b2"), _builtin("b3"), _fact("u1")]
    kw = dict(min_world_days=30.0, min_peers=3, min_familiarity=0.2,
              seasoning_days=28.0)

    assert cp.select_promotions(_snap(facts, edges), **kw) == ("u1",)
    # 条件① 不满足（在镇 10 世界日 < 30）
    young = [f if f.resident_id != "u1" else _fact("u1", age_days=10.0)
             for f in facts]
    assert cp.select_promotions(_snap(young, edges), **kw) == ()
    # 条件② 不满足（只有 2 位达标同伴）
    thin = edges[:2]
    assert cp.select_promotions(_snap(facts, thin), **kw) == ()
    # 条件② 的边低于 θ
    weak = [(a, b, 0.15) for a, b, _ in edges]
    assert cp.select_promotions(_snap(facts, weak), **kw) == ()


def test_peers_must_be_anchored_citizens_not_other_denizens():
    edges = [("u1", "u2", 0.9), ("u1", "u3", 0.9), ("u1", "u4", 0.9)]
    facts = [_fact("u1"), _fact("u2"), _fact("u3"), _fact("u4")]
    assert cp.select_promotions(
        _snap(facts, edges), min_world_days=1.0, min_peers=3,
        min_familiarity=0.2, seasoning_days=28.0) == ()


def test_edges_are_undirected():
    """resident_relations 存的是规范化无向对（party_a ≤ party_b），所以判定
    必须两个方向都认。"""
    edges = [("b1", "u1", 0.5), ("b2", "u1", 0.5)]
    facts = [_builtin("b1"), _builtin("b2"), _fact("u1")]
    assert cp.select_promotions(
        _snap(facts, edges), min_world_days=1.0, min_peers=2,
        min_familiarity=0.2, seasoning_days=28.0) == ("u1",)


def test_only_ugc_denizens_are_candidates():
    """内置阵容、admin preset、玩家化身、已是公民的人都不进候选面。"""
    edges = [(x, "b1", 0.9) for x in ("p1", "adm1", "n1")]
    facts = [
        _builtin("b1"),
        _fact("p1", rtype=cm.PLAYER_RESIDENT_TYPE, ugc=False),
        _fact("adm1", rtype=cm.ADMIN_PRESET_TYPE, ugc=False),
        _fact("n1", rtype=cm.CIVIC_MEMBER_TYPE, ugc=True, promoted_days_ago=99),
    ]
    assert cp.select_promotions(
        _snap(facts, edges), min_world_days=1.0, min_peers=1,
        min_familiarity=0.2, seasoning_days=28.0) == ()


def test_civic_ban_is_excluded_from_day_one():
    """civic_ban 是 sticky 剥夺位：v1 只留状态位不实现写入，但候选面从第一天
    起就排除它——否则被逐者只要在冷却期内和几个 npc 聊够 familiarity 就自动
    升回，晋升任务无法区分「因疏远而降」与「因违规而逐」。"""
    edges = [("u1", "b1", 0.9), ("u1", "b2", 0.9)]
    facts = [_builtin("b1"), _builtin("b2"), _fact("u1", banned=True)]
    assert cp.select_promotions(
        _snap(facts, edges), min_world_days=1.0, min_peers=2,
        min_familiarity=0.2, seasoning_days=28.0) == ()


# ── 顺序无关性（硬门 4）─────────────────────────────────────────────

def test_output_is_invariant_under_input_shuffling():
    facts = [_builtin(f"b{i}") for i in range(4)]
    facts += [_fact(f"u{i}") for i in range(5)]
    edges = [(f"u{i}", f"b{j}", 0.5) for i in range(5) for j in range(3)]

    kw = dict(min_world_days=30.0, min_peers=3, min_familiarity=0.2,
              seasoning_days=28.0)
    baseline = cp.select_promotions(_snap(facts, edges), **kw)
    assert baseline == ("u0", "u1", "u2", "u3", "u4")

    rng = random.Random(20260727)
    for _ in range(20):
        f2, e2 = list(facts), list(edges)
        rng.shuffle(f2)
        rng.shuffle(e2)
        assert cp.select_promotions(_snap(f2, e2), **kw) == baseline


def test_output_is_sorted_so_the_per_run_cap_is_deterministic():
    facts = [_builtin("b1"), _builtin("b2"),
             _fact("zzz"), _fact("aaa"), _fact("mmm")]
    edges = [(x, b, 0.9) for x in ("zzz", "aaa", "mmm") for b in ("b1", "b2")]
    out = cp.select_promotions(
        _snap(facts, edges), min_world_days=1.0, min_peers=2,
        min_familiarity=0.2, seasoning_days=28.0)
    assert out == tuple(sorted(out)) == ("aaa", "mmm", "zzz")


# ── 证据 ───────────────────────────────────────────────────────────────

def test_promotion_evidence_records_the_three_numbers():
    edges = [("u1", "b1", 0.5), ("u1", "b2", 0.7), ("u1", "b3", 0.15)]
    facts = [_builtin("b1"), _builtin("b2"), _builtin("b3"),
             _fact("u1", age_days=42.0)]
    ev = cp.promotion_evidence(_snap(facts, edges), "u1",
                               min_familiarity=0.2, seasoning_days=28.0)
    assert ev["world_days"] == pytest.approx(42.0)
    assert ev["peers"] == 2
    assert ev["peer_ids"] == ["b1", "b2"]
    assert ev["min_familiarity"] == 0.2
    # 观测面要输出 top-familiarity 分布而非只输出达标计数（否则「晋升面长期
    # 为空」时分不清是阈值问题还是加权采样对新人的结构性歧视）
    assert ev["top_familiarity"][:2] == [0.7, 0.5]


# ── 占位门槛值的开闸闸门（本任务是三个旋钮的第一个调用点）─────────────
#
# spec §4.2：门槛由实测分布标定，不许拍数字。``rep_credit_min_score = -0.3``
# 之所以成为装饰性闸门（拒绝面 0/13），正因为它是拍出来的。本任务是三个旋钮
# 的第一个调用点，所以「占位值不得被当成已标定值走到生产开闸路径」的机制必须
# 在这里立住。

def _gate_snap():
    """一个在任何阈值下都能过判定的最小世界（闸门测的是闸门，不是判据）。"""
    facts = [_builtin("b1"), _builtin("b2"), _builtin("b3"),
             _fact("u1", age_days=999.0)]
    edges = [("u1", f"b{i}", 0.99) for i in (1, 2, 3)]
    return _snap(facts, edges)


_PLACEHOLDER_KW = dict(min_world_days=30.0, min_peers=3, min_familiarity=0.20,
                       seasoning_days=28.0)


def test_the_placeholder_fingerprint_tracks_task2_defaults(monkeypatch):
    """指纹必须与 Task 2 的三个占位默认值逐字相等——这条断言就是绊线：
    真标定完把 ``_DEFAULT_*`` 改成实测值时本测试会红，改指纹的那一笔 diff
    就是「这组数字是量出来的」的书面承认，不会被顺手滑过去。"""
    for name in ("CIVIC_PROMOTION_MIN_WORLD_DAYS", "CIVIC_PROMOTION_MIN_PEERS",
                 "CIVIC_PROMOTION_MIN_FAMILIARITY"):
        monkeypatch.delenv(name, raising=False)
    assert cp.PLACEHOLDER_THRESHOLDS == (
        cm._DEFAULT_MIN_WORLD_DAYS, cm._DEFAULT_MIN_PEERS,
        cm._DEFAULT_MIN_FAMILIARITY)
    # 旋钮今天返回的就是占位值 → 现在开闸必然撞闸门（下一条测的就是它）
    assert (cm.min_world_days(), cm.min_peers(), cm.min_familiarity()) == \
        cp.PLACEHOLDER_THRESHOLDS


def test_live_promotion_refuses_the_untouched_placeholder_thresholds(monkeypatch):
    """mode=on（真写库那一态）+ 三个值原封不动 = 「占位值被当成已标定值」，
    拒绝并告警，而不是照着装饰性闸门整批放行。"""
    monkeypatch.delenv("CIVIC_THRESHOLDS_CALIBRATED", raising=False)
    with pytest.raises(cp.UncalibratedThresholds) as err:
        cp.select_promotions(_gate_snap(), mode="on", **_PLACEHOLDER_KW)
    assert "标定" in str(err.value)


def test_shadow_and_off_still_run_on_placeholder_values(monkeypatch):
    """闸门只挡开闸，不挡观测：标定报告与 shadow 名单恰恰要在占位值上跑，
    挡住它们就没人能量出真值了。默认参数（不传 mode）也必须是观测态。"""
    monkeypatch.delenv("CIVIC_THRESHOLDS_CALIBRATED", raising=False)
    snap = _gate_snap()
    assert cp.select_promotions(snap, **_PLACEHOLDER_KW) == ("u1",)
    for mode in ("shadow", "off"):
        assert cp.select_promotions(snap, mode=mode, **_PLACEHOLDER_KW) == ("u1",)


def test_calibrated_values_open_the_gate(monkeypatch):
    """任一门槛不再是占位值 → 有人动过手，开闸放行。"""
    monkeypatch.delenv("CIVIC_THRESHOLDS_CALIBRATED", raising=False)
    kw = dict(_PLACEHOLDER_KW, min_familiarity=0.34)
    assert cp.select_promotions(_gate_snap(), mode="on", **kw) == ("u1",)


def test_an_explicit_ack_is_the_only_other_way_through(monkeypatch):
    """唯一的另一条合法出口：实测分布恰好落在占位值上。这时要在环境里显式
    写下标定凭据（报告日期 / commit），闸门放行并把凭据打进日志。"""
    monkeypatch.setenv("CIVIC_THRESHOLDS_CALIBRATED", "2026-07-27-vm212-report")
    assert cp.select_promotions(
        _gate_snap(), mode="on", **_PLACEHOLDER_KW) == ("u1",)
    # 空串 / 0 / false 不算凭据——「设了个空变量」不是标定
    for junk in ("", "  ", "0", "false", "no"):
        monkeypatch.setenv("CIVIC_THRESHOLDS_CALIBRATED", junk)
        with pytest.raises(cp.UncalibratedThresholds):
            cp.select_promotions(_gate_snap(), mode="on", **_PLACEHOLDER_KW)


# ── 快照构建（唯一一次 DB 读）───────────────────────────────────────

def _res(slug, rtype, *, creator_id="u1", meta=None, created_at=None):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id=creator_id, tile_x=1,
                    tile_y=1, meta_json=meta,
                    created_at=created_at or datetime.now(UTC))


@pytest.mark.anyio
async def test_build_snapshot_anchors_on_history_not_created_at(db_session):
    """锚 created_at 会让 T2 的降权对存量整批走过场——一个已在镇 200 世界日的
    UGC 被降权后，开闸当晚条件①立刻重新满足。"""
    old = _res("ugc-old", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"},
               created_at=datetime.now(UTC) - timedelta(days=365))
    db_session.add(old)
    await db_session.commit()
    recent_world = world_clock.now_world().astimezone(UTC) - timedelta(days=2)
    db_session.add(CivicStandingHistory(
        resident_id=old.id, old_standing=cm.CITIZEN, new_standing=cm.DENIZEN,
        reason=None, reason_code="ops_backfill", actor="ops_backfill_t2",
        evidence_json={}, world_at=recent_world))
    await db_session.commit()

    snap = await cp.build_snapshot(db_session)
    fact = next(f for f in snap.facts if f.slug == "ugc-old")
    age = (snap.now_world - fact.anchor_world) / timedelta(days=1)
    assert age < 5, f"锚点回落到了 created_at（世界龄 {age:.1f} 天）"


@pytest.mark.anyio
async def test_build_snapshot_falls_back_to_created_at_without_history(db_session):
    born = datetime.now(UTC) - timedelta(days=10)
    db_session.add(_res("ugc-1", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"},
                        created_at=born))
    await db_session.commit()

    snap = await cp.build_snapshot(db_session)
    fact = next(f for f in snap.facts if f.slug == "ugc-1")
    # k=4：10 真实日 = 40 世界日
    age = (snap.now_world - fact.anchor_world) / timedelta(days=1)
    assert age == pytest.approx(10 * world_clock._k(), rel=0.05)


@pytest.mark.anyio
async def test_build_snapshot_honours_the_t2_backfill_mark_fallback(db_session):
    """降级路径（运维时序反了时的兜底）：无历史行的 UGC，anchor 取
    max(created_at→world, T2 完成标记的世界时间)。"""
    from app.services.config_service import ConfigService

    db_session.add(_res("ugc-1", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"},
                        created_at=datetime.now(UTC) - timedelta(days=365)))
    await db_session.commit()
    mark = (world_clock.now_world() - timedelta(days=3)).date().isoformat()
    await ConfigService(db_session).set(cp.BACKFILL_MARK_KEY, mark,
                                        group="civic", updated_by="ops")

    snap = await cp.build_snapshot(db_session)
    fact = next(f for f in snap.facts if f.slug == "ugc-1")
    age = (snap.now_world - fact.anchor_world) / timedelta(days=1)
    assert age <= 4, f"降级路径没生效（世界龄 {age:.1f} 天）"


@pytest.mark.anyio
async def test_build_snapshot_reads_relations_and_provenance(db_session):
    b = _res("b1", cm.CIVIC_MEMBER_TYPE, creator_id=cm.SYSTEM_CREATOR_ID,
             meta={"origin": "preset"})
    u = _res("u1", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"})
    db_session.add_all([b, u])
    await db_session.flush()
    a_id, b_id = sorted([b.id, u.id])
    db_session.add(ResidentRelation(party_a=a_id, party_b=b_id,
                                    familiarity=0.42, affinity=0.1))
    await db_session.commit()

    snap = await cp.build_snapshot(db_session)
    by_slug = {f.slug: f for f in snap.facts}
    assert by_slug["b1"].is_builtin is True and by_slug["b1"].is_ugc is False
    assert by_slug["u1"].is_builtin is False and by_slug["u1"].is_ugc is True
    assert snap.familiarity == ((a_id, b_id, 0.42),)


@pytest.mark.anyio
async def test_build_snapshot_ignores_player_party_relations(db_session):
    """resident_relations 把 resident-resident 与 resident-player 统一在一张
    表里（party_*_type）；玩家的边不该给公民权判定投票。"""
    b = _res("b1", cm.CIVIC_MEMBER_TYPE, creator_id=cm.SYSTEM_CREATOR_ID)
    db_session.add(b)
    await db_session.flush()
    db_session.add(ResidentRelation(party_a="user-xyz", party_a_type="player",
                                    party_b=b.id, party_b_type="resident",
                                    familiarity=0.9))
    await db_session.commit()

    snap = await cp.build_snapshot(db_session)
    assert snap.familiarity == ()
