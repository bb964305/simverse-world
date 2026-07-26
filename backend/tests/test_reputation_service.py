"""S1-1 public reputation regression tests."""
import pytest

from app.config import settings
from app.models.memory import Memory
from app.models.resident import Resident
from app.services import election_service
from app.services.reputation_service import (
    credit_allowed,
    get_many,
    recompute,
    score_from_meta,
)


def _resident(slug: str, *, reputation: float | None = None, mood: float = 0.0):
    meta = {"sbti": {"dimensions": {"Ac1": "H"}}}
    if reputation is not None:
        meta["reputation"] = {"score": reputation, "samples": 1}
    return Resident(
        slug=slug,
        name=slug,
        district="central_plaza",
        status="idle",
        resident_type="npc",
        creator_id=None,
        tile_x=70,
        tile_y=56,
        mood_json={"valence": mood, "arousal": 0.2, "label": "calm"},
        meta_json=meta,
    )


@pytest.mark.anyio
async def test_recompute_disabled_is_noop(db_session, monkeypatch):
    monkeypatch.setattr(settings, "rep_enabled", False)
    resident = _resident("disabled")
    db_session.add(resident)
    await db_session.commit()

    assert await recompute(db_session) == 0
    await db_session.refresh(resident)
    assert "reputation" not in (resident.meta_json or {})


@pytest.mark.anyio
async def test_recompute_uses_gossip_distortion_hops_and_mood(db_session, monkeypatch):
    monkeypatch.setattr(settings, "rep_enabled", True)
    plain = _resident("plain", mood=-0.5)
    distorted = _resident("distorted", mood=-0.5)
    far = _resident("far", mood=-0.5)
    db_session.add_all([plain, distorted, far])
    await db_session.flush()
    db_session.add_all([
        Memory(
            resident_id=plain.id, type="event", content="plain",
            importance=0.7, source="gossip", related_resident_id=plain.id,
            metadata_json={"hops": 0, "distorted": False},
        ),
        Memory(
            resident_id=plain.id, type="event", content="distorted",
            importance=0.7, source="gossip", related_resident_id=distorted.id,
            metadata_json={"hops": 0, "distorted": True},
        ),
        Memory(
            resident_id=plain.id, type="event", content="far",
            importance=0.7, source="gossip", related_resident_id=far.id,
            metadata_json={"hops": 3, "distorted": False},
        ),
    ])
    await db_session.commit()

    assert await recompute(db_session) == 3
    await db_session.refresh(plain)
    await db_session.refresh(distorted)
    await db_session.refresh(far)
    plain_score = score_from_meta(plain.meta_json)
    distorted_score = score_from_meta(distorted.meta_json)
    far_score = score_from_meta(far.meta_json)
    assert distorted_score < plain_score < 0
    assert far_score > plain_score
    assert (plain.meta_json or {})["reputation"]["samples"] == 1


@pytest.mark.anyio
async def test_get_many_and_credit_threshold(db_session, monkeypatch):
    monkeypatch.setattr(settings, "rep_enabled", True)
    low = _resident("low", reputation=-0.8)
    high = _resident("high", reputation=0.8)
    db_session.add_all([low, high])
    await db_session.commit()

    scores = await get_many(db_session, [low.id, high.id, "missing"])
    assert scores[low.id] == -0.8
    assert scores[high.id] == 0.8
    assert scores["missing"] == settings.rep_neutral
    assert credit_allowed(-0.8) is False
    assert credit_allowed(0.8) is True


@pytest.mark.anyio
async def test_open_election_ranks_reputation_when_enabled(db_session, monkeypatch):
    monkeypatch.setattr(settings, "rep_enabled", True)
    low = _resident("low", reputation=-0.5)
    high = _resident("high", reputation=0.9)
    db_session.add_all([low, high])
    await db_session.commit()

    poll = await election_service.open_election(
        db_session,
        candidate_slugs=["low", "high"],
    )
    assert poll.options_json[0]["effect"]["slug"] == "high"
