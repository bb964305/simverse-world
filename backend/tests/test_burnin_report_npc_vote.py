"""NPC 投票分布探针（burnin_report.py）——fix/npc-choice-option0 的验收复核口径。

固化 ops-audit-2026-07-25B §A 那次手写 SQL 的取数口径：option-0 占比、归一化熵
H/lnK、全票压单一选项的 poll 数。只读、零 LLM。
"""
import pytest

from app.config import settings
from app.models.resident import Resident
from app.services import civic_service
from scripts.burnin_report import (
    fetch_poll_vote_snapshot,
    npc_vote_distribution,
    render_probes_npc_vote,
)
from tests.test_npc_choice_bias import (
    ELECTION_OPTIONS,
    PRODUCTION_NPCS,
    _seed_production_cast,
)


async def _voted_poll(db):
    await _seed_production_cast(db)
    poll = await civic_service.propose(
        db, "镇长选举:谁来当下一任镇长?", ELECTION_OPTIONS,
        proposer_slug="jiang-lin",
    )
    await civic_service.run_npc_voting(db)
    return poll


@pytest.mark.anyio
async def test_probe_reports_spread_after_the_fix(db_session):
    await _voted_poll(db_session)
    snap = await fetch_poll_vote_snapshot(db_session, limit=10)
    assert snap["available"] is True

    dist = npc_vote_distribution(snap)
    assert dist["polls_with_votes"] == 1
    assert dist["votes_total"] == len(PRODUCTION_NPCS) == 14
    assert dist["share0_overall"] <= 0.45
    assert dist["bias_index"] <= 0.15
    assert dist["per_poll"][0]["uniform"] == 0.25
    assert dist["entropy_mean"] >= 0.60
    assert dist["monopoly_polls"] == 0

    text = render_probes_npc_vote(snap, legacy_on=False, limit=10)
    assert "超额偏向指数" in text and "🔴" not in text
    assert "归一化熵" in text and "全票压单一选项" in text


@pytest.mark.anyio
async def test_probe_flags_the_legacy_monopoly(db_session, monkeypatch):
    """开关回落旧算法 → 探针必须把 100% / H=0 / 垄断如实报红（这正是审计里
    现网 3 张 poll 的形态）。"""
    monkeypatch.setattr(settings, "civic_npc_choice_legacy", True)
    await _voted_poll(db_session)
    snap = await fetch_poll_vote_snapshot(db_session, limit=10)

    dist = npc_vote_distribution(snap)
    assert dist["per_poll"][0]["tally"] == [14, 0, 0, 0]
    assert dist["share0_overall"] == 1.0
    assert dist["bias_index"] == 0.75      # 1.0 - 1/4，K=4 形态的满偏
    assert dist["entropy_mean"] == 0.0
    assert dist["monopoly_polls"] == 1

    text = render_probes_npc_vote(snap, legacy_on=True, limit=10)
    assert "🔴 option-0 结构性偏向" in text
    assert "CIVIC_NPC_CHOICE_LEGACY=true" in text


@pytest.mark.anyio
async def test_probe_is_fail_open_without_samples(db_session):
    """没有 poll / 没有 NPC 票 → 探针说「无样本」，不炸、不编数。"""
    snap = await fetch_poll_vote_snapshot(db_session, limit=10)
    assert snap == {"available": True, "polls": []}
    assert npc_vote_distribution(snap)["polls_with_votes"] == 0
    assert "无样本" in render_probes_npc_vote(snap, legacy_on=False, limit=10)

    # a poll nobody voted on is skipped rather than counted as a 0-0 monopoly
    db_session.add(Resident(
        slug="zhao", name="赵启文", district="town_hall", status="idle",
        resident_type="npc", creator_id="sys", tile_x=1, tile_y=1,
        meta_json={"duty": {"key": "town_clerk"}},
    ))
    await db_session.commit()
    await civic_service.propose(
        db_session, "无人投票的议案",
        [{"label": "A", "effect": None}, {"label": "B", "effect": None}],
    )
    snap = await fetch_poll_vote_snapshot(db_session, limit=10)
    assert len(snap["polls"]) == 1
    assert npc_vote_distribution(snap)["polls_with_votes"] == 0
