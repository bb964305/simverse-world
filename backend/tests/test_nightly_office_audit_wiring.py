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

import pytest

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


@pytest.mark.anyio
async def test_nightly_chain_dry_run_all_gates_off(db_engine, monkeypatch, caplog):
    """收口验收干跑：默认开关全关时整条夜间链真实跑通、政治层零写入。

    真实调用 run_nightly_jobs（不 mock 任何 job）。DB 隔离覆盖的是三条不同
    形状的路径，缺一条都会漏到共享全局 engine：
    - nightly_cron 模块级 `from app.database import async_session`——patch
      `nightly_cron.async_session`；
    - 各 job 函数体内 `from app.database import async_session`——这是**调用
      时**才 import，读的是 `app.database` 当时的模块属性，patch
      `app_db.async_session` 就能覆盖它们全部；
    - `app.services.dream_service` 是例外中的例外：它在**模块顶层**
      `from app.database import async_session`（dream_service.py:16），只在
      该模块第一次被 import 时求值一次并绑死引用，之后任何对
      `app.database.async_session` 的 monkeypatch 都追不上——必须单独 patch
      `dream_service.async_session` 本身（下方第三条 setattr）。

    维护提示：夜间链新增的 job 如果也用「模块顶层 from app.database import
    async_session」这种绑死引用写法，必须在这里补一条对应 monkeypatch，否则
    它会静默打到共享全局 engine 而不是本测试的 in-memory sqlite，本测试的
    隔离断言会假阴性地放过它（当前四条断言不受漏补 dream_service 影响，是
    因为 dream 不写 civic_standing_history/offices，纯属巧合，不是设计）。

    空世界下 digest 走 has_material=False 短路，零 LLM 调用。断言的是行为
    面：F2 pass 走 off 态零读零写、term_check 因 gate 关闭根本不被调用、
    civic_standing_history / offices 保持空。
    """
    import app.database as app_db
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.config import settings
    from app.models.civic_standing_history import CivicStandingHistory
    from app.models.office import Office
    from app.services import dream_service
    from app.tasks import nightly_cron

    factory = async_sessionmaker(db_engine, class_=AsyncSession,
                                 expire_on_commit=False)
    monkeypatch.setattr(app_db, "async_session", factory)
    monkeypatch.setattr(nightly_cron, "async_session", factory)
    monkeypatch.setattr(dream_service, "async_session", factory)
    # CIVIC_PROMOTION_MODE 是调用时活读（civic_membership._env_str 每次读
    # os.environ），delenv 是有效隔离手段。polis_office_enabled 不是——它是
    # Settings 单例在启动时定值的静态字段（app/config.py:683 `settings =
    # Settings()`），运行中 delenv 环境变量不会让已构造好的单例重新读取，此前
    # 那行 delenv 是死代码。改为直接 patch 单例属性，对开发机 .env 残留值也
    # 免疫。
    monkeypatch.delenv("CIVIC_PROMOTION_MODE", raising=False)
    monkeypatch.setattr(settings, "polis_office_enabled", False)

    term_check_calls: list[int] = []

    async def _term_check_spy(self, **kwargs):
        term_check_calls.append(1)
        return 0

    monkeypatch.setattr(
        "app.services.office_service.OfficeService.term_check", _term_check_spy)

    with caplog.at_level("INFO"):
        await nightly_cron.run_nightly_jobs()

    async with factory() as db:
        n_hist = (await db.execute(
            select(func.count()).select_from(CivicStandingHistory))).scalar()
        n_office = (await db.execute(
            select(func.count()).select_from(Office))).scalar()
    assert n_hist == 0, "开关全关的干跑不得写任何 civic_standing_history 行"
    assert n_office == 0, "开关全关的干跑不得写 offices 行"
    assert not term_check_calls, (
        "polis_office_enabled 默认关——term_check 不得被夜间链调用")
    assert "F2 civic promotion pass" not in caplog.text, (
        "off 态不应产生晋升 pass 日志（run_promotion_pass 只在 mode!=off 时 log）")
