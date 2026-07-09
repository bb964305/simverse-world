"""E1 emotion engine: label mapping, event bumps, decay, prompt injection."""

import pytest
from sqlalchemy import select

from app.models.user import User
from app.models.resident import Resident


def test_label_for_quadrants():
    from app.services.mood_service import label_for
    assert label_for(0.8, 0.8) == "excited"
    assert label_for(0.2, 0.7) == "content"
    assert label_for(0.3, 0.2) == "calm"
    assert label_for(-0.8, 0.8) == "furious"
    assert label_for(-0.3, 0.7) == "annoyed"
    assert label_for(-0.6, 0.2) == "gloomy"


async def _resident(db, slug="klaus", mood=None):
    r = Resident(slug=slug, name="克劳斯", creator_id="system",
                 district="central_plaza", status="idle", tile_x=1, tile_y=1, mood_json=mood)
    db.add(r)
    await db.commit()
    return r


@pytest.mark.anyio
async def test_apply_mood_event_raises_label(db_session):
    from app.services.mood_service import apply_mood_event, get_mood
    r = await _resident(db_session)
    assert get_mood(r)["label"] == "calm"

    mood = await apply_mood_event(db_session, r, dv=0.25, da=0.1)
    assert mood["valence"] == 0.25
    assert mood["label"] in ("content", "excited")  # moved up from calm


@pytest.mark.anyio
async def test_decay_regresses_toward_neutral(db_session):
    from app.services.mood_service import decay_all
    r = await _resident(db_session, mood={"valence": 0.8, "arousal": 0.9, "label": "excited", "updated_at": "x"})

    await decay_all(db_session)
    await db_session.refresh(r)
    assert r.mood_json["valence"] < 0.8  # moved toward 0

    for _ in range(60):
        await decay_all(db_session)
    await db_session.refresh(r)
    assert abs(r.mood_json["valence"]) < 0.1  # essentially neutral
    assert r.mood_json["label"] in ("calm", "content", "tired")


def test_decide_prompt_includes_mood():
    from app.agent.prompts import build_decision_prompt
    resident = Resident(slug="r1", name="小明", district="central_plaza", status="idle",
                        tile_x=0, tile_y=0, meta_json={},
                        mood_json={"valence": -0.6, "arousal": 0.3, "label": "gloomy", "updated_at": "x"})
    _system, user = build_decision_prompt(
        resident=resident, schedule_phase="day", world_time="10:00",
        nearby_residents=[], memories=[], today_actions=[], available_actions=[], max_daily_actions=10,
    )
    assert "当前心情：gloomy" in user
    assert "独处" in user  # low-valence behavior hint


def test_player_prompt_includes_mood():
    from app.llm.prompt import assemble_system_prompt
    resident = Resident(slug="r1", name="小明", district="central_plaza", status="idle",
                        tile_x=0, tile_y=0, soul_md="", persona_md="", ability_md="",
                        mood_json={"valence": 0.7, "arousal": 0.8, "label": "excited", "updated_at": "x"})
    prompt = assemble_system_prompt(resident)
    assert "excited" in prompt


@pytest.mark.anyio
async def test_gift_raises_resident_mood(db_session):
    from app.services.shop_service import purchase, seed_items
    await seed_items(db_session)
    buyer = User(name="b", email="moodbuy@test.com", soul_coin_balance=200)
    db_session.add(buyer)
    await db_session.commit()
    res = await _resident(db_session, slug="giftee")

    await purchase(db_session, buyer.id, "gift_flower", 1, {"resident_slug": "giftee"})
    await db_session.refresh(res)
    assert res.mood_json is not None and res.mood_json["valence"] > 0
