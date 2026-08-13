"""M-A C4 接线 — 集市日开场触发外来商队进镇(event_cron 钩子)。

Plan: `docs/plans/2026-08-09-M-A-npc-economy.md` Step 10;spec §4 C4「触发」。

`run_caravan_visit` 写得再对,没人调就是死代码(同 test_nightly_npc_trade.py 钉
nightly #23 的那类故障)。这里钉四件事:

1. **判据在调用点**:只有 `payload_json.market_day` 的事件、只在 `phase=="start"`
   才进镇(与 `shop_service._market_discount` 同源判据)。
2. **双闸在调用之前**:`npc_economy_enabled and caravan_enabled` 关 = 连
   `caravan_service` 都不 import,零 DB 触碰。
3. **顺序**:排在 `write_collective_memories` 之后——先让居民知道今天是集市日,
   商队才进场。
4. **自带兜底**:这一轮的 session 是 C3 脚本段/E3 辩论段共享的,商队炸了必须就地
   `db.rollback()`(否则 PendingRollbackError 会顺着 session 传染,报错还会被误算
   到 C3/E3 头上——:69-77 的注释就是上一次踩过的坑),且同轮 C3/E3 照常跑完。

驱动一轮 loop 的姿势沿用 test_world_events.py:143:`asyncio.sleep` 抛
`CancelledError` 收尾,session 用 MagicMock(本段不验资金,资金面在 test_caravan.py)。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings

pytestmark = pytest.mark.anyio

# `flip_active_events` 交出来的事件形态(world_event_service.py:24-33)。
MARKET = {"id": "evt-market-001", "title": "集市日",
          "payload_json": {"market_day": True}}
PLAIN = {"id": "evt-rain-001", "title": "落雨", "payload_json": {}}
VISIT = {"bought": 1, "spent": 15, "tax": 0, "fee": 5, "imported": 3}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def caravan_gate(monkeypatch):
    def _set(economy: bool, caravan: bool):
        monkeypatch.setattr(settings, "npc_economy_enabled", economy)
        monkeypatch.setattr(settings, "caravan_enabled", caravan)
        monkeypatch.setattr(settings, "caravan_lifecycle_enabled", False)

    return _set


@pytest.fixture
def one_pass(monkeypatch):
    """跑 `event_cron_loop` 恰一轮,返回本轮的调用序 + 商队拿到的实参。"""
    from app.services import (
        caravan_lifecycle_service, caravan_service, debate_service, script_service,
    )
    from app.tasks import event_cron, weather

    async def _run(changes, *, boom: bool = False):
        calls: list[str] = []
        seen: list[tuple] = []
        db = MagicMock()

        async def _rollback():
            calls.append("rollback")

        db.rollback = _rollback

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=db)
        cm.__aexit__ = AsyncMock(return_value=False)

        def _spy(name, result=None):
            async def _fn(*args, **kwargs):
                calls.append(name)
                return result
            return _fn

        async def _visit(visit_db, event):
            calls.append("caravan")
            seen.append((visit_db, event))
            if boom:
                raise RuntimeError("caravan exploded")
            return VISIT

        monkeypatch.setattr(event_cron, "async_session", lambda: cm)
        monkeypatch.setattr(event_cron, "flip_active_events",
                            _spy("flip", list(changes)))
        monkeypatch.setattr(event_cron, "write_collective_memories",
                            _spy("memories", 1))
        monkeypatch.setattr(event_cron, "beat", _spy("beat"))
        monkeypatch.setattr(event_cron.manager, "broadcast", _spy("broadcast"))
        monkeypatch.setattr(weather, "ensure_weather_event", _spy("weather"))
        monkeypatch.setattr(caravan_service, "run_caravan_visit", _visit)
        monkeypatch.setattr(
            caravan_lifecycle_service, "ensure_visit_for_event",
            _spy("caravan_wake"),
        )
        monkeypatch.setattr(
            caravan_lifecycle_service, "wake_visit_for_event",
            _spy("caravan_close_wake"),
        )
        monkeypatch.setattr(script_service, "fire_due_scripts", _spy("c3_fire", []))
        monkeypatch.setattr(script_service, "settle_due_seasons", _spy("c3_settle", []))
        monkeypatch.setattr(script_service, "ensure_active_season", _spy("c3_open"))
        monkeypatch.setattr(debate_service, "drive_due_debates",
                            _spy("e3", {"live": 0, "settled": 0, "refunded": 0}))

        with patch("app.tasks.event_cron.asyncio.sleep",
                   AsyncMock(side_effect=asyncio.CancelledError())):
            with pytest.raises(asyncio.CancelledError):
                await event_cron.event_cron_loop()
        return calls, seen, db

    return _run


def test_event_cron_reads_binding_caravan_policy():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1]
              / "app" / "tasks" / "event_cron.py").read_text()
    assert "is_caravan_enabled" in source
    assert source.index("is_caravan_enabled") < source.index("run_caravan_visit(db, event)")


async def test_market_day_start_runs_the_visit_once(one_pass, caravan_gate):
    caravan_gate(True, True)

    calls, seen, db = await one_pass([(MARKET, "start")])

    assert calls.count("caravan") == 1, (
        f"集市日开场必须恰好触发一次商队到访,实测调用序 {calls!r}")
    visit_db, event = seen[0]
    assert visit_db is db, "商队要复用本轮 event_cron 的 session,不许另开"
    assert event is MARKET, "整个事件 dict 原样传下去(服务按 id 做 at-most-once)"


async def test_lifecycle_gate_wakes_state_machine_instead_of_legacy_settlement(
    one_pass, caravan_gate, monkeypatch,
):
    caravan_gate(True, True)
    monkeypatch.setattr(settings, "caravan_lifecycle_enabled", True)

    calls, seen, _ = await one_pass([(MARKET, "start")])

    assert calls.count("caravan_wake") == 1
    assert "caravan" not in calls
    assert seen == []


async def test_visit_runs_after_collective_memories(one_pass, caravan_gate):
    """先让居民知道今天是集市日,商队才进场——插点就在 :36-42 块的后面。"""
    caravan_gate(True, True)

    calls, _, _ = await one_pass([(MARKET, "start")])

    assert calls.index("memories") < calls.index("caravan"), (
        f"商队到访必须排在 write_collective_memories 之后,实测 {calls!r}")


@pytest.mark.parametrize("economy,caravan", [(False, True), (True, False), (False, False)])
async def test_gate_off_never_calls_the_visit(one_pass, caravan_gate, economy, caravan):
    caravan_gate(economy, caravan)

    calls, seen, _ = await one_pass([(MARKET, "start")])

    assert "caravan" not in calls, (
        f"npc_economy_enabled={economy} caravan_enabled={caravan} 时不许触碰商队,"
        f"实测 {calls!r}")
    assert seen == []


async def test_non_market_day_start_is_ignored(one_pass, caravan_gate):
    caravan_gate(True, True)

    calls, _, _ = await one_pass([(PLAIN, "start")])

    assert "caravan" not in calls, (
        f"判据是 payload_json.market_day,普通事件开场商队不进镇,实测 {calls!r}")


async def test_market_day_end_is_ignored(one_pass, caravan_gate):
    """集市散场不是第二次到访——否则同一个集市日会被收两次摊位费。"""
    caravan_gate(True, True)

    calls, _, _ = await one_pass([(MARKET, "end")])

    assert "caravan" not in calls, f"phase=='end' 不许触发到访,实测 {calls!r}"


async def test_market_day_end_wakes_lifecycle_for_safe_departure(
    one_pass, caravan_gate, monkeypatch,
):
    caravan_gate(True, True)
    monkeypatch.setattr(settings, "caravan_lifecycle_enabled", True)

    calls, _, _ = await one_pass([(MARKET, "end")])

    assert calls.count("caravan_close_wake") == 1
    assert "caravan" not in calls


async def test_visit_failure_is_swallowed_with_rollback(one_pass, caravan_gate, caplog):
    """商队炸掉:异常吞在本段 + 就地 rollback + 同轮 C3/E3 照常跑完。"""
    caravan_gate(True, True)

    with caplog.at_level("WARNING"):
        calls, _, _ = await one_pass([(MARKET, "start")], boom=True)

    assert calls.index("rollback") > calls.index("caravan"), (
        f"共享 session 写了半截必须就地 rollback,否则 PendingRollbackError 会传染"
        f"给 C3/E3。实测 {calls!r}")
    for name in ("c3_fire", "c3_settle", "c3_open", "e3"):
        assert name in calls, (
            f"商队失败不许连坐同轮的 C3/E3({name} 没跑),实测 {calls!r}")
    assert "broadcast" in calls, "事件广播也不该被商队拖死"
    assert any("caravan" in r.message.lower() for r in caplog.records
               if r.levelname == "WARNING"), "失败必须留下 logger.warning 痕迹"
