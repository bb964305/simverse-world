"""Tests for agent phase plugins."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.agent.actions import ActionType
from app.agent.schemas import TickContext, HourlyPlan


def _make_resident(slug="test-resident"):
    r = MagicMock()
    r.id = "res-1"
    r.slug = slug
    r.name = "Test Resident"
    r.district = "engineering"
    r.status = "idle"
    r.tile_x = 10
    r.tile_y = 10
    r.home_tile_x = 5
    r.home_tile_y = 5
    r.meta_json = {"sbti": {"type": "GOGO", "type_name": "行者", "dimensions": {
        "S1": "H", "S2": "H", "S3": "M",
        "E1": "H", "E2": "M", "E3": "H",
        "A1": "M", "A2": "M", "A3": "H",
        "Ac1": "H", "Ac2": "H", "Ac3": "H",
        "So1": "M", "So2": "H", "So3": "M",
    }}}
    return r


def _make_ctx():
    db = AsyncMock()
    resident = _make_resident()
    ctx = TickContext(
        db=db,
        resident=resident,
        world_time="10:00",
        hour=10,
        schedule_phase="上午",
        nearby_residents=[],
        current_plan=None,
        available_actions=[ActionType.WORK, ActionType.IDLE, ActionType.WANDER, ActionType.OBSERVE],
    )
    return ctx


# ── Perceive Tests ───────────────────────────────────────────────────

@pytest.mark.anyio
async def test_basic_perceive_finds_nearby():
    from app.agent.phases.perceive.basic import BasicPerceivePlugin

    resident = _make_resident("self")
    resident.tile_x = 76
    resident.tile_y = 50
    nearby_r = _make_resident("nearby")
    nearby_r.tile_x = 80
    nearby_r.tile_y = 50
    nearby_r.id = "id-nearby"
    far_r = _make_resident("far")
    far_r.tile_x = 100
    far_r.tile_y = 100
    far_r.id = "id-far"

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [nearby_r, far_r]
    db.execute = AsyncMock(return_value=result_mock)

    plugin = BasicPerceivePlugin(params={"radius": 10})
    ctx = _make_ctx()
    ctx.db = db
    ctx.resident = resident
    ctx = await plugin.execute(ctx)

    assert len(ctx.nearby_residents) == 1
    assert ctx.nearby_residents[0].slug == "nearby"


@pytest.mark.anyio
async def test_basic_perceive_custom_radius():
    from app.agent.phases.perceive.basic import BasicPerceivePlugin

    resident = _make_resident("self")
    resident.tile_x = 76
    resident.tile_y = 50
    nearby_r = _make_resident("nearby")
    nearby_r.tile_x = 80
    nearby_r.tile_y = 50
    nearby_r.id = "id-nearby"

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [nearby_r]
    db.execute = AsyncMock(return_value=result_mock)

    plugin = BasicPerceivePlugin(params={"radius": 3})  # dist=4 > 3
    ctx = _make_ctx()
    ctx.db = db
    ctx.resident = resident
    ctx = await plugin.execute(ctx)

    assert len(ctx.nearby_residents) == 0


# ── Decide Tests ─────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_basic_decide_force_executes_high_importance_plan():
    from app.agent.phases.decide.basic import BasicDecidePlugin

    ctx = _make_ctx()
    ctx.current_plan = HourlyPlan(
        slot=3, hour_range=(9, 12), action="WORK",
        target=None, location="office", importance=7,
        reason="重要工作", status="pending",
    )
    ctx.available_actions = [ActionType.WORK, ActionType.IDLE, ActionType.WANDER]

    with patch("app.agent.phases.decide.basic.MemoryService") as MockMS:
        mock_svc = AsyncMock()
        mock_svc.get_memories = AsyncMock(return_value=[])
        MockMS.return_value = mock_svc

        plugin = BasicDecidePlugin(params={"interrupt_threshold": 6, "plan_adherence_hint": True})
        ctx = await plugin.execute(ctx)

    assert ctx.action_result is not None
    assert ctx.action_result.action == ActionType.WORK
    assert ctx.plan_followed is True
    assert ctx.current_plan.status == "executing"


@pytest.mark.anyio
async def test_basic_decide_low_importance_calls_llm():
    from app.agent.phases.decide.basic import BasicDecidePlugin

    ctx = _make_ctx()
    ctx.current_plan = HourlyPlan(
        slot=0, hour_range=(7, 9), action="IDLE",
        target=None, location="home", importance=3,
        reason="早起休息", status="pending",
    )
    ctx.available_actions = [ActionType.IDLE, ActionType.WANDER, ActionType.OBSERVE]

    with patch("app.agent.phases.decide.basic.llm_chat") as mock_llm, \
         patch("app.agent.phases.decide.basic.MemoryService") as MockMS:
        mock_llm.return_value = '{"action": "WANDER", "target_slug": null, "target_tile": [80, 50], "reason": "出去走走"}'
        mock_svc = AsyncMock()
        mock_svc.get_memories = AsyncMock(return_value=[])
        MockMS.return_value = mock_svc

        plugin = BasicDecidePlugin(params={"interrupt_threshold": 6, "plan_adherence_hint": True})
        ctx = await plugin.execute(ctx)

    assert ctx.action_result is not None
    assert ctx.action_result.action == ActionType.WANDER
    assert ctx.plan_followed is False
    assert ctx.current_plan.status == "interrupted"


@pytest.mark.anyio
async def test_basic_decide_no_plan_calls_llm():
    from app.agent.phases.decide.basic import BasicDecidePlugin

    ctx = _make_ctx()
    ctx.current_plan = None
    ctx.available_actions = [ActionType.IDLE, ActionType.WANDER]

    with patch("app.agent.phases.decide.basic.llm_chat") as mock_llm, \
         patch("app.agent.phases.decide.basic.MemoryService") as MockMS:
        mock_llm.return_value = '{"action": "IDLE", "target_slug": null, "target_tile": null, "reason": "发呆"}'
        mock_svc = AsyncMock()
        mock_svc.get_memories = AsyncMock(return_value=[])
        MockMS.return_value = mock_svc

        plugin = BasicDecidePlugin(params={"interrupt_threshold": 6, "plan_adherence_hint": True})
        ctx = await plugin.execute(ctx)

    assert ctx.action_result is not None
    assert ctx.action_result.action == ActionType.IDLE


# ── Decide skip lever (E-09/E-10) ────────────────────────────────────

def _decide_ctx_with_plan(action="IDLE", importance=3):
    ctx = _make_ctx()
    ctx.current_plan = HourlyPlan(
        slot=0, hour_range=(9, 11), action=action, target=None,
        location="home", importance=importance, reason="按计划", status="pending",
    )
    ctx.available_actions = [ActionType.IDLE, ActionType.WANDER, ActionType.OBSERVE]
    return ctx


@pytest.mark.anyio
async def test_decide_skip_follows_plan_without_llm():
    """skip_decide_when_planned + fresh plan + no interrupt -> no LLM, plan executed."""
    from app.agent.phases.decide.basic import BasicDecidePlugin
    ctx = _decide_ctx_with_plan()
    with patch("app.agent.phases.decide.basic.llm_chat") as mock_llm, \
         patch("app.agent.phases.decide.basic.MemoryService") as MockMS:
        MockMS.return_value = AsyncMock(get_memories=AsyncMock(return_value=[]))
        plugin = BasicDecidePlugin(params={"skip_decide_when_planned": True})
        ctx = await plugin.execute(ctx)
    mock_llm.assert_not_called()
    assert ctx.action_result is not None
    assert ctx.action_result.action == ActionType.IDLE
    assert ctx.plan_followed is True
    assert ctx.current_plan.status == "executing"


@pytest.mark.anyio
async def test_decide_skip_interrupts_on_social_opportunity():
    """A nearby available partner (CHAT_RESIDENT offered) + non-social plan -> LLM."""
    from app.agent.phases.decide.basic import BasicDecidePlugin
    ctx = _decide_ctx_with_plan()
    neighbor = _make_resident("neighbor")
    neighbor.id = "other-res"
    neighbor.status = "idle"
    ctx.nearby_residents = [neighbor]  # execute() recomputes available_actions -> +CHAT_RESIDENT
    with patch("app.agent.phases.decide.basic.llm_chat") as mock_llm, \
         patch("app.agent.phases.decide.basic.MemoryService") as MockMS:
        mock_llm.return_value = '{"action": "IDLE", "target_slug": null, "target_tile": null, "reason": "x"}'
        MockMS.return_value = AsyncMock(get_memories=AsyncMock(return_value=[]))
        plugin = BasicDecidePlugin(params={"skip_decide_when_planned": True})
        ctx = await plugin.execute(ctx)
    mock_llm.assert_awaited()


@pytest.mark.anyio
async def test_decide_skip_interrupts_on_fresh_high_importance_memory():
    """The newest event memory being high-importance -> re-decide with LLM."""
    from app.agent.phases.decide.basic import BasicDecidePlugin
    ctx = _decide_ctx_with_plan()
    hot_mem = MagicMock()
    hot_mem.importance = 0.9
    with patch("app.agent.phases.decide.basic.llm_chat") as mock_llm, \
         patch("app.agent.phases.decide.basic.MemoryService") as MockMS:
        mock_llm.return_value = '{"action": "WANDER", "target_slug": null, "target_tile": null, "reason": "x"}'
        MockMS.return_value = AsyncMock(get_memories=AsyncMock(return_value=[hot_mem]))
        plugin = BasicDecidePlugin(params={"skip_decide_when_planned": True})
        ctx = await plugin.execute(ctx)
    mock_llm.assert_awaited()


@pytest.mark.anyio
async def test_decide_force_plan_only_never_interrupts():
    """force_plan_only (budget 95%+) hard-follows the plan even with a social
    opportunity present, and even if skip_decide_when_planned is off."""
    from app.agent.phases.decide.basic import BasicDecidePlugin
    ctx = _decide_ctx_with_plan()
    neighbor = _make_resident("neighbor")
    neighbor.id = "other-res"
    neighbor.status = "idle"
    ctx.nearby_residents = [neighbor]  # social opportunity present, but budget forces plan-only
    ctx.force_plan_only = True
    with patch("app.agent.phases.decide.basic.llm_chat") as mock_llm, \
         patch("app.agent.phases.decide.basic.MemoryService") as MockMS:
        MockMS.return_value = AsyncMock(get_memories=AsyncMock(return_value=[]))
        plugin = BasicDecidePlugin(params={"skip_decide_when_planned": False})
        ctx = await plugin.execute(ctx)
    mock_llm.assert_not_called()
    assert ctx.action_result.action == ActionType.IDLE
    assert ctx.plan_followed is True


@pytest.mark.anyio
async def test_social_interrupt_probability_runs_once_per_plan_slot(monkeypatch):
    from app.agent.phases.decide.basic import BasicDecidePlugin
    from app.config import settings
    monkeypatch.setattr(settings, "realism_plan_continuity_enabled", True)
    plan_date = "2026-08-11"

    def context():
        ctx = _decide_ctx_with_plan()
        ctx.plan_date = plan_date
        ctx.scheduled_plan = ctx.current_plan
        ctx.resident.meta_json["needs"] = {
            "energy": 0.8, "satiety": 0.8, "social": 0.1,
        }
        neighbor = _make_resident("neighbor")
        neighbor.id = "other-res"
        neighbor.status = "idle"
        ctx.nearby_residents = [neighbor]
        return ctx

    plugin = BasicDecidePlugin(params={
        "skip_decide_when_planned": True, "social_interrupt_chance": 1.0,
    })
    with patch("app.agent.phases.decide.basic.llm_chat") as llm, \
         patch("app.agent.phases.decide.basic.MemoryService") as MockMS:
        llm.return_value = (
            '{"action":"IDLE","target_slug":null,"target_tile":null,"reason":"聊聊"}'
        )
        MockMS.return_value = AsyncMock(get_memories=AsyncMock(return_value=[]))
        first = await plugin.execute(context())
        second = await plugin.execute(context())

    assert first.plan_interrupt_reason == "social"
    assert llm.await_count == 1
    assert second.action_result.action == ActionType.IDLE
    assert second.plan_interrupt_reason is None


@pytest.mark.anyio
async def test_walking_plan_is_sticky_against_social_noise(monkeypatch):
    from app.agent.phases.decide.basic import BasicDecidePlugin
    from app.config import settings
    monkeypatch.setattr(settings, "realism_enabled", True)
    monkeypatch.setattr(settings, "realism_plan_continuity_enabled", True)
    ctx = _decide_ctx_with_plan(action="VISIT_DISTRICT")
    ctx.current_plan.target = "academy"
    ctx.current_plan.location = "学院"
    ctx.scheduled_plan = ctx.current_plan
    ctx.plan_date = "2026-08-11"
    ctx.resident.status = "walking"
    ctx.resident.meta_json["needs"] = {
        "energy": 0.8, "satiety": 0.8, "social": 0.1,
    }
    neighbor = _make_resident("neighbor")
    neighbor.id = "other-res"
    neighbor.status = "idle"
    ctx.nearby_residents = [neighbor]

    with patch("app.agent.phases.decide.basic.llm_chat") as llm, \
         patch("app.agent.phases.decide.basic.MemoryService") as MockMS:
        MockMS.return_value = AsyncMock(get_memories=AsyncMock(return_value=[]))
        out = await BasicDecidePlugin(
            params={"skip_decide_when_planned": True}
        ).execute(ctx)

    llm.assert_not_awaited()
    assert out.action_result.action == ActionType.VISIT_DISTRICT
    assert out.plan_followed is True


@pytest.mark.anyio
async def test_distraction_marks_plan_not_followed(monkeypatch):
    """F5 回归锚: distraction 分支必须显式写 plan_followed=False。

    否则 TickContext 默认 True 一路活到 tick 收尾, LLM 的自由移动会因
    plan_followed && scheduled_plan 被误判为 planned_move 建 sticky trip。
    """
    from app.agent.phases.decide.spontaneous import SpontaneousDecidePlugin
    from app.config import settings
    monkeypatch.setattr(settings, "realism_plan_continuity_enabled", True)

    ctx = _decide_ctx_with_plan(action="WORK")
    ctx.plan_date = "2026-08-11"
    ctx.scheduled_plan = ctx.current_plan

    with patch("app.agent.phases.decide.basic.llm_chat") as mock_llm, \
         patch("app.agent.phases.decide.basic.MemoryService") as MockMS:
        mock_llm.return_value = (
            '{"action": "WANDER", "target_slug": null,'
            ' "target_tile": [80, 55], "reason": "随便走走"}'
        )
        MockMS.return_value = AsyncMock(get_memories=AsyncMock(return_value=[]))
        plugin = SpontaneousDecidePlugin(
            params={"distraction_chance": 1.0, "social_eagerness": False})
        out = await plugin.execute(ctx)

    assert out.plan_interrupt_reason == "spontaneous"
    assert out.current_plan is None
    assert out.action_result.action == ActionType.WANDER
    assert out.plan_followed is False


@pytest.mark.anyio
async def test_distraction_free_move_does_not_create_sticky_trip(monkeypatch):
    """F5 端到端: distraction 后的 LLM 自由移动不得建 sticky planned trip。

    走真 resident_tick + 真 SpontaneousDecidePlugin: PlanStub 模拟 plan 阶段
    产出 current_plan/scheduled_plan, distraction 命中清 plan 后 LLM 自由
    WANDER——tick 收尾不允许对已放弃的 plan slot set_active_trip。
    """
    from app.agent.phases.decide.spontaneous import SpontaneousDecidePlugin
    from app.agent.plan_continuity import _active_key
    from app.agent.tick import resident_tick
    from app.config import settings
    from app.redis_client import get_redis

    monkeypatch.setattr(settings, "realism_plan_continuity_enabled", True)

    resident = _make_resident("distracted-res")

    class PlanStub:
        async def execute(self, ctx):
            ctx.current_plan = HourlyPlan(
                slot=1, hour_range=(9, 12), action="WORK", target=None,
                location="office", importance=3, reason="上班", status="pending",
            )
            ctx.scheduled_plan = ctx.current_plan
            ctx.plan_date = "2026-08-11"
            return ctx

    decide = SpontaneousDecidePlugin(
        params={"distraction_chance": 1.0, "social_eagerness": False})

    with patch("app.agent.tick.registry") as mock_reg, \
         patch("app.agent.phases.decide.basic.llm_chat") as mock_llm, \
         patch("app.agent.phases.decide.basic.MemoryService") as MockMS:
        mock_reg.get_phases.return_value = [PlanStub(), decide]
        mock_llm.return_value = (
            '{"action": "WANDER", "target_slug": null,'
            ' "target_tile": [80, 55], "reason": "随便走走"}'
        )
        MockMS.return_value = AsyncMock(get_memories=AsyncMock(return_value=[]))
        result = await resident_tick(AsyncMock(), resident)

    assert result is not None and result.action == ActionType.WANDER
    assert await get_redis().get(_active_key(resident.id)) is None


# ── Plan Tests ───────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_basic_plan_generates_plan_when_stale():
    from app.agent.phases.plan.basic import BasicPlanPlugin

    ctx = _make_ctx()
    resident = ctx.resident
    resident.daily_plans_json = None
    resident.daily_goal_json = None
    resident.persona_md = "A curious learner."

    llm_response = '{"goal": {"goal": "学习新技能", "motivation": "好奇心驱使"}, "plans": [{"slot": 0, "hour_range": [7, 9], "action": "IDLE", "target": null, "location": "home", "importance": 2, "reason": "起床"}, {"slot": 1, "hour_range": [9, 11], "action": "STUDY", "target": null, "location": "library", "importance": 5, "reason": "学习"}, {"slot": 2, "hour_range": [11, 13], "action": "IDLE", "target": null, "location": "home", "importance": 2, "reason": "午餐"}]}'

    with patch("app.agent.phases.plan.basic.llm_chat", return_value=llm_response), \
         patch("app.agent.phases.plan.basic.MemoryService") as MockMS, \
         patch("app.agent.phases.plan.basic.manager") as mock_mgr:
        mock_svc = AsyncMock()
        mock_svc.get_memories = AsyncMock(return_value=[])
        MockMS.return_value = mock_svc
        mock_mgr.broadcast = AsyncMock()

        plugin = BasicPlanPlugin(params={"hourly_slots": 3, "max_social_slots": 1, "max_high_importance": 1})
        ctx = await plugin.execute(ctx)

    assert resident.daily_goal_json is not None
    assert resident.daily_goal_json["goal"] == "学习新技能"
    assert resident.daily_plans_json is not None
    assert len(resident.daily_plans_json["plans"]) == 3


@pytest.mark.anyio
async def test_basic_plan_skips_when_fresh():
    from app.agent.phases.plan.basic import BasicPlanPlugin
    from app.world_clock import world_date_key

    ctx = _make_ctx()
    resident = ctx.resident
    # World time (agent-T): a plan is "fresh" when generated on the current WORLD
    # day, so the fixture stamps the world date key (not the real wall-clock date).
    today = world_date_key()
    resident.daily_goal_json = {"goal": "existing", "motivation": "test", "created_at": "now", "status": "active"}
    resident.daily_plans_json = {
        "generated_date": today,
        "plans": [
            {"slot": 0, "hour_range": [7, 9], "action": "IDLE", "target": None, "location": "home", "importance": 2, "reason": "休息", "status": "pending"},
        ],
    }
    ctx.hour = 8

    plugin = BasicPlanPlugin(params={"hourly_slots": 1})
    ctx = await plugin.execute(ctx)

    assert ctx.current_plan is not None
    assert ctx.current_plan.action == "IDLE"


@pytest.mark.anyio
async def test_basic_plan_broadcasts_on_generation():
    from app.agent.phases.plan.basic import BasicPlanPlugin

    ctx = _make_ctx()
    resident = ctx.resident
    resident.daily_plans_json = None
    resident.daily_goal_json = None
    resident.persona_md = "A friendly person."

    llm_response = '{"goal": {"goal": "test", "motivation": "test"}, "plans": [{"slot": 0, "hour_range": [7, 9], "action": "IDLE", "target": null, "location": "home", "importance": 3, "reason": "rest"}]}'

    with patch("app.agent.phases.plan.basic.llm_chat", return_value=llm_response), \
         patch("app.agent.phases.plan.basic.MemoryService") as MockMS, \
         patch("app.agent.phases.plan.basic.manager") as mock_mgr:
        mock_svc = AsyncMock()
        mock_svc.get_memories = AsyncMock(return_value=[])
        MockMS.return_value = mock_svc
        mock_mgr.broadcast = AsyncMock()

        plugin = BasicPlanPlugin(params={"hourly_slots": 1, "max_social_slots": 1, "max_high_importance": 1})
        ctx = await plugin.execute(ctx)

    mock_mgr.broadcast.assert_called_once()
    broadcast_data = mock_mgr.broadcast.call_args[0][0]
    assert broadcast_data["type"] == "resident_plan_generated"
    assert broadcast_data["resident_slug"] == resident.slug


@pytest.mark.anyio
async def test_basic_plan_missing_importance_still_persists_and_loads():
    """LLM 偶发漏掉 slot 的 importance 键时，plan 仍要持久化、当前时段仍要
    加载成功（importance 落到 prompt 示例默认值 3），不能抛 KeyError 让整个
    phase 失败进入重试循环（vm212 生产 'Phase failed: importance' bug）。"""
    from app.agent.phases.plan.basic import BasicPlanPlugin

    ctx = _make_ctx()
    resident = ctx.resident
    resident.daily_plans_json = None
    resident.daily_goal_json = None
    resident.persona_md = "A curious learner."

    # 覆盖 ctx.hour=10 的 slot 1 缺 importance 键
    llm_response = '{"goal": {"goal": "学习新技能", "motivation": "好奇心驱使"}, "plans": [{"slot": 0, "hour_range": [7, 9], "action": "IDLE", "target": null, "location": "home", "importance": 2, "reason": "起床"}, {"slot": 1, "hour_range": [9, 11], "action": "STUDY", "target": null, "location": "library", "reason": "学习"}]}'

    with patch("app.agent.phases.plan.basic.llm_chat", return_value=llm_response), \
         patch("app.agent.phases.plan.basic.MemoryService") as MockMS, \
         patch("app.agent.phases.plan.basic.manager") as mock_mgr:
        mock_svc = AsyncMock()
        mock_svc.get_memories = AsyncMock(return_value=[])
        MockMS.return_value = mock_svc
        mock_mgr.broadcast = AsyncMock()

        plugin = BasicPlanPlugin(params={"hourly_slots": 2, "max_social_slots": 1, "max_high_importance": 1})
        ctx = await plugin.execute(ctx)

    assert resident.daily_plans_json is not None
    assert len(resident.daily_plans_json["plans"]) == 2
    assert ctx.current_plan is not None
    assert ctx.current_plan.action == "STUDY"
    assert ctx.current_plan.importance == 3


@pytest.mark.anyio
async def test_basic_plan_broadcast_survives_missing_importance():
    """top_plan 缺 importance 时 broadcast 不能静默失败，importance 用默认值。"""
    from app.agent.phases.plan.basic import BasicPlanPlugin

    ctx = _make_ctx()
    resident = ctx.resident
    resident.daily_plans_json = None
    resident.daily_goal_json = None
    resident.persona_md = "A friendly person."

    llm_response = '{"goal": {"goal": "test", "motivation": "test"}, "plans": [{"slot": 0, "hour_range": [7, 9], "action": "IDLE", "target": null, "location": "home", "reason": "rest"}]}'

    with patch("app.agent.phases.plan.basic.llm_chat", return_value=llm_response), \
         patch("app.agent.phases.plan.basic.MemoryService") as MockMS, \
         patch("app.agent.phases.plan.basic.manager") as mock_mgr:
        mock_svc = AsyncMock()
        mock_svc.get_memories = AsyncMock(return_value=[])
        MockMS.return_value = mock_svc
        mock_mgr.broadcast = AsyncMock()

        plugin = BasicPlanPlugin(params={"hourly_slots": 1, "max_social_slots": 1, "max_high_importance": 1})
        ctx = await plugin.execute(ctx)

    mock_mgr.broadcast.assert_called_once()
    broadcast_data = mock_mgr.broadcast.call_args[0][0]
    assert broadcast_data["top_plan"]["importance"] == 3


# ── Execute Tests ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_basic_execute_movement():
    from app.agent.phases.execute.basic import BasicExecutePlugin
    from app.agent.actions import ActionResult

    resident = _make_resident()
    resident.tile_x = 76
    resident.tile_y = 50
    ctx = _make_ctx()
    ctx.resident = resident
    ctx.action_result = ActionResult(
        action=ActionType.WANDER, target_slug=None,
        target_tile=(80, 50), reason="散步",
    )

    with patch("app.agent.phases.execute.basic.get_walkable_tiles") as mock_wt, \
         patch("app.agent.phases.execute.basic.find_path") as mock_fp:
        mock_wt.return_value = {(76, 50), (77, 50), (78, 50), (79, 50), (80, 50)}
        mock_fp.return_value = [(76, 50), (77, 50), (78, 50), (79, 50), (80, 50)]
        plugin = BasicExecutePlugin(params={"max_steps_per_tick": 1})
        ctx = await plugin.execute(ctx)

    assert ctx.new_tile == (77, 50)
    assert resident.tile_x == 77
    ctx.db.commit.assert_awaited_once()


@pytest.mark.anyio
async def test_basic_execute_idle():
    from app.agent.phases.execute.basic import BasicExecutePlugin
    from app.agent.actions import ActionResult

    ctx = _make_ctx()
    ctx.action_result = ActionResult(
        action=ActionType.IDLE, target_slug=None, target_tile=None, reason="休息",
    )

    plugin = BasicExecutePlugin(params={})
    ctx = await plugin.execute(ctx)
    assert ctx.resident.status == "idle"


@pytest.mark.anyio
async def test_basic_execute_skips_when_no_action():
    from app.agent.phases.execute.basic import BasicExecutePlugin

    ctx = _make_ctx()
    ctx.action_result = None

    plugin = BasicExecutePlugin(params={})
    ctx = await plugin.execute(ctx)
    assert ctx.new_tile is None


# ── Memorize Tests ───────────────────────────────────────────────────

@pytest.mark.anyio
async def test_basic_memorize_creates_memory():
    from app.agent.phases.memorize.basic import BasicMemorizePlugin
    from app.agent.actions import ActionResult

    ctx = _make_ctx()
    ctx.action_result = ActionResult(
        action=ActionType.WANDER, target_slug=None,
        target_tile=(80, 50), reason="散步",
    )

    with patch("app.agent.phases.memorize.basic.MemoryService") as MockMS:
        mock_svc = AsyncMock()
        MockMS.return_value = mock_svc
        plugin = BasicMemorizePlugin(params={"base_importance": 0.3, "plan_deviation_boost": 0.2})
        ctx = await plugin.execute(ctx)

    mock_svc.add_memory.assert_called_once()
    call_kwargs = mock_svc.add_memory.call_args
    assert call_kwargs[1]["importance"] == 0.3
    assert ctx.memory_created is True


@pytest.mark.anyio
async def test_basic_memorize_boosts_importance_on_plan_deviation():
    from app.agent.phases.memorize.basic import BasicMemorizePlugin
    from app.agent.actions import ActionResult

    ctx = _make_ctx()
    ctx.action_result = ActionResult(
        action=ActionType.CHAT_RESIDENT, target_slug="alice",
        target_tile=None, reason="聊天",
    )
    ctx.plan_followed = False

    with patch("app.agent.phases.memorize.basic.MemoryService") as MockMS:
        mock_svc = AsyncMock()
        MockMS.return_value = mock_svc
        plugin = BasicMemorizePlugin(params={"base_importance": 0.3, "plan_deviation_boost": 0.2})
        ctx = await plugin.execute(ctx)

    call_kwargs = mock_svc.add_memory.call_args
    assert call_kwargs[1]["importance"] == pytest.approx(0.5)
