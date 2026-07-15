"""Task 3（burn-in 修复批次 1）：夜间归巢——作息门关闭后零 LLM 走回家。"""
import pytest
from unittest.mock import patch


@pytest.mark.anyio
async def test_homing_step_moves_one_tile(db_session):
    from app.models.resident import Resident
    from app.agent.night_homing import night_homing_step

    r = Resident(slug="klaus", name="克劳斯", persona_md="x", creator_id="test-user",
                 tile_x=10, tile_y=10, home_location_id="home-klaus", status="idle")
    db_session.add(r)
    await db_session.commit()

    with patch("app.agent.night_homing.get_valid_target_tile", return_value=(14, 10)), \
         patch("app.agent.night_homing.get_walkable_tiles", return_value=set()), \
         patch("app.agent.night_homing.find_path",
               return_value=[(10, 10), (11, 10), (12, 10)]):
        new_tile = await night_homing_step(db_session, r)

    assert new_tile == (11, 10)
    assert (r.tile_x, r.tile_y) == (11, 10)
    assert r.status == "walking"


@pytest.mark.anyio
async def test_homing_step_arrived_returns_none(db_session):
    from app.models.resident import Resident
    from app.agent.night_homing import night_homing_step

    r = Resident(slug="mei", name="梅", persona_md="x", creator_id="test-user",
                 tile_x=14, tile_y=10, home_location_id="home-mei", status="walking")
    db_session.add(r)
    await db_session.commit()

    with patch("app.agent.night_homing.get_valid_target_tile", return_value=(14, 10)):
        assert await night_homing_step(db_session, r) is None
    assert r.status == "idle"   # 到家收尾


@pytest.mark.anyio
async def test_homing_step_no_home_returns_none(db_session):
    from app.models.resident import Resident
    from app.agent.night_homing import night_homing_step

    r = Resident(slug="adam", name="亚当", persona_md="x", creator_id="test-user",
                 tile_x=5, tile_y=5, home_location_id=None, status="idle")
    db_session.add(r)
    await db_session.commit()
    assert await night_homing_step(db_session, r) is None
