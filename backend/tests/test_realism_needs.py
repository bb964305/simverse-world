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
