"""M-A 集成 — nightly 段落 #23:NPC 贸易三 pass 接进夜间链。

Plan: `docs/plans/2026-08-09-M-A-npc-economy.md` Step 8;spec §「集成:nightly 段落 #23」。

接线不做完,C2/C3 的全部代码在运行时是死的(F2 接线本 test_nightly_civic_promotion_
wiring.py 钉的是同一类故障)。这里钉三件事:

1. **顺序是硬要求**:结算 → 接单 → 消费。先结算再接单,今晚刚接的单不会在同一晚
   被结掉(委托要跨夜才有"跑腿"的意味);消费放最后,让结算刚到账的赏金当晚就能花
   出去(赏金→购买力的传导不必等一整晚)。
2. **gate 在调用之前**:`npc_economy_enabled and npc_trade_enabled` 双闸(与
   `npc_trade_service` 三个 pass 的内部闸同口径),关 = 连 session 都不开。
3. **fail-open**:段内任一 pass 抛,异常被吞在本段的 try/except 里,不外溢到
   `run_nightly_jobs` 的调用方,也不影响本段之前已经跑完的段落。

DB 隔离沿用 test_nightly_office_audit_wiring.py:76 的三处 patch(模块级 /
调用时 import / dream_service 的绑死引用),否则整条链会打到共享全局 engine。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

CRON = Path(__file__).resolve().parents[1] / "app" / "tasks" / "nightly_cron.py"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _fn(tree: ast.AST, name: str):
    return next((n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == name), None)


def _call_order(fn_name: str) -> list[str]:
    """`run_nightly_jobs` 函数体里,按源码行序出现的被调函数名。"""
    fn = _fn(ast.parse(CRON.read_text(encoding="utf-8")), fn_name)
    assert fn is not None, f"{CRON.name} 里找不到 {fn_name}"
    calls: list[tuple[int, str]] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name:
                calls.append((node.lineno, name))
    return [n for _, n in sorted(calls)]


# --------------------------------------------------------------------------- #
# 结构面:接线位置 / gate 位置 / 自带 try                                        #
# --------------------------------------------------------------------------- #

def test_three_passes_are_wired_into_the_nightly_chain():
    order = _call_order("run_nightly_jobs")
    for name in ("run_commission_settle_pass", "run_commission_accept_pass",
                 "run_consumption_pass"):
        assert name in order, (
            f"{name} 没有出现在 run_nightly_jobs 里——M-A 的 C2/C3 在运行时是死的")


def test_settle_then_accept_then_consume():
    order = _call_order("run_nightly_jobs")
    i_settle = order.index("run_commission_settle_pass")
    i_accept = order.index("run_commission_accept_pass")
    i_consume = order.index("run_consumption_pass")
    assert i_settle < i_accept, (
        "结算必须先于接单:同一晚先接后结 = 委托当晚发当晚完,跑腿这件事失去时间"
        f"厚度。实测 settle={i_settle} accept={i_accept}")
    assert i_accept < i_consume, (
        "消费放最后:结算到账的赏金当晚即可花出去。实测 accept="
        f"{i_accept} consume={i_consume}")


def test_block_is_appended_after_the_existing_ones():
    """新段追加在 S1-5 之后,既有段一个都不许挪位。"""
    order = _call_order("run_nightly_jobs")
    assert order.index("run_public_spending") < order.index("run_commission_settle_pass")


def test_passes_are_gated_before_the_session_opens():
    fn = _fn(ast.parse(CRON.read_text(encoding="utf-8")), "run_nightly_jobs")
    assert fn is not None
    gated = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and "npc_trade_enabled" in ast.dump(node.test):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    name = getattr(inner.func, "id", None) or getattr(
                        inner.func, "attr", None)
                    if name:
                        gated.add(name)
    assert {"run_commission_settle_pass", "run_commission_accept_pass",
            "run_consumption_pass"} <= gated, (
        "三个 pass 必须整体在 npc_trade_enabled gate 里面——默认关 = 夜间链上零"
        "读零写,这是「接线与开闸分开」红线的结构保证")
    assert "async_session" in gated, "session 也要在 gate 内开,关闸不许碰 DB"


# --------------------------------------------------------------------------- #
# 行为面:真跑一遍夜间链                                                        #
# --------------------------------------------------------------------------- #

@pytest.fixture
def nightly(db_engine, monkeypatch):
    """把整条夜间链的 session 工厂钉到本测试的 in-memory sqlite 上。"""
    import app.database as app_db
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.services import dream_service
    from app.tasks import nightly_cron

    factory = async_sessionmaker(db_engine, class_=AsyncSession,
                                 expire_on_commit=False)
    monkeypatch.setattr(app_db, "async_session", factory)
    monkeypatch.setattr(nightly_cron, "async_session", factory)
    monkeypatch.setattr(dream_service, "async_session", factory)
    return nightly_cron


@pytest.fixture
def spy_passes(monkeypatch):
    """把三个 pass 换成记录器;`boom` 指定哪一个抛。"""
    from app.services import npc_trade_service

    calls: list[str] = []

    def _wire(boom: str | None = None):
        def _make(name: str, result: dict):
            async def _pass(db, *args, **kwargs):
                assert db is not None, f"{name} 必须拿到夜间链开的 session"
                calls.append(name)
                if name == boom:
                    raise RuntimeError(f"{name} exploded")
                return result
            return _pass

        monkeypatch.setattr(npc_trade_service, "run_commission_settle_pass",
                            _make("settle", {"settled": 1, "paid": 8, "reopened": 0}))
        monkeypatch.setattr(npc_trade_service, "run_commission_accept_pass",
                            _make("accept", {"accepted": 1}))
        monkeypatch.setattr(npc_trade_service, "run_consumption_pass",
                            _make("consume", {"bought": 2, "spent": 30, "tax": 1}))
        return calls

    return _wire


@pytest.fixture
def trade_gate(monkeypatch):
    from app.config import settings

    def _set(economy: bool, trade: bool):
        monkeypatch.setattr(settings, "npc_economy_enabled", economy)
        monkeypatch.setattr(settings, "npc_trade_enabled", trade)

    return _set


async def test_gate_on_runs_each_pass_once_in_order(nightly, spy_passes, trade_gate):
    trade_gate(True, True)
    calls = spy_passes()

    await nightly.run_nightly_jobs()

    assert calls == ["settle", "accept", "consume"], (
        f"每个 pass 每晚恰跑一次、按结算→接单→消费的顺序,实测 {calls!r}")


@pytest.mark.parametrize("economy,trade", [(False, True), (True, False), (False, False)])
async def test_gate_off_calls_nothing(nightly, spy_passes, trade_gate, economy, trade):
    trade_gate(economy, trade)
    calls = spy_passes()

    await nightly.run_nightly_jobs()

    assert calls == [], (
        f"npc_economy_enabled={economy} npc_trade_enabled={trade} 时夜间链不得触碰"
        f"三个 pass,实测 {calls!r}")


@pytest.mark.parametrize("boom,before", [
    ("settle", []),
    ("accept", ["settle"]),
    ("consume", ["settle", "accept"]),
])
async def test_a_failing_pass_is_swallowed(nightly, spy_passes, trade_gate, caplog,
                                           boom, before):
    """任一 pass 抛:异常吞在本段,不外溢;本段之前跑完的段落不受影响。"""
    trade_gate(True, True)
    calls = spy_passes(boom=boom)

    with caplog.at_level("ERROR"):
        await nightly.run_nightly_jobs()      # 不得抛

    assert calls == before + [boom], (
        f"{boom} 抛之前的 pass 应已跑完,实测 {calls!r}")
    assert any("M-A" in r.message or "npc trade" in r.message.lower()
               for r in caplog.records if r.levelname == "ERROR"), (
        "失败必须留下 logger.error 痕迹——静默吞掉等于夜间链上悄悄失效")


async def test_summary_is_logged_when_anything_happened(nightly, spy_passes,
                                                        trade_gate, caplog):
    trade_gate(True, True)
    spy_passes()

    with caplog.at_level("INFO"):
        await nightly.run_nightly_jobs()

    assert any("M-A" in r.message for r in caplog.records), (
        "有成交/结算时要有一行摘要日志,否则线上开闸后无从核对")
