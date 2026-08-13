"""P2 Task 7 — crowd / 人流聚集 (festival ×3 draw + herd micro-rule).

Completes the P1 task-9 deviation (festival ×3 VISIT_DISTRICT weight). All gated
by REALISM_CROWD_ENABLED; off → pre-P2 behavior.
"""
import asyncio
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.agent.map_data import LOCATIONS, get_location_by_id
from app.services import crowd_service
from app.agent.actions import ActionType
from app.models.resident import Resident


FESTIVAL = {"id": "f1", "type": "festival", "title": "丰收节", "description": "丰收", "payload_json": {}}
LEGACY_MARKET_DAY = {
    "id": "m1", "type": "festival", "title": "集市日", "description": "赶集",
    "payload_json": {"market_day": True, "location_id": "central_plaza"},
}
MARKET_DAY = {
    "id": "market-1", "type": "festival", "title": "集市日",
    "starts_at": "2026-08-13T00:00:00+00:00",
    "ends_at": "2026-08-14T00:00:00+00:00",
    "payload_json": {"market_day": True, "location_id": "market_hall"},
}


def test_active_event_location_defaults_to_plaza():
    assert crowd_service.active_event_location([FESTIVAL]) == "central_plaza"
    assert crowd_service.active_event_location([{"type": "news", "payload_json": {}}]) is None
    assert crowd_service.active_event_location([]) is None


def test_active_legacy_market_day_is_read_from_the_market_hall():
    assert crowd_service.active_event_location([LEGACY_MARKET_DAY]) == "market_hall"
    custom = {
        **LEGACY_MARKET_DAY,
        "payload_json": {"market_day": True, "location_id": "east_gardens"},
    }
    assert crowd_service.active_event_location([custom]) == "east_gardens"


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
        r = await plug._maybe_crowd_draw(ctx, rng=random.Random(i))
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
    results = [await plug._maybe_crowd_draw(ctx, rng=random.Random(i)) for i in range(30)]
    assert all(result is None for result in results)


@pytest.mark.anyio
async def test_market_day_cohort_is_real_bounded_and_deterministic(db_session):
    crowd_service._reset_for_tests()
    hall = get_location_by_id("market_hall")["center"]
    eligible_ids = []
    for i in range(8):
        resident_id = f"eligible-{i}"
        eligible_ids.append(resident_id)
        db_session.add(Resident(
            id=resident_id, slug=resident_id, name=resident_id, creator_id="sys",
            resident_type="npc", district="cafe", status="idle",
            tile_x=10 + i, tile_y=10,
        ))
    db_session.add_all([
        Resident(id="asleep", slug="asleep", name="asleep", creator_id="sys",
                 resident_type="npc", district="cafe", status="sleeping", tile_x=1, tile_y=1),
        Resident(id="chatting", slug="chatting", name="chatting", creator_id="sys",
                 resident_type="npc", district="cafe", status="chatting", tile_x=1, tile_y=1),
        Resident(id="socializing", slug="socializing", name="socializing", creator_id="sys",
                 resident_type="npc", district="cafe", status="socializing", tile_x=1, tile_y=1),
        Resident(id="at-hall", slug="at-hall", name="at-hall", creator_id="sys",
                 resident_type="npc", district="market_hall", status="idle",
                 tile_x=hall[0], tile_y=hall[1]),
        Resident(id="avatar", slug="avatar", name="avatar", creator_id="player",
                 resident_type="player", district="cafe", status="idle", tile_x=1, tile_y=1),
    ])
    await db_session.commit()

    first = await crowd_service.market_day_crowd_cohort(db_session, [MARKET_DAY], ttl=0)
    second = await crowd_service.market_day_crowd_cohort(db_session, [MARKET_DAY], ttl=0)

    assert first == second
    assert len(first) == crowd_service.MARKET_DAY_CROWD_LIMIT == 4
    assert first <= set(eligible_ids)


@pytest.mark.anyio
async def test_market_day_cohort_cache_single_flights_concurrent_ticks():
    crowd_service._reset_for_tests()
    lifecycle_result = MagicMock()
    lifecycle_result.all.return_value = []
    resident_result = MagicMock()
    resident_result.all.return_value = [
        (f"resident-{i}", i, 0) for i in range(7)
    ]
    db = SimpleNamespace(execute=AsyncMock(
        side_effect=[lifecycle_result, resident_result],
    ))

    cohorts = await asyncio.gather(*(
        crowd_service.market_day_crowd_cohort(db, [MARKET_DAY])
        for _ in range(10)
    ))

    # One lifecycle assignment lookup plus one fallback resident selection,
    # still single-flighted across all ten concurrent resident ticks.
    assert db.execute.await_count == 2
    assert all(cohort == cohorts[0] for cohort in cohorts)
    assert len(cohorts[0]) == 4


@pytest.mark.anyio
async def test_no_active_market_day_has_no_cohort_query():
    crowd_service._reset_for_tests()
    db = SimpleNamespace(execute=AsyncMock())

    assert await crowd_service.market_day_crowd_cohort(db, []) == frozenset()
    assert await crowd_service.market_day_crowd_cohort(db, [FESTIVAL]) == frozenset()
    db.execute.assert_not_awaited()


@pytest.mark.anyio
async def test_market_cohort_member_gets_stable_visit_target(monkeypatch):
    monkeypatch.setattr(settings, "realism_crowd_enabled", True)
    resident = SimpleNamespace(
        id="chosen", slug="chosen", tile_x=0, tile_y=0, status="idle",
    )
    ctx = _ctx(resident, [MARKET_DAY])

    with patch.object(
        crowd_service, "market_day_crowd_cohort",
        AsyncMock(return_value=frozenset({"chosen"})),
    ):
        result = await _plugin()._maybe_crowd_draw(ctx, rng=random.Random(0))

    assert result is not None
    assert result.action == ActionType.VISIT_DISTRICT
    assert result.target_slug == "market_hall"
    assert result.target_tile in crowd_service.MARKET_DAY_VISITOR_TILES


def test_market_cohort_slots_are_reachable_unique_and_clear_of_loading_lane():
    from app.agent.pathfinder import get_reachable_tiles

    tiles = crowd_service.MARKET_DAY_VISITOR_TILES
    assert len(tiles) == crowd_service.MARKET_DAY_CROWD_LIMIT == len(set(tiles))
    assert set(tiles) <= get_reachable_tiles()
    assert all(x >= 114 and (x, y) != (109, 94) for x, y in tiles)


def test_market_cohort_members_receive_stable_distinct_slots():
    cohort = frozenset({"a", "b", "c", "d"})
    first = {
        resident_id: crowd_service.market_day_visitor_tile(
            resident_id, cohort, [MARKET_DAY],
        )
        for resident_id in cohort
    }
    second = {
        resident_id: crowd_service.market_day_visitor_tile(
            resident_id, cohort, [MARKET_DAY],
        )
        for resident_id in reversed(sorted(cohort))
    }
    assert first == second
    assert len(set(first.values())) == len(cohort)


@pytest.mark.anyio
@pytest.mark.parametrize("protection", ["chatting", "socializing", "active_trip", "go_home"])
async def test_market_cohort_does_not_break_protected_activity(monkeypatch, protection):
    from app.agent.schemas import HourlyPlan

    monkeypatch.setattr(settings, "realism_crowd_enabled", True)
    resident = SimpleNamespace(
        id="chosen", slug="chosen", tile_x=0, tile_y=0, status="idle",
    )
    ctx = _ctx(resident, [MARKET_DAY])
    if protection in {"chatting", "socializing"}:
        resident.status = protection
    elif protection == "active_trip":
        ctx.continuation_trip = {"action": "VISIT_DISTRICT"}
    else:
        ctx.current_plan = HourlyPlan(0, (9, 12), "GO_HOME", None, "home", 3, "回家")
        ctx.scheduled_plan = ctx.current_plan
    cohort = AsyncMock(return_value=frozenset({"chosen"}))

    with patch.object(crowd_service, "market_day_crowd_cohort", cohort):
        assert await _plugin()._maybe_crowd_draw(ctx) is None

    cohort.assert_not_awaited()


@pytest.mark.anyio
async def test_critical_need_remains_ahead_of_market_pull(monkeypatch):
    monkeypatch.setattr(settings, "realism_enabled", True)
    monkeypatch.setattr(settings, "realism_crowd_enabled", True)
    resident = Resident(
        id="chosen", slug="chosen", name="Chosen", creator_id="sys",
        resident_type="npc", district="cafe", status="idle", tile_x=0, tile_y=0,
        home_tile_x=5, home_tile_y=5,
        meta_json={"needs": {"energy": 0.1, "satiety": 0.8, "social": 0.8}},
    )
    ctx = _ctx(resident, [MARKET_DAY])
    ctx.db = AsyncMock()
    plugin = _plugin()
    plugin._load_memories = AsyncMock()
    cohort = AsyncMock(return_value=frozenset({"chosen"}))

    with patch.object(crowd_service, "market_day_crowd_cohort", cohort):
        result = await plugin.execute(ctx)

    assert result.action_result.action == ActionType.GO_HOME
    cohort.assert_not_awaited()


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
