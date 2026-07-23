"""Realism P1-8: weather affects activity probability, location choice, mood."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import settings
from app.agent.scheduler import build_schedule, get_activity_probability
from app.agent.map_data import location_is_indoor, nearest_indoor_location


def test_storm_lowers_activity_probability(monkeypatch):
    monkeypatch.setattr(settings, "realism_enabled", True)
    sched = build_schedule(None)  # default: wake 8 sleep 22 peak [10,14]
    sunny = get_activity_probability(sched, 14, "sunny")
    storm = get_activity_probability(sched, 14, "storm")
    assert storm < sunny
    assert storm == pytest.approx(sunny * settings.realism_weather_storm)


def test_weather_ignored_when_realism_off(monkeypatch):
    monkeypatch.setattr(settings, "realism_enabled", False)
    sched = build_schedule(None)
    assert get_activity_probability(sched, 14, "storm") == get_activity_probability(sched, 14, "sunny")


def test_location_is_indoor():
    assert location_is_indoor("academy") is True       # public building
    assert location_is_indoor("central_plaza") is False  # outdoor
    assert location_is_indoor(None) is False


def test_nearest_indoor_from_plaza():
    loc_id = nearest_indoor_location((75, 56))  # central plaza
    assert loc_id is not None and location_is_indoor(loc_id)


@pytest.mark.anyio
async def test_shelter_reroute_in_storm(monkeypatch):
    from app.agent.phases.decide.basic import BasicDecidePlugin
    from app.agent.actions import ActionType
    from app.agent.schemas import TickContext
    monkeypatch.setattr(settings, "realism_enabled", True)
    monkeypatch.setattr(settings, "realism_shelter_prob", 0.6)

    r = MagicMock()
    r.id, r.slug = "r1", "r1"
    r.tile_x, r.tile_y = 75, 56   # central plaza (outdoor)
    r.mood_json = None
    ctx = TickContext(db=AsyncMock(), resident=r, world_time="10:00", hour=10, schedule_phase="上午")
    ctx.world_events = [{"type": "weather", "payload_json": {"kind": "storm"}}]
    ctx.available_actions = [ActionType.VISIT_DISTRICT, ActionType.WANDER, ActionType.IDLE]

    plugin = BasicDecidePlugin()
    with patch("app.agent.phases.decide.basic.random.random", return_value=0.1):  # < 0.6 → reroute
        res = plugin._maybe_shelter(ctx)
    assert res is not None
    assert res.action == ActionType.VISIT_DISTRICT
    assert location_is_indoor(res.target_slug)

    # roll above shelter_prob → no reroute
    with patch("app.agent.phases.decide.basic.random.random", return_value=0.9):
        assert plugin._maybe_shelter(ctx) is None


@pytest.mark.anyio
async def test_apply_weather_mood_rain_depresses(db_session, monkeypatch):
    from app.services.mood_service import apply_weather_mood, get_mood
    from app.models.resident import Resident
    monkeypatch.setattr(settings, "realism_enabled", True)
    r = Resident(slug="w", name="W", creator_id="s", status="idle", tile_x=1, tile_y=1,
                 mood_json={"valence": 0.0, "arousal": 0.5, "label": "calm"})
    db_session.add(r)
    await db_session.commit()
    with patch("app.tasks.weather.get_current_weather", AsyncMock(return_value={"kind": "rain"})):
        n = await apply_weather_mood(db_session, hour=15)
    assert n == 1
    await db_session.refresh(r)
    assert r.mood_json["valence"] < 0.0    # rain lowered valence
