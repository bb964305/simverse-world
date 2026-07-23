"""Realism P1-7: movement speed modulation + commute-time plan hints."""
import pytest
from unittest.mock import AsyncMock

from app.config import settings


def test_effective_speed_weather_and_arousal():
    from app.agent.phases.execute.basic import _effective_speed
    assert _effective_speed(8, None, None) == 8
    assert _effective_speed(8, "storm", None) == 4         # ×0.5
    assert _effective_speed(8, "rain", None) == 6          # ×0.75
    assert _effective_speed(8, "snow", None) == 5          # ×0.6 → 4.8 → 5
    # arousal > 0.7 → ×1.2 (rounded)
    assert _effective_speed(8, None, 0.9) == 10
    assert _effective_speed(8, None, 0.5) == 8
    # floor at 1
    assert _effective_speed(1, "storm", None) == 1


def test_weather_kind_from_events():
    from app.agent.phases.execute.basic import _weather_kind
    events = [{"type": "weather", "payload_json": {"kind": "storm"}},
              {"type": "festival", "payload_json": {}}]
    assert _weather_kind(events) == "storm"
    assert _weather_kind([]) is None


@pytest.mark.anyio
async def test_execute_walks_up_to_speed(monkeypatch):
    from app.agent.phases.execute.basic import BasicExecutePlugin
    from app.agent.actions import ActionType, ActionResult
    from app.agent.schemas import TickContext
    from app.agent.map_data import get_valid_target_tile
    from app.agent.pathfinder import get_walkable_tiles, find_path
    monkeypatch.setattr(settings, "realism_enabled", True)
    monkeypatch.setattr(settings, "realism_move_speed", 8)

    target = get_valid_target_tile("academy")
    # start somewhere with a path length > speed to academy
    walkable = get_walkable_tiles()
    start = (75, 56)  # central plaza hub, connected
    path = find_path(start, target, walkable)
    assert path and len(path) > 9   # ensure a long path so speed caps it

    from unittest.mock import MagicMock
    r = MagicMock()
    r.tile_x, r.tile_y = start
    r.status = "idle"
    r.mood_json = None
    ctx = TickContext(db=AsyncMock(), resident=r, world_time="10:00", hour=10, schedule_phase="上午")
    ctx.world_events = []
    ctx.action_result = ActionResult(ActionType.VISIT_DISTRICT, "academy", target, "去")
    await BasicExecutePlugin().execute(ctx)

    moved = abs(r.tile_x - start[0]) + abs(r.tile_y - start[1])
    # single-tick manhattan displacement along the path must be ≤ speed
    assert 1 <= moved <= 8


def test_commute_hint_in_location_list(monkeypatch):
    from app.agent.map_data import format_location_list_for_prompt
    monkeypatch.setattr(settings, "realism_enabled", True)
    # far-away origin → non-zero commute minutes appear
    text = format_location_list_for_prompt(from_tile=(0, 0))
    assert "分钟路程" in text
    # no from_tile → legacy format (no commute hint)
    assert "分钟路程" not in format_location_list_for_prompt()
    # realism off → no commute hint even with from_tile
    monkeypatch.setattr(settings, "realism_enabled", False)
    assert "分钟路程" not in format_location_list_for_prompt(from_tile=(0, 0))
