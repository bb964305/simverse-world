"""B2 location encounters: nearby resident, probability, cooldown, daily cap."""

from datetime import datetime, UTC
from unittest.mock import AsyncMock, patch

import pytest

from app.models.resident import Resident


@pytest.fixture(autouse=True)
def _reset_enc():
    from app.services import encounter_service as es
    es._reset_for_tests()
    yield
    es._reset_for_tests()


async def _resident(db, slug, tile=(20, 20), status="idle"):
    r = Resident(slug=slug, name="克劳斯", creator_id="system",
                 district="central_plaza", status=status, tile_x=tile[0], tile_y=tile[1])
    db.add(r)
    await db.commit()
    return r


def test_startchat_accepts_context():
    from app.ws.protocol import StartChat
    m = StartChat(resident_slug="klaus", context="你们在图书馆偶遇")
    assert m.context == "你们在图书馆偶遇"


@pytest.mark.anyio
async def test_no_nearby_resident_no_encounter(db_session):
    from app.services import encounter_service as es
    with patch.object(es.manager, "send", new_callable=AsyncMock) as send, \
         patch("app.services.encounter_service.random.random", return_value=0.1):
        assert await es.maybe_encounter(db_session, "u1", "academy") is None
    send.assert_not_awaited()


@pytest.mark.anyio
async def test_encounter_hit_sends_prompt(db_session):
    from app.services import encounter_service as es
    await _resident(db_session, "klaus", (20, 20))
    with patch.object(es.manager, "send", new_callable=AsyncMock) as send, \
         patch("app.services.encounter_service.random.random", return_value=0.1), \
         patch("app.services.encounter_service.random.choice", side_effect=lambda x: x[0]):
        payload = await es.maybe_encounter(db_session, "u1", "academy")
    assert payload is not None
    assert payload["type"] == "encounter_prompt" and payload["resident_slug"] == "klaus"
    assert payload["location_id"] == "academy" and payload["opener"]
    send.assert_awaited_once()


@pytest.mark.anyio
async def test_probability_miss(db_session):
    from app.services import encounter_service as es
    await _resident(db_session, "klaus", (20, 20))
    with patch.object(es.manager, "send", new_callable=AsyncMock) as send, \
         patch("app.services.encounter_service.random.random", return_value=0.9):
        assert await es.maybe_encounter(db_session, "u1", "academy") is None
    send.assert_not_awaited()


@pytest.mark.anyio
async def test_cooldown_blocks_repeat(db_session):
    from app.services import encounter_service as es
    await _resident(db_session, "klaus", (20, 20))
    with patch.object(es.manager, "send", new_callable=AsyncMock), \
         patch("app.services.encounter_service.random.random", return_value=0.1), \
         patch("app.services.encounter_service.random.choice", side_effect=lambda x: x[0]):
        assert await es.maybe_encounter(db_session, "u1", "academy") is not None
        assert await es.maybe_encounter(db_session, "u1", "academy") is None  # cooldown


@pytest.mark.anyio
async def test_daily_cap(db_session):
    from app.services import encounter_service as es
    await _resident(db_session, "klaus", (20, 20))
    es._daily[("u1", datetime.now(UTC).date().isoformat())] = 5
    with patch.object(es.manager, "send", new_callable=AsyncMock), \
         patch("app.services.encounter_service.random.random", return_value=0.1):
        assert await es.maybe_encounter(db_session, "u1", "academy") is None
