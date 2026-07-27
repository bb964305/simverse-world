"""F1 第 3 项:rep_credit_min_score 重标定。

纯函数单测在上半部分;用真实机制跑出分布的 harness 在下半部分(Task 9)。
"""
import random

import pytest
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.models.memory import Memory
from app.models.resident import Resident
from app.services import gossip_service, relation_service
from app.services.reputation_service import (
    CalibrationError,
    ScoreRow,
    credit_allowed,
    describe,
    describe_affinity_coverage,
    recompute,
    recommend_credit_min_score,
    score_from_meta,
)
from scripts.rep_calibrate import build_report, render


def test_describe_reports_the_shape_of_the_distribution():
    stats = describe([-0.2, -0.1, 0.0, 0.1, 0.2])
    assert stats["n"] == 5
    assert stats["min"] == pytest.approx(-0.2)
    assert stats["max"] == pytest.approx(0.2)
    assert stats["median"] == pytest.approx(0.0)
    assert stats["p25"] == pytest.approx(-0.1)
    assert stats["p75"] == pytest.approx(0.1)
    assert stats["mean"] == pytest.approx(0.0)
    assert stats["negative_share"] == pytest.approx(0.4)


def test_describe_of_an_empty_sample_is_all_zero():
    stats = describe([])
    assert stats["n"] == 0
    assert stats["median"] == 0.0
    assert stats["negative_share"] == 0.0


def test_recommend_cuts_close_to_the_target_reject_fraction():
    scores = [0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    threshold = recommend_credit_min_score(scores, 0.2)
    assert threshold == pytest.approx(0.5)
    assert sum(1 for s in scores if s < threshold) == 2


def test_recommend_never_rejects_everyone_or_no_one():
    scores = [-0.31, -0.12, -0.05, 0.0, 0.0, 0.02, 0.09, 0.21]
    for fraction in (0.01, 0.15, 0.5, 0.99):
        threshold = recommend_credit_min_score(scores, fraction)
        rejected = sum(1 for s in scores if s < threshold)
        assert 0 < rejected < len(scores), (fraction, threshold, rejected)


def test_recommend_refuses_degenerate_samples():
    with pytest.raises(CalibrationError, match="degenerate"):
        recommend_credit_min_score([0.0] * 10)
    with pytest.raises(CalibrationError, match="at least 2"):
        recommend_credit_min_score([0.3])
    with pytest.raises(CalibrationError, match="reject_fraction"):
        recommend_credit_min_score([0.0, 1.0], 1.5)


def test_recommend_breaks_ties_toward_the_lower_threshold():
    """变异测试发现的缺口:两个候选阈值到 target 的 gap 相等时,``best_gap is
    None or gap < best_gap`` 与 ``<=`` 在 ``test_recommend_never_rejects_*``
    的不变式断言下**完全等价**(两者都满足 ``0 < rejected < n``),brief 自带
    的测试组捉不住这处改动——是本任务里实测到的一条真实幸存变异,而不是
    臆测。这条钉死具体阈值,把 tie-break 方向变成规范而不是巧合。

    scores 在 fraction=0.5(target=4)下,(-0.05, 0.0) 与 (0.0, 0.02) 两个中点
    的 gap 并列最小(都是 1):前者 threshold=-0.025 拒 3 个,后者 threshold=0.01
    拒 5 个。``<`` 保留先出现的候选,即数值更低、拒绝面更小的那个。
    """
    scores = [-0.31, -0.12, -0.05, 0.0, 0.0, 0.02, 0.09, 0.21]
    threshold = recommend_credit_min_score(scores, 0.5)
    assert threshold == pytest.approx(-0.025)
    assert sum(1 for s in scores if s < threshold) == 3


# ── 覆盖率读数:光有分数分布分不清「修好了」和「全落在 fallback 上」──────
#
# gossip_tone(affinity) 在 affinity==0(无关系行,或 canonical pair 查不到)时
# 退化为 rep_gossip_base_tone —— 修复前那个恒定负值。如果生产里绝大多数
# gossip 记忆的 (holder, subject) pair 都查不到非零 affinity,最终分数分布
# 依然会是一条看起来正常的负偏曲线,和「机制生效但population确实偏负」的
# 曲线长得一样。describe_affinity_coverage 吃的是 _score_all 内部本已算出
# 、但目前被丢弃的逐条 pair affinity,把两种情况分开。


def test_describe_affinity_coverage_reports_the_covered_share():
    stats = describe_affinity_coverage([0.0, 0.0, 0.4, -0.2, 0.0])
    assert stats["n"] == 5
    assert stats["covered"] == 2
    assert stats["uncovered"] == 3
    assert stats["coverage_share"] == pytest.approx(0.4)


def test_describe_affinity_coverage_of_an_empty_sample_is_all_zero():
    stats = describe_affinity_coverage([])
    assert stats["n"] == 0
    assert stats["covered"] == 0
    assert stats["uncovered"] == 0
    assert stats["coverage_share"] == 0.0


def test_describe_affinity_coverage_all_fallback_is_zero_coverage():
    stats = describe_affinity_coverage([0.0, 0.0, 0.0])
    assert stats["covered"] == 0
    assert stats["coverage_share"] == 0.0


def test_describe_affinity_coverage_all_real_relations_is_full_coverage():
    stats = describe_affinity_coverage([0.3, -0.5, 0.1])
    assert stats["covered"] == 3
    assert stats["coverage_share"] == pytest.approx(1.0)


# ── Task 8: scripts/rep_calibrate.py 只读标定脚本(照抄 brief) ──────────

from sqlalchemy import select  # noqa: E402

from app.models.memory import Memory  # noqa: E402
from app.models.resident import Resident  # noqa: E402
from app.services import relation_service  # noqa: E402
from app.services.reputation_service import project  # noqa: E402
from scripts.rep_calibrate import _gossip_affinities, _run  # noqa: E402


def _rows(scores):
    return [
        ScoreRow(resident_id=f"r{i}", slug=f"s{i}", previous=0.0, score=score, samples=i)
        for i, score in enumerate(scores)
    ]


def test_build_report_flags_the_decorative_gate():
    report = build_report(_rows([-0.20, -0.12, -0.05, 0.0, 0.0, 0.03, 0.08, 0.15]),
                          0.25, -0.3)
    assert report["n"] == 8
    assert report["current_rejected"] == 0            # -0.3 谁也拒绝不了
    assert report["recommended"] == pytest.approx(-0.085)
    assert report["recommended_rejected"] == 2
    assert [entry["slug"] for entry in report["lowest"]][0] == "s0"
    text = render(report)
    assert "装饰性闸门" in text
    assert "建议 REP_CREDIT_MIN_SCORE" in text


def test_build_report_handles_a_degenerate_world():
    report = build_report(_rows([0.0, 0.0]), 0.15, -0.3)
    assert report["recommended"] is None
    assert "degenerate" in report["error"]
    assert "无法标定" in render(report)


def test_build_report_handles_an_empty_world():
    report = build_report([], 0.15, -0.3)
    assert report["n"] == 0
    assert report["recommended"] is None
    assert render(report)   # 不炸


# ── 硬性要求:输出必须同时包含分数分布与 affinity 覆盖率,缺一不可 ──────
#
# task-8-brief.md 的 Step 3 代码原样只把 describe()/recommend_credit_min_score()
# 接进 build_report——两者都只吃最终分数,回答不了"多大比例的 gossip 落在
# gossip_tone(0) fallback 上"这个问题(Task 7 的报告已论证过,见
# describe_affinity_coverage 的 docstring)。这正是本脚本存在的理由,orchestrator
# 的任务说明把它列为硬性要求。下面钉死 build_report/render 必须能端出这份读数,
# 且不破坏 brief 原样的 3 参调用。


def test_build_report_includes_affinity_coverage_when_provided():
    rows = _rows([-0.2, 0.1, 0.0])
    report = build_report(rows, 0.25, -0.3, affinities=[0.4, 0.0, 0.0, -0.2])
    assert report["affinity_coverage"] == {
        "n": 4, "covered": 2, "uncovered": 2, "coverage_share": 0.5,
    }
    text = render(report)
    assert "2/4" in text
    assert "50.0%" in text
    assert "fallback" in text.lower()


def test_build_report_without_affinities_omits_the_coverage_section():
    """brief 原样的 3 参调用(上面那组测试)必须保持不变——affinities 是
    可选的向后兼容追加参数,不是破坏性改动。"""
    report = build_report(_rows([-0.1, 0.1]), 0.25, -0.3)
    assert "affinity_coverage" not in report
    render(report)   # 不炸,且不含覆盖率行


def test_render_names_the_fallback_when_affinity_coverage_is_all_zero():
    """覆盖率为 0 时,措辞要点名 fallback——呼应"光看分数分布分不清机制生效
    但偏负、还是全落在 fallback 上"这条口径。"""
    rows = _rows([-0.2, -0.1, 0.0])
    report = build_report(rows, 0.25, -0.3, affinities=[0.0, 0.0, 0.0])
    text = render(report)
    assert "0/3" in text
    assert "fallback" in text.lower()


def test_build_report_affinity_coverage_of_empty_gossip_sample_does_not_explode():
    report = build_report(_rows([-0.1, 0.1]), 0.25, -0.3, affinities=[])
    assert report["affinity_coverage"]["n"] == 0
    assert render(report)


# ── DB 集成:重建 _score_all 丢弃的逐 pair affinity,并锁死只读 ──────────


def _npc(slug: str):
    return Resident(
        slug=slug, name=slug, district="central_plaza", status="idle",
        resident_type="npc", creator_id=None, tile_x=70, tile_y=56,
        mood_json={"valence": 0.0, "arousal": 0.2, "label": "calm"},
        meta_json={"sbti": {"dimensions": {"Ac1": "H"}}},
    )


@pytest.mark.anyio
async def test_gossip_affinities_reconstructs_the_list_score_all_discards(
    db_session, monkeypatch
):
    """_score_all 算完 tone 就把逐 pair affinity 丢了(Task 7 交接的缺口)——
    标定脚本必须自己从 _affinity_lookup 重新拼,不能假设 project() 的返回值
    里有它。"""
    from app.config import settings

    monkeypatch.setattr(settings, "rep_enabled", False)
    teller = _npc("aff_teller")
    liked = _npc("aff_liked")
    stranger = _npc("aff_stranger")
    db_session.add_all([teller, liked, stranger])
    await db_session.flush()
    db_session.add_all([
        Memory(
            resident_id=teller.id, type="event", content="about liked",
            importance=0.7, source="gossip", related_resident_id=liked.id,
            metadata_json={"hops": 0, "distorted": False},
        ),
        Memory(
            resident_id=teller.id, type="event", content="about stranger",
            importance=0.7, source="gossip", related_resident_id=stranger.id,
            metadata_json={"hops": 0, "distorted": False},
        ),
    ])
    await db_session.commit()
    await relation_service.bump(db_session, teller.id, liked.id, d_affinity=0.4)
    # teller <-> stranger: 无 relation 行 → fallback(affinity 0.0)

    rows = await project(db_session, force=True)
    affinities = await _gossip_affinities(db_session, [row.resident_id for row in rows])

    assert sorted(affinities) == pytest.approx([0.0, 0.4])


@pytest.mark.anyio
async def test_run_combines_distribution_and_coverage_and_writes_nothing(
    db_session, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "rep_enabled", False)  # force=True 绕过它读真实分布
    teller = _npc("run_teller")
    liked = _npc("run_liked")
    stranger = _npc("run_stranger")
    db_session.add_all([teller, liked, stranger])
    await db_session.flush()
    db_session.add_all([
        Memory(
            resident_id=teller.id, type="event", content="about liked",
            importance=0.7, source="gossip", related_resident_id=liked.id,
            metadata_json={"hops": 0, "distorted": False},
        ),
        Memory(
            resident_id=teller.id, type="event", content="about stranger",
            importance=0.7, source="gossip", related_resident_id=stranger.id,
            metadata_json={"hops": 0, "distorted": False},
        ),
    ])
    await db_session.commit()
    await relation_service.bump(db_session, teller.id, liked.id, d_affinity=0.4)

    before = {
        row.id: (row.meta_json, row.mood_json)
        for row in (await db_session.execute(select(Resident))).scalars().all()
    }
    before_memory_count = len((await db_session.execute(select(Memory))).scalars().all())

    report = await _run(0.4, db=db_session)

    # 只读性:逐字段比对(下面的 meta_json/mood_json/行数断言)只能盯住我们想到
    # 的字段——评审复现过一条"在 _gossip_affinities() 循环里改一个字段、不
    # commit/flush"的变异,21 条测试原样全绿,且探针确认该对象确实进了
    # session.dirty。这种"改脏但没提交"的对象,一旦调用方后续在同一 session
    # 上下文里再 commit 一次(比如请求生命周期的收尾钩子),就会被悄悄带着落
    # 库——脏对象本身就是风险,不能靠"这次调用方没 commit"侥幸过关。直接问
    # session 有没有脏对象/待插入对象,把"通过 ORM 间接写库"这整条路径堵死。
    assert not db_session.dirty, f"_run() 弄脏了 ORM 对象: {db_session.dirty!r}"
    assert not db_session.new, f"_run() 往 session 里塞了待插入对象: {db_session.new!r}"

    assert report["n"] == 3
    assert report["affinity_coverage"] == {
        "n": 2, "covered": 1, "uncovered": 1, "coverage_share": 0.5,
    }

    after = (await db_session.execute(select(Resident))).scalars().all()
    assert len(after) == len(before)
    for resident in after:
        assert (resident.meta_json, resident.mood_json) == before[resident.id]
        assert "reputation" not in (resident.meta_json or {})
    after_memory_count = len((await db_session.execute(select(Memory))).scalars().all())
    assert after_memory_count == before_memory_count


# ── main() 退出码契约(mock _run,不碰真实 DB)──────────────────────────


def test_main_returns_0_when_a_reject_face_is_found(monkeypatch, capsys):
    from scripts import rep_calibrate

    async def _fake_run(reject_fraction, db=None):
        return build_report(_rows([-0.2, -0.1, 0.0, 0.1, 0.2]), reject_fraction, -0.3)

    monkeypatch.setattr(rep_calibrate, "_run", _fake_run)
    assert rep_calibrate.main(["--reject-fraction", "0.4"]) == 0
    assert "建议 REP_CREDIT_MIN_SCORE" in capsys.readouterr().out


def test_main_returns_2_when_the_distribution_is_degenerate(monkeypatch, capsys):
    from scripts import rep_calibrate

    async def _fake_run(reject_fraction, db=None):
        return build_report(_rows([0.0, 0.0]), reject_fraction, -0.3)

    monkeypatch.setattr(rep_calibrate, "_run", _fake_run)
    assert rep_calibrate.main([]) == 2
    assert "无法标定" in capsys.readouterr().out


# ── Task 9: 真实机制驱动的分布 harness(第 3 项验收)──────────────────────

CAST = 12
ROUNDS = 12
SEED = 20260727
#: 收口建议值。本线不改 config.py,测试用 monkeypatch 复现收口取值。
RECOMMENDED_BASE_TONE = -0.05


def _sim_resident(index: int) -> Resident:
    # creator_id 必须是 None:Resident.creator_id 是 ForeignKey("users.id")
    # (app/models/resident.py:27-29,nullable),而 harness 从不建 users 行。
    # sqlite 默认不校验外键所以填什么都"能过",但 Task 10 Step 2 允许把
    # DATABASE_URL 指向 Postgres,填字符串就是 ForeignKeyViolation。
    # 与本文件既有的 _resident 助手(tests/test_reputation_service.py)一致。
    return Resident(
        slug=f"sim{index:02d}", name=f"居民{index:02d}", district="central_plaza",
        status="idle", resident_type="npc", creator_id=None,
        tile_x=70, tile_y=56,
        mood_json={"valence": 0.0, "arousal": 0.2, "label": "calm"},
        meta_json={"sbti": {"dimensions": {"Ac1": "H"}}},
    )


async def simulate_world(db, *, cast: int = CAST, rounds: int = ROUNDS,
                         seed: int = SEED) -> list[Resident]:
    """用**真实机制**跑出一个小镇:关系走 relation_service.bump,传闻走
    gossip_service.maybe_gossip。所有数值都是生产常量(闲聊 familiarity +0.05 /
    affinity ±0.03,app/agent/chat.py:64-68),没有一个分数是手写的。

    调用方须先:settings.realism_relations_enabled=True、
    gossip_service.GOSSIP_PROBABILITY=1.0、stub 掉 gossip_service._distort
    (唯一被替换的是那次 LLM 改写调用,测试不联网)。
    """
    residents = [_sim_resident(i) for i in range(cast)]
    db.add_all(residents)
    await db.commit()

    rng = random.Random(seed)
    random.seed(seed)   # maybe_gossip 直接用模块级 random

    # 一手见闻:每个人手里有一条关于别人的高重要性事件记忆(传闻链的源头)
    for index, resident in enumerate(residents):
        subject = residents[(index + 1) % cast]
        db.add(Memory(
            resident_id=resident.id, type="event",
            content=f"{subject.name}在广场上做了件事",
            importance=0.8, source="observation",
            related_resident_id=subject.id,
        ))
    await db.commit()

    pairs = [(a, b) for a in range(cast) for b in range(a + 1, cast)]
    for _ in range(rounds):
        for a, b in pairs:
            if rng.random() >= 0.5:      # 这轮这两人没碰上
                continue
            positive = rng.random() < 0.65
            await relation_service.bump(
                db, residents[a].id, residents[b].id,
                d_familiarity=settings.realism_rel_familiarity_chat,
                d_affinity=(settings.realism_rel_affinity_chat if positive
                            else -settings.realism_rel_affinity_chat),
            )
            await gossip_service.maybe_gossip(db, residents[a], residents[b], rng)
            await gossip_service.maybe_gossip(db, residents[b], residents[a], rng)
    return residents


async def _steady_state(db, residents, nights: int = 3) -> list[float]:
    for _ in range(nights):
        await recompute(db)
    for resident in residents:
        await db.refresh(resident)
    return [score_from_meta(resident.meta_json) for resident in residents]


async def _clear_scores(db, residents) -> None:
    for resident in residents:
        meta = dict(resident.meta_json or {})
        meta.pop("reputation", None)
        resident.meta_json = meta
        flag_modified(resident, "meta_json")
    await db.commit()


@pytest.mark.anyio
async def test_emergent_distribution_is_two_sided_and_has_a_reject_face(
    db_session, monkeypatch
):
    """第 3 项验收:阈值必须由**跑出来的**分布决定,不是构造数据凑。

    注意:这条用例要跑 ~500 次真实的 bump/maybe_gossip,耗时以十秒计,是本仓最慢
    的单测之一。迭代时用 ``-k emergent`` 单独跑。
    """
    monkeypatch.setattr(settings, "rep_enabled", True)
    monkeypatch.setattr(settings, "realism_relations_enabled", True)
    monkeypatch.setattr(gossip_service, "GOSSIP_PROBABILITY", 1.0)

    async def _fake_distort(content: str) -> str:
        return f"据说{content}"

    monkeypatch.setattr(gossip_service, "_distort", _fake_distort)

    residents = await simulate_world(db_session)

    # ① 冻结常量(rep_gossip_base_tone=-0.3)下的稳态分布
    frozen = await _steady_state(db_session, residents)
    frozen_stats = describe(frozen)
    assert all(score > -0.3 for score in frozen), (
        f"-0.3 竟然拒绝到了人,spec 的判断需要重新核对: {sorted(frozen)}")

    # ② 收口建议常量下的稳态分布(同一个世界,清空分数重算)
    await _clear_scores(db_session, residents)
    monkeypatch.setattr(settings, "rep_gossip_base_tone", RECOMMENDED_BASE_TONE)
    fixed = await _steady_state(db_session, residents)
    fixed_stats = describe(fixed)

    print("\n[frozen  base=-0.3 ]", frozen_stats)
    print("[fixed   base=%.2f]" % RECOMMENDED_BASE_TONE, fixed_stats)

    assert min(fixed) < 0.0 < max(fixed), f"分布仍是单边: {sorted(fixed)}"
    assert fixed_stats["negative_share"] < frozen_stats["negative_share"]

    # ③ 用②的真实分布标定阈值,拒绝面必须非空且非全量
    threshold = recommend_credit_min_score(fixed, 0.15)
    print("[recommended REP_CREDIT_MIN_SCORE] %+.4f" % threshold)
    monkeypatch.setattr(settings, "rep_credit_min_score", threshold)
    rejected = [score for score in fixed if not credit_allowed(score)]
    assert 0 < len(rejected) < len(fixed)

    # ④ 现行 -0.3 在同一分布上仍然谁也拒绝不了 —— 装饰性闸门
    monkeypatch.setattr(settings, "rep_credit_min_score", -0.3)
    assert all(credit_allowed(score) for score in fixed)
