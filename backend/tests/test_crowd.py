"""P2 Task 7 — crowd / 人流聚集 (festival ×3 draw + herd micro-rule).

Completes the P1 task-9 deviation (festival ×3 VISIT_DISTRICT weight). All gated
by REALISM_CROWD_ENABLED; off → pre-P2 behavior.
"""
import random
from types import SimpleNamespace

import pytest

from app.config import settings
from app.agent.map_data import LOCATIONS, get_location_by_id
from app.services import crowd_service
from app.agent.actions import ActionType
from app.models.resident import Resident


FESTIVAL = {"id": "f1", "type": "festival", "title": "丰收节", "description": "丰收", "payload_json": {}}


def test_active_event_location_defaults_to_plaza():
    assert crowd_service.active_event_location([FESTIVAL]) == "central_plaza"
    assert crowd_service.active_event_location([{"type": "news", "payload_json": {}}]) is None
    assert crowd_service.active_event_location([]) is None


def test_festival_draw_is_triple_weighted():
    n = len(LOCATIONS)
    rng = random.Random(0)
    redirects = sum(
        1 for _ in range(5000)
        if crowd_service.festival_draw_target([FESTIVAL], "somewhere_else", rng) == "central_plaza"
    )
    rate = redirects / 5000
    expected = 3.0 / (n + 2)          # weight 3 vs (n-1) others at weight 1
    assert abs(rate - expected) < 0.03
    # ... and far above a uniform 1/n pick, proving the ×3 boost.
    assert rate > 2 * (1.0 / n)


def test_festival_draw_none_when_already_there_or_no_event():
    rng = random.Random(0)
    assert crowd_service.festival_draw_target([FESTIVAL], "central_plaza", rng) is None
    assert crowd_service.festival_draw_target([], "x", rng) is None


def _ctx(resident, world_events=None):
    from app.agent.schemas import TickContext
    ctx = TickContext(db=None, resident=resident, world_time="", hour=12, schedule_phase="free")
    ctx.world_events = world_events or []
    ctx.available_actions = [ActionType.VISIT_DISTRICT]
    return ctx


def _plugin():
    from app.agent.phases.decide.basic import BasicDecidePlugin
    return BasicDecidePlugin()


@pytest.mark.anyio
async def test_maybe_crowd_draw_redirects_to_event(monkeypatch):
    monkeypatch.setattr(settings, "realism_crowd_enabled", True)
    plug = _plugin()
    res = SimpleNamespace(id="r", slug="r", tile_x=0, tile_y=0, status="idle")
    ctx = _ctx(res, [FESTIVAL])
    redirects = []
    for i in range(60):
        r = plug._maybe_crowd_draw(ctx, rng=random.Random(i))
        if r is not None:
            redirects.append(r)
    assert redirects, "festival should draw at least once over 60 seeded ticks"
    for r in redirects:
        assert r.action == ActionType.VISIT_DISTRICT
        assert r.target_slug == "central_plaza"   # every redirect is to the event


@pytest.mark.anyio
async def test_maybe_crowd_draw_gated_off(monkeypatch):
    monkeypatch.setattr(settings, "realism_crowd_enabled", False)
    plug = _plugin()
    res = SimpleNamespace(id="r", slug="r", tile_x=0, tile_y=0, status="idle")
    ctx = _ctx(res, [FESTIVAL])
    assert all(plug._maybe_crowd_draw(ctx, rng=random.Random(i)) is None for i in range(30))


# ------------------------------- herd hint -------------------------------------

async def _crowd_at_plaza(db, n):
    center = get_location_by_id("central_plaza")["center"]
    for i in range(n):
        db.add(Resident(id=f"c{i}", slug=f"c{i}", name=f"C{i}", creator_id="sys",
                        district="cafe", status="idle", tile_x=center[0], tile_y=center[1]))
    await db.commit()


def _low_social_resident():
    return SimpleNamespace(id="me", slug="me", tile_x=0, tile_y=0, status="idle",
                           meta_json={"needs": {"social": 0.3, "energy": 0.8, "satiety": 0.8}})


@pytest.mark.anyio
async def test_herd_hint_fires_when_lively_and_lonely(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_crowd_enabled", True)
    crowd_service._reset_for_tests()
    await _crowd_at_plaza(db_session, 5)     # ≥ threshold
    plug = _plugin()
    ctx = _ctx(_low_social_resident())
    ctx.db = db_session
    hint = await plug._crowd_hint(ctx)
    assert "热闹" in hint and "中央广场" in hint


@pytest.mark.anyio
async def test_herd_hint_silent_when_socially_full(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_crowd_enabled", True)
    crowd_service._reset_for_tests()
    await _crowd_at_plaza(db_session, 5)
    plug = _plugin()
    res = _low_social_resident()
    res.meta_json = {"needs": {"social": 0.8, "energy": 0.8, "satiety": 0.8}}   # not lonely
    ctx = _ctx(res)
    ctx.db = db_session
    assert await plug._crowd_hint(ctx) == ""


@pytest.mark.anyio
async def test_herd_hint_silent_when_no_crowd(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_crowd_enabled", True)
    crowd_service._reset_for_tests()
    await _crowd_at_plaza(db_session, 3)     # below threshold (5)
    plug = _plugin()
    ctx = _ctx(_low_social_resident())
    ctx.db = db_session
    assert await plug._crowd_hint(ctx) == ""
