"""Realism P1-11: emotion loop — inputs (dream tone/goal/gossip) + outputs
(activity valence modulation, contagion)."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import settings
from app.agent.scheduler import build_schedule, get_activity_probability
from app.models.resident import Resident


def test_activity_valence_modulation(monkeypatch):
    monkeypatch.setattr(settings, "realism_enabled", True)
    sched = build_schedule(None)
    neutral = get_activity_probability(sched, 14, None, False, valence=0.0)
    depressed = get_activity_probability(sched, 14, None, False, valence=-0.8)
    upbeat = get_activity_probability(sched, 14, None, False, valence=0.8)
    assert depressed < neutral < upbeat
    assert depressed == pytest.approx(neutral * (1 + 0.2 * -0.8))


@pytest.mark.anyio
async def test_emotion_contagion_converges(db_session, monkeypatch):
    from app.agent.chat import _apply_contagion
    monkeypatch.setattr(settings, "realism_enabled", True)
    a = Resident(slug="a", name="A", creator_id="s", status="idle", tile_x=1, tile_y=1,
                 mood_json={"valence": 0.6, "arousal": 0.5, "label": "content"})
    b = Resident(slug="b", name="B", creator_id="s", status="idle", tile_x=1, tile_y=1,
                 mood_json={"valence": 0.0, "arousal": 0.5, "label": "calm"})
    db_session.add_all([a, b])
    await db_session.commit()
    await _apply_contagion(db_session, a, b)
    # mean 0.3; a moves down toward mean, b moves up toward mean
    assert a.mood_json["valence"] < 0.6
    assert b.mood_json["valence"] > 0.0
    assert a.mood_json["valence"] > b.mood_json["valence"]  # still ordered, just closer


@pytest.mark.anyio
async def test_gossip_victim_mood(db_session, monkeypatch):
    from app.services import gossip_service
    from app.services.mood_service import get_mood
    from app.memory.service import MemoryService
    monkeypatch.setattr(settings, "realism_enabled", True)
    speaker = Resident(id="sp", slug="sp", name="Sp", creator_id="s", status="idle", tile_x=1, tile_y=1)
    listener = Resident(id="li", slug="li", name="Li", creator_id="s", status="idle", tile_x=1, tile_y=1)
    subject = Resident(id="su", slug="su", name="Su", creator_id="s", status="idle", tile_x=1, tile_y=1,
                       mood_json={"valence": 0.0, "arousal": 0.5, "label": "calm"})
    db_session.add_all([speaker, listener, subject])
    await db_session.commit()
    # speaker holds a high-importance rumor about the subject, already at hops 1
    await MemoryService(db_session).add_memory(
        "sp", "event", "苏做了件糗事", importance=0.8, source="chat_resident",
        related_resident_id="su", metadata_json={"hops": 1},
    )
    with patch("app.services.gossip_service.random.random", return_value=0.1), \
         patch("app.services.gossip_service._distort", AsyncMock(side_effect=lambda c: c)):
        res = await gossip_service.maybe_gossip(db_session, speaker, listener)
    assert res is not None                      # gossip fired at hops 2
    await db_session.refresh(subject)
    assert subject.mood_json["valence"] < 0.0   # being gossiped about stings


@pytest.mark.anyio
async def test_goal_verdict_applies_mood(db_session, monkeypatch):
    from app.services import goal_service
    monkeypatch.setattr(settings, "realism_enabled", True)
    calls = []

    async def fake(db, rid, dv, da=0.0):
        calls.append((rid, dv, da))

    goal = MagicMock()
    goal.title = "成为大师"
    goal.id = "g1"
    with patch("app.services.mood_service.apply_mood_event_by_id", fake), \
         patch("app.memory.service.MemoryService.add_memory", AsyncMock()), \
         patch("app.services.investment_service.settle_goal_investments", AsyncMock()), \
         patch("app.services.feed_service.push", AsyncMock()):
        with patch("app.models.resident.Resident"):
            await goal_service._on_resolved(db_session, "res-1", goal, "achieved")
    assert calls and calls[0][0] == "res-1"
    assert calls[0][1] == settings.realism_goal_achieved_valence


def _mock_dream_client(text: str):
    client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    client.messages.create = AsyncMock(return_value=resp)
    return client


@pytest.mark.anyio
async def test_dream_tone_applies_mood(db_session, monkeypatch):
    from datetime import datetime, UTC
    from app.services import dream_service as ds
    from app.models.memory import Memory
    monkeypatch.setattr(settings, "realism_enabled", True)
    res = Resident(slug="dk", name="DK", creator_id="system", status="idle", tile_x=1, tile_y=1,
                   meta_json={"sbti": {"type": "X"}})
    db_session.add(res)
    await db_session.commit()
    now = datetime.now(UTC)
    for i in range(3):
        db_session.add(Memory(resident_id=res.id, type="event", content=f"事{i}",
                              importance=0.7, source="chat_resident", created_at=now))
    await db_session.commit()

    payload = '{"dream": "我梦见阳光洒满小镇。", "tone": "positive"}'
    with patch.object(ds, "get_client", return_value=_mock_dream_client(payload)), \
         patch.object(ds, "record_usage", new_callable=AsyncMock), \
         patch("app.services.dream_service.random.random", return_value=0.1):
        dream = await ds.generate_dream(db_session, res)
    assert dream is not None and "阳光" in dream.content   # JSON parsed, dream text used
    await db_session.refresh(res)
    assert (res.mood_json or {}).get("valence", 0.0) > 0.0   # positive tone lifted mood
