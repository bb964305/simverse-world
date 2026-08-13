"""P2 Task 5 — information gradient (abolish the omniscient broadcast).

Gate off → every active resident gets a first-hand event memory (pre-P2).
Gate on: non-weather events inform < 50% first-hand (geo-related @0.6 + a 20%
sample @0.5); weather stays all-broadcast; first-hand memories carry event_id.
"""
import random

import pytest
from sqlalchemy import select, func

from app.config import settings
from app.services import world_event_service as wes
from app.models.resident import Resident
from app.models.memory import Memory


async def _residents(db, n, tile=(0, 0)):
    for i in range(n):
        db.add(Resident(id=f"r{i}", slug=f"r{i}", name=f"R{i}", creator_id="sys",
                        district="cafe", status="idle", tile_x=tile[0], tile_y=tile[1]))
    await db.commit()


async def _first_hand(db):
    rows = (await db.execute(
        select(Memory.resident_id, Memory.importance, Memory.metadata_json)
        .where(Memory.source == "world_event")
    )).all()
    return rows


@pytest.mark.anyio
async def test_gate_off_broadcasts_to_all(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_info_gradient_enabled", False)
    await _residents(db_session, 20)
    n = await wes.write_collective_memories(
        db_session, {"id": "ev", "type": "news", "description": "news", "payload_json": {}})
    assert n == 20
    assert len(await _first_hand(db_session)) == 20


@pytest.mark.anyio
async def test_collective_memory_excludes_player_avatars(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_info_gradient_enabled", False)
    await _residents(db_session, 2)
    db_session.add(Resident(
        id="avatar", slug="avatar", name="Avatar", creator_id="player",
        resident_type="player", district="cafe", status="idle",
        tile_x=0, tile_y=0,
    ))
    await db_session.commit()

    n = await wes.write_collective_memories(
        db_session, {"id": "ev", "type": "news", "description": "news",
                     "payload_json": {}})
    assert n == 2
    assert {row[0] for row in await _first_hand(db_session)} == {"r0", "r1"}


@pytest.mark.anyio
async def test_weather_stays_all_broadcast(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_info_gradient_enabled", True)
    await _residents(db_session, 20)
    n = await wes.write_collective_memories(
        db_session, {"id": "w", "type": "weather", "description": "storm", "payload_json": {}})
    assert n == 20        # sky is visible to everyone


@pytest.mark.anyio
async def test_non_weather_informs_minority_with_event_id(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_info_gradient_enabled", True)
    await _residents(db_session, 20)     # no geo location → only the random sample
    n = await wes.write_collective_memories(
        db_session, {"id": "ev1", "type": "news", "description": "big news", "payload_json": {}},
        rng=random.Random(0))
    assert n == round(0.2 * 20) == 4      # 20% well-informed sample
    rows = await _first_hand(db_session)
    assert len(rows) == 4
    assert n / 20 < 0.5                    # informed ratio < 50% (non-weather)
    # every first-hand memory carries the event_id
    for rid, imp, meta in rows:
        assert (meta or {}).get("event_id") == "ev1"
        assert (meta or {}).get("first_hand") is True
        assert imp == pytest.approx(0.5)   # sample importance


@pytest.mark.anyio
async def test_geo_related_get_higher_importance(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_info_gradient_enabled", True)
    from app.agent.map_data import get_location_by_id
    loc = get_location_by_id("market_hall")
    center = loc.get("center") or (
        (loc["bounds"][0] + loc["bounds"][2]) // 2, (loc["bounds"][1] + loc["bounds"][3]) // 2)
    # Two residents at the market-hall center (geo-relevant), plus far-away others.
    db_session.add(Resident(id="near1", slug="near1", name="N1", creator_id="sys",
                            district="cafe", status="idle", tile_x=center[0], tile_y=center[1]))
    db_session.add(Resident(id="near2", slug="near2", name="N2", creator_id="sys",
                            district="cafe", status="idle", tile_x=center[0], tile_y=center[1]))
    for i in range(18):
        db_session.add(Resident(id=f"far{i}", slug=f"far{i}", name=f"F{i}", creator_id="sys",
                                district="cafe", status="idle", tile_x=center[0] + 500, tile_y=center[1] + 500))
    await db_session.commit()

    await wes.write_collective_memories(
        db_session,
        {"id": "market", "type": "festival", "description": "集市日",
         # Compatibility path: an already-scheduled legacy row still names the plaza.
         "payload_json": {"market_day": True, "location_id": "central_plaza"}},
        rng=random.Random(0))

    rows = {rid: imp for rid, imp, _ in await _first_hand(db_session)}
    # the two plaza residents are informed first-hand at geo importance 0.6
    assert rows.get("near1") == pytest.approx(0.6)
    assert rows.get("near2") == pytest.approx(0.6)
    # still a minority overall (< 50% of 20)
    assert len(rows) < 10
