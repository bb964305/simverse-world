"""P2 Task 6 — gossip as a second-hand information channel.

With the info gradient on, a resident who holds a first-hand world-event memory
(event_id in metadata) can pass it on as gossip; the second-hand memory inherits
the same event_id with hops incremented. Chains extend "知情者→朋友→朋友的朋友".
Gate off → world-event memories never enter the gossip pool (pre-P2).
"""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.services import gossip_service as gs
from app.models.resident import Resident
from app.models.memory import Memory


async def _residents(db, *ids):
    for i in ids:
        db.add(Resident(id=i, slug=i, name=i.upper(), creator_id="sys",
                        district="cafe", status="idle", tile_x=1, tile_y=1))
    await db.commit()


async def _first_hand_event(db, rid, event_id="ev1", importance=0.6):
    db.add(Memory(id=f"{rid}-fh", resident_id=rid, type="event", content="节日开始了",
                  importance=importance, source="world_event",
                  metadata_json={"first_hand": True, "event_id": event_id}))
    await db.commit()


@pytest.mark.anyio
async def test_first_hand_event_becomes_second_hand_gossip(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_info_gradient_enabled", True)
    monkeypatch.setattr(gs, "GOSSIP_PROBABILITY", 1.0)
    monkeypatch.setattr(gs, "_distort", AsyncMock(side_effect=lambda c: c))
    await _residents(db_session, "A", "B")
    await _first_hand_event(db_session, "A", event_id="ev1")

    a = await db_session.get(Resident, "A")
    b = await db_session.get(Resident, "B")
    mem = await gs.maybe_gossip(db_session, a, b)
    assert mem is not None
    assert mem.resident_id == "B"
    assert mem.source == "gossip"
    meta = mem.metadata_json or {}
    assert meta.get("event_id") == "ev1"      # event_id inherited
    assert meta.get("hops") == 1              # first hop


@pytest.mark.anyio
async def test_event_gossip_chains_to_friend_of_friend(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_info_gradient_enabled", True)
    monkeypatch.setattr(gs, "GOSSIP_PROBABILITY", 1.0)
    monkeypatch.setattr(gs, "_distort", AsyncMock(side_effect=lambda c: c))
    await _residents(db_session, "A", "B", "C")
    await _first_hand_event(db_session, "A", event_id="ev1")

    a = await db_session.get(Resident, "A")
    b = await db_session.get(Resident, "B")
    c = await db_session.get(Resident, "C")
    m1 = await gs.maybe_gossip(db_session, a, b)     # A → B (hops 1)
    assert (m1.metadata_json or {}).get("hops") == 1
    m2 = await gs.maybe_gossip(db_session, b, c)     # B → C (hops 2), same event_id
    assert m2 is not None
    assert (m2.metadata_json or {}).get("event_id") == "ev1"
    assert (m2.metadata_json or {}).get("hops") == 2


@pytest.mark.anyio
async def test_gate_off_event_memory_not_gossiped(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_info_gradient_enabled", False)
    monkeypatch.setattr(gs, "GOSSIP_PROBABILITY", 1.0)
    await _residents(db_session, "A", "B")
    await _first_hand_event(db_session, "A", event_id="ev1")   # only a world_event memory
    a = await db_session.get(Resident, "A")
    b = await db_session.get(Resident, "B")
    # With the gradient off the world-event memory is not a gossip candidate → None.
    assert await gs.maybe_gossip(db_session, a, b) is None


@pytest.mark.anyio
async def test_classic_resident_gossip_still_works_gate_off(db_session, monkeypatch):
    # Regression: personal third-party rumors (importance ≥ 0.6, related resident)
    # still gossip with the gradient off, exactly as pre-P2.
    monkeypatch.setattr(settings, "realism_info_gradient_enabled", False)
    monkeypatch.setattr(gs, "GOSSIP_PROBABILITY", 1.0)
    monkeypatch.setattr(gs, "_distort", AsyncMock(side_effect=lambda c: c))
    await _residents(db_session, "A", "B", "S")
    db_session.add(Memory(id="rumor", resident_id="A", type="event", content="S 做了件事",
                          importance=0.7, source="chat_resident", related_resident_id="S"))
    await db_session.commit()
    a = await db_session.get(Resident, "A")
    b = await db_session.get(Resident, "B")
    mem = await gs.maybe_gossip(db_session, a, b)
    assert mem is not None and mem.related_resident_id == "S"
