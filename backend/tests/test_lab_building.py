"""P0 — Experiment building (Lab) skeleton: location registration, the RESEARCH
action gate, the narrative execute handler, and the entrance WS prompt.

Mirrors test_agent_actions (MagicMock residents) and test_location_tracker
(patched async_session + patched manager.send) so nothing here needs a live DB
beyond the in-memory sqlite fixtures. Real sandbox execution is P1+; this file
only covers the building being reachable + the tick-side narrative wiring.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.agent.actions import ActionType, ActionResult, get_available_actions
from app.agent import map_data
from app.models.resident import Resident


EXPERIMENT_BOUNDS = (108, 72, 124, 86)


def _make_resident(*, researcher: bool, tile_x: int, tile_y: int, status: str = "idle"):
    r = MagicMock(spec=Resident)
    r.id = "res-lab"
    r.slug = "res-lab"
    r.status = status
    r.tile_x = tile_x
    r.tile_y = tile_y
    r.home_tile_x = None
    r.home_tile_y = None
    r.home_location_id = None
    r.meta_json = {"lab": {"access": True, "tier": "junior", "skills": ["web"]}} if researcher else {}
    return r


# ── Location registration ─────────────────────────────────────────────

def test_experiment_building_registered():
    loc = map_data.LOCATIONS.get("experiment_building")
    assert loc is not None, "experiment_building must be in LOCATIONS"
    assert loc["type"] == "public"
    assert loc["bounds"] == EXPERIMENT_BOUNDS
    assert "center" in loc and "entrance" in loc
    assert "RESEARCH" in loc.get("boosted_actions", [])


def test_experiment_bounds_no_overlap():
    """The Lab sits on empty land — its bbox must not intersect any other."""
    def overlap(a, b):
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        return not (ax2 < bx1 or bx2 < ax1 or ay2 < by1 or by2 < ay1)

    for slug, loc in map_data.LOCATIONS.items():
        if slug == "experiment_building":
            continue
        assert not overlap(EXPERIMENT_BOUNDS, loc["bounds"]), f"overlaps {slug}"


def test_experiment_building_lookup():
    # center is inside the bbox → resolves to the building.
    assert map_data.get_location_id_at(116, 79) == "experiment_building"
    assert map_data.get_location_at(116, 79)["name"] == "实验楼"


def test_load_dynamic_locations_is_noop_in_p0():
    # P3 replaces the body; the P0 stub must be a harmless 0-return.
    assert map_data.load_dynamic_locations() == 0


# ── RESEARCH action + gating ──────────────────────────────────────────

def test_research_is_15th_action():
    assert ActionType.RESEARCH.value == "RESEARCH"
    assert len(list(ActionType)) == 15


def test_research_available_to_researcher_in_building():
    r = _make_resident(researcher=True, tile_x=116, tile_y=79)
    assert ActionType.RESEARCH in get_available_actions(r, nearby_residents=[])


def test_research_unavailable_to_non_researcher_in_building():
    r = _make_resident(researcher=False, tile_x=116, tile_y=79)
    assert ActionType.RESEARCH not in get_available_actions(r, nearby_residents=[])


def test_research_unavailable_to_researcher_outside_building():
    # Central plaza — a researcher elsewhere cannot research.
    r = _make_resident(researcher=True, tile_x=76, tile_y=50)
    assert ActionType.RESEARCH not in get_available_actions(r, nearby_residents=[])


def test_research_does_not_leak_into_default_actions():
    # A normal (non-lab) resident anywhere never sees RESEARCH.
    r = _make_resident(researcher=False, tile_x=76, tile_y=50)
    assert ActionType.RESEARCH not in get_available_actions(r, nearby_residents=[])


# ── Narrative memory + execute handler ────────────────────────────────

def test_research_memory_text():
    from app.agent.phases.memorize.basic import format_action_memory

    r = _make_resident(researcher=True, tile_x=116, tile_y=79)
    result = ActionResult(action=ActionType.RESEARCH, target_slug=None, target_tile=None, reason="做研究")
    text = format_action_memory(result, r)
    assert "研究" in text
    assert "实验楼" in text  # loc name resolves inside the building


@pytest.mark.anyio
async def test_research_execute_sets_researching():
    from app.agent.phases.execute.basic import BasicExecutePlugin
    from app.agent.schemas import TickContext

    resident = _make_resident(researcher=True, tile_x=116, tile_y=79)
    resident.status = "idle"
    ctx = TickContext(
        db=AsyncMock(), resident=resident, world_time="10:00", hour=10,
        schedule_phase="上午", nearby_residents=[], current_plan=None,
        available_actions=[ActionType.RESEARCH],
    )
    ctx.action_result = ActionResult(
        action=ActionType.RESEARCH, target_slug=None, target_tile=None, reason="研究",
    )
    plugin = BasicExecutePlugin(params={})
    ctx = await plugin.execute(ctx)
    assert ctx.resident.status == "researching"


# ── Entrance → experiment_prompt WS frame ─────────────────────────────

@pytest.fixture
def lt_session(db_engine):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("app.services.location_tracker.async_session", factory):
        yield


@pytest.mark.anyio
async def test_entering_experiment_building_sends_prompt(db_session, lt_session):
    from app.services import location_tracker as lt
    import app.ws.manager as wsm

    with patch.object(lt, "emit", new_callable=AsyncMock), \
         patch.object(wsm.manager, "send", new_callable=AsyncMock) as send_mock:
        await lt.process_one("u1", "experiment_building")

    sent = [c.args[1] for c in send_mock.call_args_list if len(c.args) >= 2]
    prompts = [d for d in sent if isinstance(d, dict) and d.get("type") == "experiment_prompt"]
    assert prompts, "entering the experiment building must send an experiment_prompt frame"
    assert prompts[0]["location_id"] == "experiment_building"
    assert prompts[0]["name"] == "实验楼"
