"""Realism P0-1: plan/decision target resolution + truthful move memories."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import settings
from app.agent.plan_target import resolve_location_id, resolve_target_tile
from app.agent.map_data import get_location_id_by_name, get_valid_target_tile, LOCATIONS
from app.agent.actions import ActionType, ActionResult
from app.agent.schemas import TickContext, HourlyPlan


# ── resolver (pure) ────────────────────────────────────────────────────

def test_resolve_by_slug():
    assert resolve_target_tile("academy", None) == get_valid_target_tile("academy")


def test_resolve_by_location_name_fallback():
    name = LOCATIONS["academy"]["name"]  # "学院"
    assert resolve_target_tile(None, name) == get_valid_target_tile("academy")


def test_legacy_coordinate_target_resolves_to_canonical_id_from_location():
    name = LOCATIONS["academy"]["name"]
    assert resolve_location_id([10, 20], name) == "academy"


def test_legacy_display_name_target_resolves_without_location_field():
    assert resolve_location_id(LOCATIONS["academy"]["name"], None) == "academy"


def test_ignore_model_reported_coords():
    # coord-shaped / unknown slug is not a location id → ignored; name None → None
    assert resolve_target_tile("[10, 20]", None) is None
    assert resolve_target_tile("999,999", "不存在的地方") is None
    # a real list (unhashable) must not crash and must be ignored
    assert resolve_target_tile([10, 20], None) is None


def test_slug_tried_as_name_too():
    # decide path passes target_slug as both slug and name candidate
    name = LOCATIONS["academy"]["name"]
    assert resolve_target_tile("not-a-slug", name) == get_valid_target_tile("academy")


def test_get_location_id_by_name():
    assert get_location_id_by_name(LOCATIONS["academy"]["name"]) == "academy"
    assert get_location_id_by_name("查无此地") is None
    assert get_location_id_by_name(None) is None


# ── phase test helpers (mirror tests/test_agent_phases.py) ──────────────

def _make_resident(**over):
    r = MagicMock()
    r.id = "res-1"
    r.slug = "test-resident"
    r.name = "Test Resident"
    r.status = "idle"
    r.tile_x = 10
    r.tile_y = 10
    r.home_tile_x = 5
    r.home_tile_y = 5
    r.home_location_id = None
    r.mood_json = None
    for k, v in over.items():
        setattr(r, k, v)
    return r


def _make_ctx(**over):
    ctx = TickContext(
        db=AsyncMock(),
        resident=_make_resident(),
        world_time="10:00",
        hour=10,
        schedule_phase="上午",
    )
    for k, v in over.items():
        setattr(ctx, k, v)
    return ctx


# ── decide: server-side target_tile resolution ─────────────────────────

@pytest.mark.anyio
async def test_plan_move_resolves_tile_when_realism_on(monkeypatch):
    from app.agent.phases.decide.basic import BasicDecidePlugin
    monkeypatch.setattr(settings, "realism_enabled", True)
    ctx = _make_ctx()
    ctx.current_plan = HourlyPlan(
        slot=0, hour_range=(9, 12), action="VISIT_DISTRICT",
        target="academy", location="学院", importance=8, reason="去学习",
        status="pending",
    )
    ctx.available_actions = [ActionType.VISIT_DISTRICT]
    with patch("app.agent.phases.decide.basic.MemoryService") as MockMS:
        MockMS.return_value = AsyncMock(get_memories=AsyncMock(return_value=[]))
        ctx = await plugin_execute(BasicDecidePlugin(params={"interrupt_threshold": 6}), ctx)
    assert ctx.action_result.action == ActionType.VISIT_DISTRICT
    assert ctx.action_result.target_tile == get_valid_target_tile("academy")


@pytest.mark.anyio
async def test_legacy_plan_coordinate_becomes_canonical_action_target(monkeypatch):
    from app.agent.phases.decide.basic import BasicDecidePlugin
    monkeypatch.setattr(settings, "realism_enabled", True)
    ctx = _make_ctx()
    ctx.current_plan = HourlyPlan(
        slot=0, hour_range=(9, 12), action="VISIT_DISTRICT",
        target=[10, 20], location="学院", importance=8, reason="去学习",
        status="pending",
    )
    with patch("app.agent.phases.decide.basic.MemoryService") as MockMS:
        MockMS.return_value = AsyncMock(get_memories=AsyncMock(return_value=[]))
        ctx = await BasicDecidePlugin(params={"interrupt_threshold": 6}).execute(ctx)
    assert ctx.action_result.target_slug == "academy"
    assert ctx.action_result.target_tile == get_valid_target_tile("academy")


@pytest.mark.anyio
async def test_plan_move_target_none_when_realism_off(monkeypatch):
    from app.agent.phases.decide.basic import BasicDecidePlugin
    monkeypatch.setattr(settings, "realism_enabled", False)
    ctx = _make_ctx()
    ctx.current_plan = HourlyPlan(
        slot=0, hour_range=(9, 12), action="VISIT_DISTRICT",
        target="academy", location="学院", importance=8, reason="去学习",
        status="pending",
    )
    ctx.available_actions = [ActionType.VISIT_DISTRICT]
    with patch("app.agent.phases.decide.basic.MemoryService") as MockMS:
        MockMS.return_value = AsyncMock(get_memories=AsyncMock(return_value=[]))
        ctx = await plugin_execute(BasicDecidePlugin(params={"interrupt_threshold": 6}), ctx)
    assert ctx.action_result.target_tile is None


async def plugin_execute(plugin, ctx):
    return await plugin.execute(ctx)


# ── memorize: truthful text + move breadcrumb ──────────────────────────

@pytest.mark.anyio
async def test_memorize_no_phantom_move_when_unreachable(monkeypatch):
    from app.agent.phases.memorize.basic import BasicMemorizePlugin
    monkeypatch.setattr(settings, "realism_enabled", True)
    ctx = _make_ctx()
    ctx.action_result = ActionResult(ActionType.VISIT_DISTRICT, "academy", None, "去")
    ctx.resident.status = "idle"          # never moved
    ctx.resident.tile_x, ctx.resident.tile_y = 10, 10  # not at academy entrance
    ctx.new_tile = None
    add = AsyncMock()
    with patch("app.agent.phases.memorize.basic.MemoryService") as MockMS:
        MockMS.return_value = AsyncMock(add_memory=add)
        await BasicMemorizePlugin().execute(ctx)
    content = add.call_args.kwargs["content"]
    assert "前往" not in content and "到达" not in content
    meta = add.call_args.kwargs["metadata_json"]
    assert meta["move"]["moved"] is False and meta["move"]["arrived"] is False


@pytest.mark.anyio
async def test_memorize_arrived_text_and_breadcrumb(monkeypatch):
    from app.agent.phases.memorize.basic import BasicMemorizePlugin
    monkeypatch.setattr(settings, "realism_enabled", True)
    entrance = get_valid_target_tile("academy")
    ctx = _make_ctx()
    ctx.action_result = ActionResult(ActionType.VISIT_DISTRICT, "academy", entrance, "去")
    ctx.resident.status = "walking"
    ctx.resident.tile_x, ctx.resident.tile_y = entrance[0], entrance[1]  # at target
    ctx.new_tile = entrance
    add = AsyncMock()
    with patch("app.agent.phases.memorize.basic.MemoryService") as MockMS:
        MockMS.return_value = AsyncMock(add_memory=add)
        await BasicMemorizePlugin().execute(ctx)
    content = add.call_args.kwargs["content"]
    assert "到达了" in content
    assert add.call_args.kwargs["metadata_json"]["move"]["arrived"] is True


@pytest.mark.anyio
async def test_memorize_enroute_text(monkeypatch):
    from app.agent.phases.memorize.basic import BasicMemorizePlugin
    monkeypatch.setattr(settings, "realism_enabled", True)
    entrance = get_valid_target_tile("academy")
    ctx = _make_ctx()
    ctx.action_result = ActionResult(ActionType.VISIT_DISTRICT, "academy", entrance, "去")
    ctx.resident.status = "walking"
    ctx.resident.tile_x, ctx.resident.tile_y = 10, 10   # moved but not yet at target
    ctx.new_tile = (11, 10)
    add = AsyncMock()
    with patch("app.agent.phases.memorize.basic.MemoryService") as MockMS:
        MockMS.return_value = AsyncMock(add_memory=add)
        await BasicMemorizePlugin().execute(ctx)
    content = add.call_args.kwargs["content"]
    assert "正在前往" in content
    assert add.call_args.kwargs["metadata_json"]["move"]["moved"] is True


@pytest.mark.anyio
async def test_memorize_legacy_plan_writes_canonical_plan_and_move_metadata(monkeypatch):
    from app.agent.phases.memorize.basic import BasicMemorizePlugin
    monkeypatch.setattr(settings, "realism_enabled", True)
    entrance = get_valid_target_tile("academy")
    ctx = _make_ctx()
    plan = HourlyPlan(
        slot=2, hour_range=(9, 12), action="VISIT_DISTRICT",
        target=[10, 20], location="学院", importance=4, reason="去学习",
    )
    ctx.current_plan = plan
    ctx.scheduled_plan = plan
    ctx.plan_date = "2028-06-08"
    ctx.plan_followed = True
    ctx.action_result = ActionResult(
        ActionType.VISIT_DISTRICT, "academy", entrance, "去学习")
    ctx.resident.status = "walking"
    add = AsyncMock()
    with patch("app.agent.phases.memorize.basic.MemoryService") as MockMS:
        MockMS.return_value = AsyncMock(add_memory=add)
        await BasicMemorizePlugin().execute(ctx)
    meta = add.call_args.kwargs["metadata_json"]
    assert meta["move"]["target"] == "academy"
    assert meta["move"]["planned"] is True
    assert meta["plan"] == {
        "date": "2028-06-08", "slot": 2,
        "scheduled_action": "VISIT_DISTRICT",
        "scheduled_target": "academy", "followed": True,
        "interrupt_reason": None,
    }
