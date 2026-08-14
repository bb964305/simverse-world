"""07-27B A3：把在途 poll 的 ``closes_at`` 推后的一次性运维脚本。

为什么需要它：三张 poll 的候选人在 2026-07-25 的数据事故里被整批删除，
而生产跑的镜像早于 ``install_mayor`` 的结票复核修复（`e83ed51`）与
``_winner_lost_civic_rights`` 的流会分支（`d89f5fb`）。在旧镜像下结票会
静默罢免在任镇长并公告一位不存在的当选人。推后 ``closes_at`` 是买时间，
让修复先上生产。

纪律来自 `docs/PARALLEL_WORKSTREAMS_2026-07-27.md:151-157`（T2 三条硬约束）：
进仓库、被评审、``--dry-run`` 为默认值、不可重放。07-25 事故的根因正是
「手工脚本自带 id 列表绕过 find_targets」，所以目标集必须由脚本自己查。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.season import Poll
from app.services.config_service import ConfigService
from scripts.postpone_open_polls import (
    MARKER_KEY,
    PostponeRefused,
    find_targets,
    postpone,
    render,
)

FAR = datetime(2026, 7, 31, 23, 29, 43, tzinfo=UTC)
SOON = datetime(2026, 7, 27, 23, 29, 43, tzinfo=UTC)
INCIDENT_NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)


def _poll(question: str, *, status: str = "open", closes_at: datetime = SOON) -> Poll:
    return Poll(question=question, status=status, closes_at=closes_at,
                options_json=[{"label": "赞成", "effect": None, "npc_votes": 1}])


# ── ① 目标集由脚本自己查，且只含 open ──────────────────────────────────

@pytest.mark.anyio
async def test_find_targets_excludes_closed_polls(db_session):
    """已结票的 poll 不得进目标集——重放它等于把历史结果重新打开。"""
    db_session.add(_poll("在途议题"))
    db_session.add(_poll("已结票议题", status="closed"))
    await db_session.commit()

    targets = await find_targets(db_session)

    assert [p.question for p in targets] == ["在途议题"]


# ── ② dry-run 是默认值，且真的零写库 ──────────────────────────────────

@pytest.mark.anyio
async def test_dry_run_writes_nothing(db_session):
    """默认 dry-run：出报告、不改 closes_at、不落完成标记。"""
    db_session.add(_poll("在途议题"))
    await db_session.commit()

    report = await postpone(db_session, until=FAR, now=INCIDENT_NOW)

    assert [e["action"] for e in report] == ["would_postpone"]
    poll = (await db_session.execute(select(Poll))).scalar_one()
    assert poll.closes_at.replace(tzinfo=UTC) == SOON, "dry-run 不得改库"
    assert await ConfigService(db_session).get(MARKER_KEY) is None, \
        "dry-run 不得落完成标记，否则真跑会被自己的标记拦住"


# ── ③ --apply 幂等：同一 until 跑两次结果相同 ─────────────────────────

@pytest.mark.anyio
async def test_apply_is_idempotent_for_the_same_until(db_session):
    """同一个 --until 重跑是收敛的，不需要 --force-rerun。"""
    db_session.add(_poll("在途议题"))
    await db_session.commit()

    first = await postpone(db_session, until=FAR, apply=True, now=INCIDENT_NOW)
    second = await postpone(db_session, until=FAR, apply=True, now=INCIDENT_NOW)

    assert [e["action"] for e in first] == ["postponed"]
    assert [e["action"] for e in second] == ["already_at_target"]
    poll = (await db_session.execute(select(Poll))).scalar_one()
    assert poll.closes_at.replace(tzinfo=UTC) == FAR


# ── ④ 不可重放：换了 until 且无 --force-rerun 必须拒绝，且是真 no-op ──

@pytest.mark.anyio
async def test_a_different_until_is_refused_without_force_rerun(db_session):
    """标记已存在而 until 变了 → 抛 PostponeRefused，且一行都没改。"""
    db_session.add(_poll("在途议题"))
    await db_session.commit()
    await postpone(db_session, until=FAR, apply=True, now=INCIDENT_NOW)

    later = FAR + timedelta(days=1)
    with pytest.raises(PostponeRefused):
        await postpone(db_session, until=later, apply=True, now=INCIDENT_NOW)

    poll = (await db_session.execute(select(Poll))).scalar_one()
    assert poll.closes_at.replace(tzinfo=UTC) == FAR, \
        "拒绝必须是真 no-op——不得留下半改状态"


@pytest.mark.anyio
async def test_force_rerun_allows_a_new_until(db_session):
    db_session.add(_poll("在途议题"))
    await db_session.commit()
    await postpone(db_session, until=FAR, apply=True, now=INCIDENT_NOW)

    later = FAR + timedelta(days=1)
    report = await postpone(
        db_session,
        until=later,
        apply=True,
        force_rerun=True,
        now=INCIDENT_NOW,
    )

    assert [e["action"] for e in report] == ["postponed"]
    poll = (await db_session.execute(select(Poll))).scalar_one()
    assert poll.closes_at.replace(tzinfo=UTC) == later


# ── ⑤ until 必须落在「已经过去」与「下次自动选举」之间 ────────────────

@pytest.mark.anyio
async def test_until_in_the_past_is_refused(db_session):
    db_session.add(_poll("在途议题"))
    await db_session.commit()

    with pytest.raises(PostponeRefused):
        await postpone(db_session, until=datetime(2020, 1, 1, tzinfo=UTC),
                       apply=True, now=datetime(2026, 7, 27, tzinfo=UTC))


@pytest.mark.anyio
async def test_until_past_the_next_auto_election_is_refused(db_session):
    """推过下次自动选举 = 新旧两张镇长 poll 撞车，必须拒绝。

    ``election_last_opened`` + ``election_interval_days``(28) 是那个边界。
    """
    db_session.add(_poll("在途议题"))
    await ConfigService(db_session).set(
        "election_last_opened", "2026-07-24", group="civic", updated_by="test")
    await db_session.commit()

    # 2026-07-24 + 28d = 2026-08-21
    with pytest.raises(PostponeRefused):
        await postpone(db_session, until=datetime(2026, 8, 22, tzinfo=UTC),
                       apply=True, now=datetime(2026, 7, 27, tzinfo=UTC))


# ── ⑥ 报告可读，且 dry-run / apply 在措辞上不可混淆 ───────────────────

@pytest.mark.anyio
async def test_render_distinguishes_dry_run_from_apply(db_session):
    db_session.add(_poll("在途议题"))
    await db_session.commit()

    text = render(
        await postpone(db_session, until=FAR, now=INCIDENT_NOW), apply=False
    )

    assert "DRY-RUN" in text and "未写库" in text
    assert "APPLY" not in text
