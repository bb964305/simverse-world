"""Realism P0-5a: heat is the real (decayable) value; pinned_heat holds the
manual/wake floor; display = max(heat, pinned_heat); state uses real value."""
import pytest

from app.config import settings
from app.models.resident import Resident
from app.schemas.resident import ResidentListItem


def test_display_heat_property():
    r = Resident(slug="a", name="A", creator_id="s", heat=5, pinned_heat=10)
    assert r.display_heat == 10
    r2 = Resident(slug="b", name="B", creator_id="s", heat=80, pinned_heat=0)
    assert r2.display_heat == 80


def test_schema_serializes_display_heat():
    r = Resident(id="c-1", slug="c", name="C", creator_id="s", district="free", status="idle",
                 heat=5, pinned_heat=10, sprite_key="x", tile_x=1, tile_y=1,
                 home_location_id=None, star_rating=1, token_cost_per_turn=1, meta_json=None)
    item = ResidentListItem.model_validate(r)
    assert item.heat == 10   # display = max(5, 10)


@pytest.mark.anyio
async def test_realism_on_heat_can_fall(db_session, monkeypatch):
    from app.services.heat_service import recalculate_heat
    monkeypatch.setattr(settings, "realism_enabled", True)
    r = Resident(slug="d", name="D", creator_id="s", district="free", status="idle",
                 heat=80, pinned_heat=0, tile_x=1, tile_y=1)
    db_session.add(r)
    await db_session.commit()
    # No conversations → new_heat computes to 0 → real heat falls to 0 (decays).
    await recalculate_heat(db_session)
    await db_session.refresh(r)
    assert r.heat == 0            # real value fell (allowed under realism)


@pytest.mark.anyio
async def test_realism_off_heat_only_rises(db_session, monkeypatch):
    from app.services.heat_service import recalculate_heat
    monkeypatch.setattr(settings, "realism_enabled", False)
    r = Resident(slug="e", name="E", creator_id="s", district="free", status="idle",
                 heat=80, pinned_heat=0, tile_x=1, tile_y=1)
    db_session.add(r)
    await db_session.commit()
    await recalculate_heat(db_session)
    await db_session.refresh(r)
    assert r.heat == 80           # legacy clamp: never falls
