"""Realism P1-10: three needs — metabolism, critical arbitration, EAT, sleep."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import settings
from app.agent import needs as N


def _res(meta=None, status="idle"):
    r = MagicMock()
    r.meta_json = meta
    r.status = status
    return r


def test_get_needs_defaults():
    assert N.get_needs(_res()) == {"energy": 0.8, "satiety": 0.8, "social": 0.8}


def test_write_and_get_roundtrip():
    r = _res(meta={"sbti": {"type": "X"}})
    N.write_needs(r, {"energy": 0.3, "satiety": 0.5, "social": 0.9})
    assert r.meta_json["sbti"] == {"type": "X"}   # other namespaces preserved
    assert N.get_needs(r) == {"energy": 0.3, "satiety": 0.5, "social": 0.9}


def test_metabolize_rates():
    base = {"energy": 0.5, "satiety": 0.5, "social": 0.5}
    awake = N.metabolize(base, status="idle", sbti=None)
    assert awake["energy"] == pytest.approx(0.5 + settings.realism_energy_awake)
    assert awake["satiety"] == pytest.approx(0.5 + settings.realism_satiety_decay)
    walking = N.metabolize(base, status="walking", sbti=None)
    assert walking["energy"] < awake["energy"]   # walking drains faster
    sleeping = N.metabolize(base, status="sleeping", sbti=None)
    assert sleeping["energy"] > 0.5              # sleep recovers


def test_social_rate_by_extraversion():
    intro = N.metabolize({"energy": 1, "satiety": 1, "social": 0.5},
                         status="idle", sbti={"dimensions": {"So1": "L"}})
    extro = N.metabolize({"energy": 1, "satiety": 1, "social": 0.5},
                         status="idle", sbti={"dimensions": {"So1": "H"}})
    assert intro["social"] > extro["social"]     # introvert drains social slower


def test_most_critical():
    assert N.most_critical({"energy": 0.1, "satiety": 0.8, "social": 0.8}) == "energy"
    assert N.most_critical({"energy": 0.8, "satiety": 0.8, "social": 0.8}) is None


@pytest.mark.anyio
async def test_arbitration_energy_forces_go_home(monkeypatch):
    from app.agent.phases.decide.basic import BasicDecidePlugin
    from app.agent.actions import ActionType
    from app.agent.schemas import TickContext
    monkeypatch.setattr(settings, "realism_enabled", True)
    r = _res(meta={"needs": {"energy": 0.1, "satiety": 0.8, "social": 0.8}})
    r.tile_x, r.tile_y = 10, 10
    ctx = TickContext(db=AsyncMock(), resident=r, world_time="10:00", hour=10, schedule_phase="上午")
    ctx.available_actions = [ActionType.GO_HOME, ActionType.IDLE]
    res = BasicDecidePlugin()._maybe_needs_action(ctx)
    assert res is not None and res.action == ActionType.GO_HOME


@pytest.mark.anyio
async def test_critical_need_cancels_sticky_trip(monkeypatch):
    from app.agent.phases.decide.basic import BasicDecidePlugin
    from app.agent.actions import ActionType
    from app.agent.plan_continuity import _active_key
    from app.agent.schemas import HourlyPlan, TickContext
    from app.redis_client import get_redis
    monkeypatch.setattr(settings, "realism_enabled", True)
    monkeypatch.setattr(settings, "realism_plan_continuity_enabled", True)
    r = _res(meta={"needs": {"energy": 0.1, "satiety": 0.8, "social": 0.8}})
    r.id, r.slug, r.tile_x, r.tile_y = "r1", "r1", 10, 10
    r.home_location_id = None
    r.home_tile_x, r.home_tile_y = 5, 5
    r.resident_type = "character"
    plan = HourlyPlan(1, (9, 12), "VISIT_DISTRICT", "academy", "academy", 3, "上课")
    trip = {"action": "VISIT_DISTRICT", "target": "academy", "target_tile": [76, 50],
            "plan_date": "2026-08-11", "plan_slot": 1}
    ctx = TickContext(db=AsyncMock(), resident=r, world_time="10:00", hour=10,
                      schedule_phase="上午", current_plan=plan, scheduled_plan=plan,
                      plan_date="2026-08-11", continuation_trip=trip)
    await get_redis().set(_active_key(r.id), "saved")

    plugin = BasicDecidePlugin(params={"skip_decide_when_planned": True})
    plugin._load_memories = AsyncMock()
    out = await plugin.execute(ctx)

    assert out.action_result.action == ActionType.GO_HOME
    assert out.plan_interrupt_reason == "critical_need"
    assert out.continuation_trip is None
    assert await get_redis().get(_active_key(r.id)) is None


@pytest.mark.anyio
async def test_severe_weather_cancels_sticky_trip(monkeypatch):
    from app.agent.phases.decide.basic import BasicDecidePlugin
    from app.agent.actions import ActionType
    from app.agent.plan_continuity import _active_key
    from app.agent.schemas import HourlyPlan, TickContext
    from app.redis_client import get_redis
    monkeypatch.setattr(settings, "realism_enabled", True)
    monkeypatch.setattr(settings, "realism_plan_continuity_enabled", True)
    monkeypatch.setattr(settings, "realism_shelter_prob", 1.0)
    r = _res(meta={"needs": {"energy": 0.8, "satiety": 0.8, "social": 0.8}})
    r.id, r.slug, r.tile_x, r.tile_y = "r1", "r1", 10, 10
    r.home_location_id = None
    r.home_tile_x, r.home_tile_y = 5, 5
    r.resident_type = "character"
    plan = HourlyPlan(1, (9, 12), "VISIT_DISTRICT", "academy", "academy", 3, "上课")
    trip = {"action": "VISIT_DISTRICT", "target": "academy", "target_tile": [76, 50],
            "plan_date": "2026-08-11", "plan_slot": 1}
    ctx = TickContext(db=AsyncMock(), resident=r, world_time="10:00", hour=10,
                      schedule_phase="上午", current_plan=plan, scheduled_plan=plan,
                      plan_date="2026-08-11", continuation_trip=trip,
                      world_events=[{"type": "weather", "payload_json": {"kind": "storm"}}])
    await get_redis().set(_active_key(r.id), "saved")

    plugin = BasicDecidePlugin(params={"skip_decide_when_planned": True})
    plugin._load_memories = AsyncMock()
    out = await plugin.execute(ctx)

    assert out.action_result.action == ActionType.VISIT_DISTRICT
    assert out.plan_interrupt_reason == "severe_weather"
    assert out.continuation_trip is None
    assert await get_redis().get(_active_key(r.id)) is None


@pytest.mark.anyio
async def test_arbitration_satiety_eats_at_dining(monkeypatch):
    from app.agent.phases.decide.basic import BasicDecidePlugin
    from app.agent.actions import ActionType
    from app.agent.schemas import TickContext
    from app.agent.map_data import get_location_by_id
    monkeypatch.setattr(settings, "realism_enabled", True)
    cafe = get_location_by_id("cafe")
    tile = cafe.get("entrance") or cafe.get("center")
    r = _res(meta={"needs": {"energy": 0.8, "satiety": 0.1, "social": 0.8}})
    r.tile_x, r.tile_y = tile
    ctx = TickContext(db=AsyncMock(), resident=r, world_time="12:00", hour=12, schedule_phase="午后")
    ctx.available_actions = [ActionType.EAT, ActionType.VISIT_DISTRICT, ActionType.IDLE]
    res = BasicDecidePlugin()._maybe_needs_action(ctx)
    assert res is not None and res.action == ActionType.EAT


@pytest.mark.anyio
async def test_eat_restores_satiety(monkeypatch):
    from app.agent.phases.execute.basic import BasicExecutePlugin
    from app.agent.actions import ActionType, ActionResult
    from app.agent.schemas import TickContext
    monkeypatch.setattr(settings, "realism_enabled", True)
    r = MagicMock()
    r.status = "idle"
    r.meta_json = {"needs": {"energy": 0.8, "satiety": 0.2, "social": 0.8}}
    ctx = TickContext(db=AsyncMock(), resident=r, world_time="12:00", hour=12, schedule_phase="午后")
    ctx.action_result = ActionResult(ActionType.EAT, "cafe", None, "吃")
    await BasicExecutePlugin().execute(ctx)
    assert N.get_needs(r)["satiety"] == pytest.approx(min(1.0, 0.2 + settings.realism_eat_restore))


@pytest.mark.anyio
async def test_go_home_exhausted_sleeps(monkeypatch):
    from app.agent.phases.execute.basic import BasicExecutePlugin
    from app.agent.actions import ActionType, ActionResult
    from app.agent.schemas import TickContext
    monkeypatch.setattr(settings, "realism_enabled", True)
    r = MagicMock()
    r.status = "idle"
    r.tile_x, r.tile_y = 67, 20
    r.home_location_id = None
    r.home_tile_x, r.home_tile_y = 67, 20   # already home
    r.meta_json = {"needs": {"energy": 0.1, "satiety": 0.8, "social": 0.8}}
    r.mood_json = None
    ctx = TickContext(db=AsyncMock(), resident=r, world_time="22:00", hour=22, schedule_phase="夜晚")
    ctx.world_events = []
    ctx.action_result = ActionResult(ActionType.GO_HOME, None, None, "回家")
    await BasicExecutePlugin().execute(ctx)
    assert r.status == "sleeping"
    assert ctx.action_result.target_tile == (67, 20)


@pytest.mark.anyio
async def test_metabolize_sleepers_wakes_rested(db_session, monkeypatch):
    from app.agent import loop as agent_loop
    from app.models.resident import Resident
    monkeypatch.setattr(settings, "realism_enabled", True)
    monkeypatch.setattr(agent_loop, "async_session", lambda: _Ctx(db_session))
    r = Resident(slug="s", name="S", creator_id="c", status="sleeping", tile_x=1, tile_y=1,
                 meta_json={"needs": {"energy": 0.49, "satiety": 0.8, "social": 0.8}})
    db_session.add(r)
    await db_session.commit()
    # hour 10 is within the default schedule window (wake 8, sleep 22)
    woke = await agent_loop._metabolize_sleepers(current_hour=10, current_weekday=2)
    await db_session.refresh(r)
    # energy 0.49 + 0.02 = 0.51 ≥ 0.5 wake threshold, in-window → wakes
    assert woke == 1 and r.status == "idle"


class _Ctx:
    def __init__(self, s): self._s = s
    async def __aenter__(self): return self._s
    async def __aexit__(self, *a): return False


def _simulate_days(days, *, chats_per_day, eats_per_day):
    """按真实节奏的日平衡模型：1 世界日 = 360 轮 60s tick（k=4）；默认作息下
    清醒 metabolize ≈84 次（should_tick 门控）+ 睡眠 ≈150 次（每轮），恢复
    动作插在白天中段。纯函数复演 loop.py/tick.py 的节奏，不碰 DB。"""
    needs = {k: settings.realism_needs_initial for k in N.NEEDS}
    awake, asleep = 84, 150
    for _ in range(days):
        for _ in range(awake // 2):
            needs = N.metabolize(needs, status="idle", sbti=None)
        for _ in range(eats_per_day):
            needs["satiety"] = min(1.0, needs["satiety"] + settings.realism_eat_restore)
        for _ in range(chats_per_day):
            needs["social"] = min(1.0, needs["social"] + settings.realism_social_chat)
        for _ in range(awake - awake // 2):
            needs = N.metabolize(needs, status="idle", sbti=None)
        for _ in range(asleep):
            needs = N.metabolize(needs, status="sleeping", sbti=None)
    return needs


def test_needs_daily_balance_not_locked_at_zero():
    """平衡回归（0804 产线：9/11 satiety=0、11/11 social=0）：以实际可达的
    恢复频率（1 EAT + 2 chat/世界日），两个世界日后 satiety/social 不得锁死 0。
    旧 satiety_decay=-0.005 时日扣减 1.17 远超 1 次 EAT 的 +0.5 → 本测试红。"""
    needs = _simulate_days(2, chats_per_day=2, eats_per_day=1)
    assert needs["satiety"] >= 0.2
    assert needs["social"] >= 0.2


@pytest.mark.anyio
async def test_deadlock_at_home_resolves_through_sleep_then_eat(monkeypatch):
    """0809 产线死锁的整条解开链:在自家门口 energy=satiety=0 的居民,
    ①GO_HOME 重新可选 → ②decide 的 energy 分支给出 GO_HOME → ③execute 的
    already-at-destination 分支置 sleeping → ④睡眠代谢把 energy 抬回阈值上 →
    ⑤此时 most_critical 才轮到 satiety(平局时 min 按元组序恒取 energy,
    这正是饿死的放大器)→ 吃饭分支终于可达。"""
    from app.agent.actions import ActionType, get_available_actions
    from app.agent.phases.decide.basic import BasicDecidePlugin
    from app.agent.schemas import TickContext
    monkeypatch.setattr(settings, "realism_enabled", True)

    r = _res(meta={"needs": {"energy": 0.0, "satiety": 0.0, "social": 0.7}})
    r.tile_x, r.tile_y = 76, 50
    r.home_tile_x, r.home_tile_y = 76, 50
    r.home_location_id = None
    r.id, r.slug = "stuck", "stuck"

    # ① 卡死态下 GO_HOME 必须重新出现
    avail = get_available_actions(r, nearby_residents=[])
    assert ActionType.GO_HOME in avail

    # ② decide 给出 GO_HOME(修复前这里是 None —— 死锁点)
    ctx = TickContext(db=AsyncMock(), resident=r, world_time="22:00", hour=22,
                      schedule_phase="夜间")
    ctx.available_actions = avail
    res = BasicDecidePlugin()._maybe_needs_action(ctx)
    assert res is not None and res.action == ActionType.GO_HOME

    # ③④ 睡眠代谢把 energy 抬过临界
    needs = N.get_needs(r)
    while needs["energy"] < settings.realism_needs_critical:
        needs = N.metabolize(needs, status="sleeping", sbti=None)
    N.write_needs(r, needs)

    # ⑤ 现在最危急的才是 satiety —— 饿死放大器解除
    assert N.most_critical(N.get_needs(r)) == "satiety"
