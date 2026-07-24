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


# ── nightly cron: real 24h cadence, Beijing-morning anchor ──

def test_nightly_anchor_is_beijing_morning(fixed_real):
    """The cron keeps a true real-24h cadence but fires at the Beijing morning
    hour (agent-T §5), not UTC 00:30. ``_seconds_until_next_run`` computes the
    delay to the next ``RUN_HOUR``:00 in the anchor zone."""
    from app.tasks import nightly_cron as nc

    assert (nc.RUN_HOUR, nc.RUN_MINUTE) == (7, 0)

    base = datetime(2026, 6, 1, 5, 0, 0, tzinfo=SH)  # 05:00 Beijing
    # Next 07:00 is 2h out.
    assert nc._seconds_until_next_run(base) == pytest.approx(2 * 3600)
    # 08:00 Beijing → already past 07:00 → next is tomorrow 07:00 (23h out).
    later = datetime(2026, 6, 1, 8, 0, 0, tzinfo=SH)
    assert nc._seconds_until_next_run(later) == pytest.approx(23 * 3600)


# ── nightly cron: world-week gate fires once per world week ──

@pytest.mark.anyio
async def test_world_week_gate_same_week_then_cross(fixed_real):
    """Two real runs inside one world week → gate passes once; a run that crosses
    into the next world week passes again. A world week is 7 world days = 42 real
    hours at k=4, so equality on real weekday would misfire (agent-T §5)."""
    from app.tasks import nightly_cron as nc

    epoch = wc.world_epoch()
    key = "sv:nightly:test_week"

    # First run in world week 0 → passes, records ordinal 0.
    fixed_real(epoch)
    assert wc.world_week_index() == 0
    assert await nc._world_week_gate(key) is True

    # Another run ~1 real day later, still world week 0 (24 real h = 4 world days
    # < 7) → does NOT pass again.
    fixed_real(epoch + timedelta(hours=24))
    assert wc.world_week_index() == 0
    assert await nc._world_week_gate(key) is False

    # A run past 42 real hours → world week 1 → passes again.
    fixed_real(epoch + timedelta(hours=42) + timedelta(minutes=1))
    assert wc.world_week_index() == 1
    assert await nc._world_week_gate(key) is True
    # Immediately re-checking the same world week does not re-fire.
    assert await nc._world_week_gate(key) is False


# ── tick: daily-action cap key resets per WORLD day ──

def test_tick_daily_key_resets_per_world_day(fixed_real):
    """The per-resident daily-action cap key is stamped with the WORLD date
    (agent-T §4, Jimmy's call: 20 actions per world day). Two real instants in
    the SAME real day but DIFFERENT world days must yield different keys, so the
    quota resets at world midnight (every 6 real hours at k=4)."""
    from app.agent.tick import _daily_key

    epoch = wc.world_epoch()  # real 2026-01-01 00:00 Beijing == world midnight
    fixed_real(epoch + timedelta(hours=1))  # world 2026-01-01 04:00
    key_early = _daily_key("res-1")
    # +6 real hours = +1 world day, still the SAME real calendar day (07:00 real).
    fixed_real(epoch + timedelta(hours=7))  # world 2026-01-02 04:00
    key_late = _daily_key("res-1")

    assert key_early == "sv:daily_actions:2026-01-01:res-1"
    assert key_late == "sv:daily_actions:2026-01-02:res-1"
    assert key_early != key_late  # same real day, different world day → reset

