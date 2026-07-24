"""World-clock wiring seams (agent-T): the points where real code reads world
time. Each test pins real 'now' via ``app.world_clock.now_real`` (the single
seam every ``world_*`` helper funnels through) and asserts the world-time
semantics of a specific call site.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app import world_clock as wc

SH = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def fixed_real(monkeypatch):
    """Pin real 'now' so every world-time read is deterministic. Returns a setter."""

    def _set(dt: datetime):
        monkeypatch.setattr(wc, "now_real", lambda: dt)

    return _set


# ── decide: "today's actions" is a WORLD-date filter over real created_at ──

@pytest.mark.anyio
async def test_decide_today_actions_uses_world_date(fixed_real):
    """A memory created at the current real instant must count as "today" on the
    world calendar even when its real UTC date differs from the world date — a
    raw ``created_at.strftime`` would compare a real date to a world key and drop
    it. Memories a full world day back are excluded.
    """
    from app.agent.phases.decide.basic import BasicDecidePlugin
    from app.agent.actions import ActionType
    from app.agent.schemas import TickContext

    epoch = wc.world_epoch()
    # Real 07:00 Beijing on epoch day → world 2026-01-02 04:00 (28 world hours in).
    fixed_real(epoch + timedelta(hours=7))
    assert wc.world_date_key() == "2026-01-02"

    # created_at is stored naive-UTC in the DB. This one is "now" in real terms
    # (2025-12-31 23:00 UTC == 2026-01-01 07:00+08): raw strftime → "2025-12-31"
    # (wrong, would be excluded), world map → 2026-01-02 (correct, included).
    now_created = datetime(2025, 12, 31, 23, 0, 0)
    # One full world day earlier (6 real hours back) → world 2026-01-01 → excluded.
    day_ago_created = datetime(2025, 12, 31, 17, 0, 0)

    mem_today = MagicMock(content="今天完成了报告", created_at=now_created)
    mem_yesterday = MagicMock(content="昨天开了会", created_at=day_ago_created)

    resident = MagicMock(id="res-1", slug="r")
    ctx = TickContext(
        db=AsyncMock(), resident=resident, world_time="04:00", hour=4,
        schedule_phase="凌晨", nearby_residents=[],
        available_actions=[ActionType.IDLE],
    )
    ctx.memories = [mem_today, mem_yesterday]
    ctx.world_events = []

    plugin = BasicDecidePlugin(params={})
    with patch("app.agent.phases.decide.basic.build_decision_prompt",
               return_value=("sys", "user")), \
         patch("app.agent.phases.decide.basic.llm_chat",
               new=AsyncMock(return_value='{"action": "IDLE", "target_slug": null, "target_tile": null, "reason": "x"}')):
        await plugin._llm_decide(ctx)

    assert ctx.today_actions == ["今天完成了报告"]

