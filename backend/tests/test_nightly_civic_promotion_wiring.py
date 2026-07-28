"""F2 晋升 pass 接进夜间链，且**位置写死**（07-27B 收口第 2 条）。

**为什么位置是硬要求而不是风格偏好。** 批次设计
`docs/PARALLEL_WORKSTREAMS_2026-07-27.md:181` 把它钉在 `close_due_polls` 之后、
`run_npc_voting` 之前：当晚晋升、当晚补投，新公民参与的第一次关票**分子分母同源**。

接在末尾不是「稍差一点」，是**把危害推迟一晚**：每晚 close 先于 vote，夜 N 末尾
晋升的人在夜 N+1 关票时已经进了法定人数分母（`_eligible_voter_count` 读的是
当下的 `is_civic_voter`），却一票未投——分母涨了、分子没涨，那一夜每张 poll 的
通过门槛被静默抬高。所以回归断言按 **N+1 晚**的口径写：只断言「今晚顺序对」不足
以排除这个故障，必须断言 pass 排在 vote **之前**。

**接线本身是零行为变更**：`run_promotion_pass` 的 `off` 态（`CIVIC_PROMOTION_MODE`
未设时的默认）零读零写立即返回。开闸是另一次独立变更（§7 ④）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

CRON = Path(__file__).resolve().parents[1] / "app" / "tasks" / "nightly_cron.py"


def _call_order(fn_name: str) -> list[str]:
    """`run_nightly_jobs` 函数体里，按源码行序出现的被调函数名。"""
    tree = ast.parse(CRON.read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == fn_name), None)
    assert fn is not None, f"{CRON.name} 里找不到 {fn_name}"
    calls: list[tuple[int, str]] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name:
                calls.append((node.lineno, name))
    return [name for _, name in sorted(calls)]


def test_promotion_pass_is_wired_into_the_nightly_chain():
    """没接线 = F2 的全部代码在运行时是死的。"""
    assert "run_promotion_pass" in _call_order("run_nightly_jobs"), (
        "run_promotion_pass 没有出现在 run_nightly_jobs 里——F2 的晋升机制"
        "在夜间链上永远不会被调用")


def test_promotion_runs_after_closing_polls_and_before_npc_voting():
    """位置硬门：close_due_polls < run_promotion_pass < run_npc_voting。"""
    order = _call_order("run_nightly_jobs")
    for name in ("close_due_polls", "run_promotion_pass", "run_npc_voting"):
        assert name in order, f"{name} 不在夜间链里"
    i_close = order.index("close_due_polls")
    i_pass = order.index("run_promotion_pass")
    i_vote = order.index("run_npc_voting")
    assert i_close < i_pass, (
        "晋升必须在关票之后：同一晚先关票再晋升，新公民才不会影响当晚已开票的"
        f"分母。实测顺序 close={i_close} pass={i_pass}")
    assert i_pass < i_vote, (
        "晋升必须在 NPC 投票之前，否则当晚晋升的人进了下一夜的法定人数分母却"
        f"一票未投，那一夜每张 poll 的通过门槛被静默抬高。实测 pass={i_pass} "
        f"vote={i_vote}")


@pytest.mark.anyio
async def test_off_mode_is_a_true_no_op(db_session, monkeypatch):
    """默认 `off` 态：跑一次 pass 不得产生任何档位变更历史行。

    接线这次变更本身必须是零行为变更——开闸是 §7 ④ 的独立一次变更。
    """
    from sqlalchemy import func, select

    from app.models.civic_standing_history import CivicStandingHistory
    from app.tasks.civic_promotion import run_promotion_pass

    monkeypatch.delenv("CIVIC_PROMOTION_MODE", raising=False)
    before = (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar()

    result = await run_promotion_pass(db_session)

    after = (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar()
    assert after == before, "off 态不得写入任何 civic_standing_history 行"
    assert result.get("mode") == "off", f"未设环境变量时应为 off，实得 {result!r}"
