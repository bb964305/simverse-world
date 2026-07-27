"""F1 第 3 项:rep_credit_min_score 重标定。

纯函数单测在此;用真实机制跑出分布的 harness 在本文件下半部分(Task 9)。
"""
import pytest

from app.services.reputation_service import (
    CalibrationError,
    describe,
    describe_affinity_coverage,
    recommend_credit_min_score,
)


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
