"""E8 收口:一次性运维脚本,把 legacy list 格式 open poll 的 NPC 票清零重投。

为什么需要它:生产 3 张 open poll 的 ``_npc_voters`` 是存量 ``list[str]`` 格式,
各带 14 个已查不到的 slug。``civic_service.run_npc_voting`` 对 legacy 格式只
移出名册、不动 ``npc_votes`` 计数(物理上没存票的归属,减错票比留一张来源不明
的票更糟)。两张建筑议案(``dynamic_location``,不在 ``_PERSON_TYPES`` 里,结票
时不做候选人存在性校验)在 2026-08-01 结票时仍由这些幽灵票决定——这是唯一在
信息论上站得住的订正:整体清零重投,不能选择性减票。

纪律来自 ``postpone_open_polls.py`` 同源的三条硬约束:目标集自查、
``--dry-run`` 默认、不可重放。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.season import Poll
from app.services.config_service import ConfigService
from scripts.reset_legacy_poll_votes import (
    MARKER_KEY,
    ResetRefused,
    find_targets,
    render,
    reset_legacy_votes,
)


def _legacy_poll(question: str, *, status: str = "open",
                 voters: list[str] | None = None) -> Poll:
    voters = voters if voters is not None else ["ghost1", "ghost2"]
    return Poll(question=question, status=status,
                options_json=[
                    {"label": "赞成", "effect": None, "npc_votes": 2,
                     "_npc_voters": voters},
                    {"label": "反对", "effect": None, "npc_votes": 0},
                ])


def _dict_poll(question: str, *, status: str = "open") -> Poll:
    return Poll(question=question, status=status,
                options_json=[
                    {"label": "赞成", "effect": None, "npc_votes": 1,
                     "_npc_voters": {"alive": 0}},
                    {"label": "反对", "effect": None, "npc_votes": 0},
                ])


async def _opts(db, poll_id) -> list[dict]:
    """列级读,绕开 identity map(conftest 是 expire_on_commit=False)。"""
    return (await db.execute(
        select(Poll.options_json).where(Poll.id == poll_id))).scalar_one()


# ── ① 目标集:只挑 legacy list 格式的 open poll ─────────────────────────

@pytest.mark.anyio
async def test_find_targets_picks_only_legacy_list_format(db_session):
    legacy = _legacy_poll("存量议题")
    modern = _dict_poll("新格式议题")
    db_session.add_all([legacy, modern])
    await db_session.commit()

    targets = await find_targets(db_session)

    assert [p.question for p in targets] == ["存量议题"]


@pytest.mark.anyio
async def test_find_targets_excludes_closed_polls(db_session):
    """已 closed 的 poll 票数已经写进历史结果,不得进目标集。"""
    open_poll = _legacy_poll("在途议题")
    closed_poll = _legacy_poll("已结票议题", status="closed")
    db_session.add_all([open_poll, closed_poll])
    await db_session.commit()

    targets = await find_targets(db_session)

    assert [p.question for p in targets] == ["在途议题"]


# ── ② dict 格式不被碰 ───────────────────────────────────────────────────

@pytest.mark.anyio
async def test_dict_format_poll_is_never_touched(db_session):
    modern = _dict_poll("新格式议题")
    db_session.add(modern)
    await db_session.commit()

    report = await reset_legacy_votes(db_session, apply=True)

    assert report == []
    opts = await _opts(db_session, modern.id)
    assert opts[0]["npc_votes"] == 1
    assert opts[0]["_npc_voters"] == {"alive": 0}


# ── ③ legacy 格式被重置:npc_votes 归零 + 名册清空 ──────────────────────

@pytest.mark.anyio
async def test_legacy_poll_is_reset_to_zero(db_session):
    legacy = _legacy_poll("存量议题")
    db_session.add(legacy)
    await db_session.commit()

    report = await reset_legacy_votes(db_session, apply=True)

    assert [e["action"] for e in report] == ["reset"]
    opts = await _opts(db_session, legacy.id)
    assert all(int(o.get("npc_votes", 0)) == 0 for o in opts)
    assert opts[0]["_npc_voters"] == {}


# ── ④ dry-run 零写库且不落标记 ──────────────────────────────────────────

@pytest.mark.anyio
async def test_dry_run_writes_nothing(db_session):
    legacy = _legacy_poll("存量议题")
    db_session.add(legacy)
    await db_session.commit()

    report = await reset_legacy_votes(db_session)

    assert [e["action"] for e in report] == ["would_reset"]
    opts = await _opts(db_session, legacy.id)
    assert opts[0]["npc_votes"] == 2, "dry-run 不得改库"
    assert opts[0]["_npc_voters"] == ["ghost1", "ghost2"]
    assert await ConfigService(db_session).get(MARKER_KEY) is None, \
        "dry-run 不得落完成标记,否则真跑会被自己的标记拦住"


# ── ⑤ --apply 幂等:重跑收敛到空目标集,不报错 ──────────────────────────

@pytest.mark.anyio
async def test_apply_rerun_converges_to_an_empty_target_set(db_session):
    """重置后那张 poll 变成 dict 格式,不再是目标——第二次 --apply 是安全的
    no-op,不需要 --force-rerun。"""
    legacy = _legacy_poll("存量议题")
    db_session.add(legacy)
    await db_session.commit()

    first = await reset_legacy_votes(db_session, apply=True)
    second = await reset_legacy_votes(db_session, apply=True)

    assert [e["action"] for e in first] == ["reset"]
    assert second == []
    opts = await _opts(db_session, legacy.id)
    assert opts[0]["_npc_voters"] == {}


# ── ⑤b 空目标集重跑不得抹掉审计标记 ────────────────────────────────────

@pytest.mark.anyio
async def test_rerun_on_a_converged_empty_target_set_preserves_the_audit_marker(db_session):
    """Minor 回归:``current_ids`` 为空时(目标集已收敛)不该无条件覆盖标记。

    第一次 --apply 真的重置了一批 id 并把标记写成那批 id;第二次 --apply 此时
    目标集已收敛为空(那张 poll 变成了 dict 格式,不再是目标)。旧代码
    ``cs.set(MARKER_KEY, current_ids, ...)`` 无条件执行,会把标记覆盖成
    ``[]``,丢失"上次实际重置了哪几张 poll"这条审计信息(防呆能力不受损——
    ``[] is not None`` 且下次目标集非空时 ``sorted([]) != 新目标`` 仍会拒绝,
    但审计线索没了)。标记应当保留第一次那批 id。
    """
    legacy = _legacy_poll("存量议题")
    db_session.add(legacy)
    await db_session.commit()

    first = await reset_legacy_votes(db_session, apply=True)
    first_marker = await ConfigService(db_session).get(MARKER_KEY)
    assert first_marker == [legacy.id]
    assert [e["action"] for e in first] == ["reset"]

    second = await reset_legacy_votes(db_session, apply=True)  # 目标集已收敛为空

    assert second == []
    second_marker = await ConfigService(db_session).get(MARKER_KEY)
    assert second_marker == [legacy.id], (
        "空目标集重跑不该把标记覆盖成 []——必须保留上一次实际重置过的那批 id")


# ── ⑥ 不可重放:目标集变了且无 --force-rerun 必须拒绝,且是真 no-op ─────

@pytest.mark.anyio
async def test_a_different_target_set_is_refused_without_force_rerun(db_session):
    first_poll = _legacy_poll("第一批议题")
    db_session.add(first_poll)
    await db_session.commit()
    await reset_legacy_votes(db_session, apply=True)

    # 模拟「又出现了新的 legacy 格式 poll」——目标集因此变化。
    second_poll = _legacy_poll("第二批议题", voters=["ghost3"])
    db_session.add(second_poll)
    await db_session.commit()

    with pytest.raises(ResetRefused):
        await reset_legacy_votes(db_session, apply=True)

    opts = await _opts(db_session, second_poll.id)
    assert opts[0]["npc_votes"] == 2, "拒绝必须是真 no-op——不得留下半改状态"
    assert opts[0]["_npc_voters"] == ["ghost3"]


@pytest.mark.anyio
async def test_force_rerun_allows_a_new_target_set(db_session):
    first_poll = _legacy_poll("第一批议题")
    db_session.add(first_poll)
    await db_session.commit()
    await reset_legacy_votes(db_session, apply=True)

    second_poll = _legacy_poll("第二批议题", voters=["ghost3"])
    db_session.add(second_poll)
    await db_session.commit()

    report = await reset_legacy_votes(db_session, apply=True, force_rerun=True)

    assert [e["action"] for e in report] == ["reset"]
    opts = await _opts(db_session, second_poll.id)
    assert opts[0]["npc_votes"] == 0
    assert opts[0]["_npc_voters"] == {}


# ── ⑦ 报告可读,且 dry-run / apply 在措辞上不可混淆 ─────────────────────

@pytest.mark.anyio
async def test_render_distinguishes_dry_run_from_apply(db_session):
    legacy = _legacy_poll("存量议题")
    db_session.add(legacy)
    await db_session.commit()

    text = render(await reset_legacy_votes(db_session), apply=False)

    assert "DRY-RUN" in text and "未写库" in text
    assert "APPLY" not in text

    applied_text = render(
        await reset_legacy_votes(db_session, apply=True), apply=True)

    assert "APPLY" in applied_text and "已写库" in applied_text
    assert "DRY-RUN" not in applied_text
