"""F3 接入夜间链的回归钉（ROADMAP #5 收口）。

F3 与 F2 的接线形态不同：F2 是 nightly_cron 里的独立 block（有专门的位置
硬门测试 test_nightly_civic_promotion_wiring.py）；F3 的两半——卸任财政审计
``record_term_audit`` + 任期到期触发补选 ``trigger_backfill``——挂在
``OfficeService.term_check`` 里，而 term_check 本就是夜间链上的 S2-1 block
（gated on ``polis_office_enabled``，默认 False）。所以「接线」由两段合成：
nightly → term_check → audit/backfill。任何一段断了，F3 在运行时都是死的
——这正是 ROADMAP #5 要收口的「代码合入但运行时是死的」状态。

顺序也是硬要求：审计先于补选——审计总结的是刚结束的任期（读数以 vacate
时刻为界），补选面向下一任；反过来补选一旦当场选出新镇长，审计窗口的
边界就脏了。
"""
from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
CRON = BACKEND / "app" / "tasks" / "nightly_cron.py"
OFFICE = BACKEND / "app" / "services" / "office_service.py"


def _fn(tree: ast.AST, name: str):
    return next((n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == name), None)


def _calls_in_order(path: Path, fn_name: str) -> list[str]:
    fn = _fn(ast.parse(path.read_text(encoding="utf-8")), fn_name)
    assert fn is not None, f"{path.name} 里找不到 {fn_name}"
    calls: list[tuple[int, str]] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name:
                calls.append((node.lineno, name))
    return [n for _, n in sorted(calls)]


def test_nightly_chain_reaches_term_check():
    assert "term_check" in _calls_in_order(CRON, "run_nightly_jobs"), (
        "term_check 不在夜间链上——F3 的任期到期/审计/补选在运行时全是死的")


def test_term_check_is_gated_on_polis_office_enabled():
    """开关默认 False、本批不开闸——term_check 调用必须留在 gate 里面。"""
    fn = _fn(ast.parse(CRON.read_text(encoding="utf-8")), "run_nightly_jobs")
    assert fn is not None
    gated = False
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and "polis_office_enabled" in ast.dump(node.test):
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and getattr(inner.func, "attr", None) == "term_check"):
                    gated = True
    assert gated, (
        "term_check 必须在 polis_office_enabled gate 内——默认关 = 夜间链上"
        "零行为，这是「接线与开闸分开」红线的结构保证")


def test_term_check_audits_then_backfills():
    order = _calls_in_order(OFFICE, "term_check")
    assert "record_term_audit" in order, "term_check 丢了 F3 卸任财政审计"
    assert "trigger_backfill" in order, (
        "term_check 丢了 F3 补选触发——回到「无限期无镇长」断链")
    assert order.index("record_term_audit") < order.index("trigger_backfill"), (
        "审计必须先于补选：审计总结刚结束的任期，补选面向下一任")
