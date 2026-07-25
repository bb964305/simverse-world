"""S2-5 §6 探针 — 政策漂移距离 + 核心条款触碰计数（burnin_report.py）。

Seeded fixture 出数；``constitutional_core`` 漂移恒 0 是硬断言（红线 §9.2）。
"""
import pytest
from sqlalchemy import select

from app.config import settings
from app.models.policy import Policy
from scripts.burnin_report import (
    core_touch_counts,
    fetch_policy_snapshot,
    policy_drift,
    render_probes_s25,
)


@pytest.fixture
def approval_gate(monkeypatch):
    monkeypatch.setattr(settings, "polis_policy_enabled", True)
    monkeypatch.setattr(settings, "polis_policy_approval_enabled", True)
    return settings


async def _seeded_world(db):
    """A seeded governance history: two administrative amends, three
    simple-majority amends, one absolute-majority amend, three rejected
    attempts on the constitutional core."""
    from app.services.policy_service import PolicyService, PolicyImmutableError

    svc = PolicyService(db)
    await svc.seed_defaults()

    await svc.apply_amend("market_day_discount", 0.85, updated_by="admin:1")
    await svc.apply_amend("market_day_discount", 0.8, updated_by="admin:1")
    await svc.apply_amend("tax_rate", 0.1, updated_by="poll:1")
    await svc.apply_amend("tax_rate", 0.2, updated_by="poll:2")
    await svc.apply_amend("curfew_hours", [22, 6], updated_by="poll:3")
    await svc.apply_amend("election_interval_days", 35, updated_by="poll:4")
    for key in ("election_exists", "exile_right", "lab_approval_gate"):
        with pytest.raises(PolicyImmutableError):
            await svc.propose_amend(key, False, origin="admin", author="admin:1")
    return svc


@pytest.mark.anyio
async def test_policy_drift_is_stepwise_and_tier_ordered(db_session, approval_gate):
    await _seeded_world(db_session)
    snap = await fetch_policy_snapshot(db_session)
    assert snap["available"] is True

    drift = policy_drift(snap)
    per_tier = drift["per_tier"]
    # 阶梯状：每次成功 amend 一跳（version-1 = amend 次数）
    by_key = {d["key"]: d for d in drift["per_policy"]}
    assert by_key["market_day_discount"]["amend_count"] == 2
    assert by_key["tax_rate"]["amend_count"] == 2
    assert by_key["election_interval_days"]["amend_count"] == 1

    # 简单多数档漂移 > 绝对多数档（门槛越高越稳定）
    assert per_tier["simple_majority"]["amend_total"] > \
        per_tier["absolute_majority"]["amend_total"]
    # 归一化数值漂移：0.9 → 0.8 = |Δ|/|seed|
    assert by_key["market_day_discount"]["drift"] == pytest.approx(
        abs(0.8 - settings.market_day_discount) / abs(settings.market_day_discount),
        abs=1e-4)


@pytest.mark.anyio
async def test_constitutional_core_drift_is_always_zero(db_session, approval_gate):
    """硬断言（红线 §9.2 / §6 目标形态）：核心条款漂移恒 0、成功数恒 0。"""
    await _seeded_world(db_session)
    snap = await fetch_policy_snapshot(db_session)

    drift = policy_drift(snap)
    assert drift["core_drift"] == 0.0
    assert drift["core_amends"] == 0
    assert drift["per_tier"]["constitutional_core"]["drift_total"] == 0.0

    versions = (await db_session.execute(
        select(Policy.version).where(Policy.tier == "constitutional_core")
    )).scalars().all()
    assert versions and set(versions) == {1}


@pytest.mark.anyio
async def test_core_touch_counts_attempts_gt_zero_successes_zero(db_session,
                                                                 approval_gate):
    await _seeded_world(db_session)
    snap = await fetch_policy_snapshot(db_session)
    counts = core_touch_counts(snap)
    assert counts["attempts"] == 3          # 尝试数可 >0
    assert counts["successes"] == 0         # 成功数恒 = 0
    assert set(counts["by_key"]) == {"election_exists", "exile_right",
                                     "lab_approval_gate"}
    assert counts["core_rows"] == 5


@pytest.mark.anyio
async def test_render_probes_s25_emits_numbers(db_session, approval_gate):
    await _seeded_world(db_session)
    snap = await fetch_policy_snapshot(db_session)
    text = render_probes_s25(snap, gate_on=True)
    assert "政策漂移距离" in text
    assert "核心条款触碰计数" in text
    assert "核心条款不可触碰" in text       # successes == 0 verdict
    assert "🔴" not in text


def test_render_probes_s25_flags_a_breach():
    """探针必须把"核心条款被改动"渲染成显式红线告警，而不是静默。"""
    snap = {
        "available": True,
        "policies": [
            {"key": "election_exists", "value": False, "tier": "constitutional_core",
             "group": "constitution", "version": 2, "updated_by": "admin:x"},
        ],
        "core_touch": {"attempts": 4, "by_key": {"election_exists": 4}},
    }
    assert core_touch_counts(snap)["successes"] == 1
    text = render_probes_s25(snap, gate_on=True)
    assert "🔴" in text and "红线破防" in text


@pytest.mark.anyio
async def test_probe_fail_open_when_empty(db_session):
    """对照组/未播种：表在但空 → 探针给出对照组说明而不是崩。"""
    snap = await fetch_policy_snapshot(db_session)
    assert snap["available"] is True
    assert snap["policies"] == []
    text = render_probes_s25(snap, gate_on=False)
    assert "对照组" in text
