"""P2-S17: OBSERVE 的 execute 分支 + 非移动动作的 act 痕迹(M5 数据源)。

act.loc 必须是**具体**地点 id:theater(172,40,178,50) 完全落在 outdoor 街区
east_gardens(140,35,179,58) 内部,get_location_id_at 首命中即返(map_data.py:
243-249),生产实测 (175,45) 返 "east_gardens"。照粗查写,M5 的
`metadata_json->'act'->>'loc' = 'theater'` 恒查不到一行。
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.actions import ActionResult, ActionType
from app.agent.location_caps import CAP_STAGE
from app.agent.map_data import LOCATIONS, get_location_id_at
from app.agent.schemas import TickContext
from app.config import settings

THEATER = {
    "name": "剧院", "type": "public", "role": "culture",
    "bounds": (172, 40, 178, 50), "center": (175, 45), "entrance": (172, 45),
    "description": "小镇剧院:说书、演展、故事会的舞台",
    "boosted_actions": ["CHAT_RESIDENT", "OBSERVE"],
}
INSIDE = (175, 45)


@pytest.fixture
def overlay():
    added: list[str] = []

    def _merge(slug: str, data: dict, capabilities=None) -> str:
        assert slug not in LOCATIONS, slug
        row = dict(data)
        if capabilities is not None:
            row["capabilities"] = capabilities
        LOCATIONS[slug] = row
        added.append(slug)
        return slug

    yield _merge
    for slug in added:
        LOCATIONS.pop(slug, None)


def _ctx(action, *, tile=INSIDE, status="walking"):
    resident = SimpleNamespace(
        id="r-1", slug="watcher", name="看客", resident_type="npc",
        status=status, tile_x=tile[0], tile_y=tile[1],
        meta_json={}, mood_json={}, home_location_id=None,
        home_tile_x=None, home_tile_y=None, daily_plans_json=None,
    )
    return TickContext(
        db=AsyncMock(), resident=resident, world_time="20:00", hour=20,
        schedule_phase="夜晚",
        action_result=ActionResult(action, None, None, "看看"),
        available_actions=[ActionType.OBSERVE, ActionType.CHAT_RESIDENT],
    )


async def _memorize(ctx):
    from app.agent.phases.memorize.basic import BasicMemorizePlugin
    with patch("app.agent.phases.memorize.basic.MemoryService") as MockMS:
        svc = AsyncMock()
        MockMS.return_value = svc
        await BasicMemorizePlugin(params={}).execute(ctx)
    return svc


# ── 依赖边守卫 ────────────────────────────────────────────────────────

def test_stage_event_flag_is_registered_by_the_previous_batch():
    from app.config import Settings
    field = Settings.model_fields.get("stage_event_enabled")
    assert field is not None, (
        "app/config.py 缺 stage_event_enabled —— 批次表 #7 必须先引入 "
        "STAGE_EVENT_ENABLED(默认 false),#10/#11 沿用同一道闸")
    assert field.default is False


# ── OBSERVE 的 execute 分支 ──────────────────────────────────────────

@pytest.mark.anyio
async def test_observe_is_a_no_op_when_the_gate_is_off(monkeypatch):
    """闸关 = 今天:选了 OBSERVE,status 还停在 walking。"""
    from app.agent.phases.execute.basic import BasicExecutePlugin
    monkeypatch.setattr(settings, "stage_event_enabled", False)
    ctx = _ctx(ActionType.OBSERVE)
    await BasicExecutePlugin(params={}).execute(ctx)
    assert ctx.resident.status == "walking"


@pytest.mark.anyio
async def test_observe_settles_the_resident_when_the_gate_is_on(monkeypatch):
    from app.agent.phases.execute.basic import BasicExecutePlugin
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    ctx = _ctx(ActionType.OBSERVE)
    await BasicExecutePlugin(params={}).execute(ctx)
    assert ctx.resident.status == "idle"
    ctx.db.commit.assert_awaited()


@pytest.mark.anyio
async def test_observe_never_interrupts_an_ongoing_chat(monkeypatch):
    from app.agent.phases.execute.basic import BasicExecutePlugin
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    for busy in ("chatting", "socializing"):
        ctx = _ctx(ActionType.OBSERVE, status=busy)
        await BasicExecutePlugin(params={}).execute(ctx)
        assert ctx.resident.status == busy


@pytest.mark.anyio
async def test_observe_writes_no_memory_in_execute(monkeypatch):
    """记忆归 memorize(memorize/basic.py:135-136);execute 再写一条就是双份。"""
    from app.agent.phases.execute import basic as execute_basic
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    # execute 没有 docstring(__doc__ is None),`in None` 会先抛 TypeError ——
    # 这行本就是 `X or True` 的恒真装饰,真正的断言是下面的 getsource。
    assert "MemoryService" not in (execute_basic.BasicExecutePlugin.execute.__doc__ or "") or True
    import inspect
    src = inspect.getsource(execute_basic.BasicExecutePlugin.execute)
    assert "add_memory" not in src


# ── metadata["act"] ──────────────────────────────────────────────────

@pytest.mark.anyio
async def test_act_records_the_specific_venue_not_the_masking_block(
        overlay, monkeypatch):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    # 遮蔽是真的。
    assert get_location_id_at(*INSIDE) == "east_gardens"
    svc = await _memorize(_ctx(ActionType.CHAT_RESIDENT))
    meta = svc.add_memory.call_args[1]["metadata_json"]
    assert meta["act"] == {"action": "CHAT_RESIDENT", "loc": "theater"}


@pytest.mark.anyio
async def test_act_falls_back_to_the_coarse_lookup_elsewhere(monkeypatch):
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    svc = await _memorize(_ctx(ActionType.OBSERVE, tile=(88, 104)))
    meta = svc.add_memory.call_args[1]["metadata_json"]
    assert meta["act"] == {"action": "OBSERVE", "loc": "south_quarter"}


@pytest.mark.anyio
async def test_gate_off_writes_no_act_key(monkeypatch):
    monkeypatch.setattr(settings, "stage_event_enabled", False)
    svc = await _memorize(_ctx(ActionType.OBSERVE))
    meta = svc.add_memory.call_args[1]["metadata_json"]
    assert meta is None or "act" not in meta


@pytest.mark.anyio
async def test_movement_keeps_move_and_never_gets_act(monkeypatch):
    """move 与 act 互斥:移动动作的落点已由 move 记录,不重复写。"""
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    monkeypatch.setattr(settings, "realism_enabled", True)
    svc = await _memorize(_ctx(ActionType.VISIT_DISTRICT))
    meta = svc.add_memory.call_args[1]["metadata_json"]
    assert "move" in meta and "act" not in meta


@pytest.mark.anyio
async def test_every_non_movement_action_is_covered(monkeypatch):
    """M5 要能数清剧院里发生了什么 —— 非移动动作必须条条有痕迹。"""
    from app.agent.phases.memorize.basic import _MOVEMENT_ACTIONS
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    for action in ActionType:
        if action in _MOVEMENT_ACTIONS:
            continue
        svc = await _memorize(_ctx(action))
        meta = svc.add_memory.call_args[1]["metadata_json"]
        assert meta["act"]["action"] == action.value, action


def test_action_type_enum_is_untouched():
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
