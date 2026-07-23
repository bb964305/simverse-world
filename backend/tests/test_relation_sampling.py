"""P2 Task 3 — read-path weighted sampling (5 sites).

Seeded RNG throughout (no "run it thrice" — deterministic given the seed):
  helper: weighted_pick (weights + ε uniform mass), turns_for_familiarity
  (a) encounter  — weight 1 + 2×familiarity
  (b) greeting   — highest-affinity idle candidate (not first idle)
  (c) gossip     — rumor weighted by familiarity with its subject
  (d) CHAT target— weight 0.5 + fam + max(0,aff), ε=0.1 uniform mix
  (e) turns      — familiarity → [3,8] band
All fall back to uniform / first-idle / importance-first when the gate is off.
"""
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.config import settings
from app.services import relation_service as rel
from app.services.relation_service import RelationView
from app.models.user import User
from app.models.resident import Resident
from app.models.memory import Memory


def _view(other_id, fam=0.0, aff=0.0):
    return RelationView(other_id=other_id, other_type="resident", familiarity=fam,
                        affinity=aff, interact_count=1, last_interact_at=None)


# ------------------------------- helpers ---------------------------------------

def test_weighted_pick_epsilon_mass_reaches_zero_weight():
    items = ["A", "B"]
    wf = lambda x: 10.0 if x == "A" else 0.0
    # No epsilon → the zero-weight item is never chosen.
    rng = random.Random(0)
    counts = {"A": 0, "B": 0}
    for _ in range(2000):
        counts[rel.weighted_pick(items, wf, rng, epsilon=0.0)] += 1
    assert counts["B"] == 0
    # ε=0.1 → the zero-weight item becomes reachable (≈ε/2 of picks).
    rng = random.Random(0)
    counts = {"A": 0, "B": 0}
    for _ in range(2000):
        counts[rel.weighted_pick(items, wf, rng, epsilon=0.1)] += 1
    assert counts["B"] > 0
    assert counts["A"] > counts["B"]      # strong tie still dominates


def test_turns_for_familiarity_interpolates():
    assert rel.turns_for_familiarity(0.0) == 3
    assert rel.turns_for_familiarity(0.1) in (3, 4)      # near-stranger: 3-4
    assert 3 <= rel.turns_for_familiarity(0.5) <= 6
    assert rel.turns_for_familiarity(0.9) in (7, 8)      # close tie: 6-8
    assert rel.turns_for_familiarity(1.0) == 8
    # monotonic non-decreasing
    vals = [rel.turns_for_familiarity(f / 10) for f in range(11)]
    assert vals == sorted(vals)


# --------------------------- (d) CHAT target -----------------------------------

def _spontaneous():
    from app.agent.phases.decide.spontaneous import SpontaneousDecidePlugin
    return SpontaneousDecidePlugin()


def _ctx(relations):
    from app.agent.schemas import TickContext
    ctx = TickContext(db=None, resident=SimpleNamespace(id="me"),
                      world_time="", hour=12, schedule_phase="free")
    ctx.relations = relations
    return ctx


@pytest.mark.anyio
async def test_chat_target_weighted_with_epsilon(monkeypatch):
    monkeypatch.setattr(settings, "realism_relations_enabled", True)
    plug = _spontaneous()
    friend = SimpleNamespace(id="friend", slug="friend", status="idle")
    stranger = SimpleNamespace(id="stranger", slug="stranger", status="idle")
    ctx = _ctx({"friend": _view("friend", fam=0.9, aff=0.9)})  # stranger has no relation
    rng = random.Random(0)
    counts = {"friend": 0, "stranger": 0}
    for _ in range(3000):
        t = await plug._pick_chat_target(ctx, [friend, stranger], rng=rng)
        counts[t.id] += 1
    # High familiarity+affinity chosen significantly more often ...
    assert counts["friend"] > counts["stranger"] * 2
    # ... yet the stranger stays reachable (ε=0.1 mixing mass — circles must not
    # ossify).
    assert counts["stranger"] > 100


@pytest.mark.anyio
async def test_chat_target_gated_off_is_uniform(monkeypatch):
    monkeypatch.setattr(settings, "realism_relations_enabled", False)
    plug = _spontaneous()
    friend = SimpleNamespace(id="friend", slug="friend", status="idle")
    stranger = SimpleNamespace(id="stranger", slug="stranger", status="idle")
    ctx = _ctx({"friend": _view("friend", fam=0.9, aff=0.9)})
    rng = random.Random(0)
    counts = {"friend": 0, "stranger": 0}
    for _ in range(3000):
        t = await plug._pick_chat_target(ctx, [friend, stranger], rng=rng)
        counts[t.id] += 1
    frac = counts["stranger"] / 3000
    assert 0.4 < frac < 0.6            # uniform: relations ignored


# ------------------------------ (a) encounter ----------------------------------

@pytest.mark.anyio
async def test_encounter_prefers_familiar(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_relations_enabled", True)
    from app.services import encounter_service as es
    from app.redis_client import get_redis
    # Always fire (base prob 1.0) and lift the daily cap so we can sample the pick.
    monkeypatch.setattr(es, "ENCOUNTER_BASE_PROB", 1.0)
    monkeypatch.setattr(es, "ENCOUNTER_DAILY_CAP", 10 ** 9)
    # Two idle residents inside the library bounds; player is very familiar with A.
    from app.agent.map_data import get_location_by_id
    loc = get_location_by_id("library")
    x1, y1, x2, y2 = loc["bounds"]
    db_session.add_all([
        Resident(id="ea", slug="ea", name="A", creator_id="sys", status="idle", tile_x=x1, tile_y=y1),
        Resident(id="eb", slug="eb", name="B", creator_id="sys", status="idle", tile_x=x1, tile_y=y1),
    ])
    await db_session.commit()
    await rel.bump(db_session, "user1", "ea", d_familiarity=0.9, type1="player", type2="resident")

    r = get_redis()
    counts = {"ea": 0, "eb": 0}
    with patch.object(es.manager, "send", new=AsyncMock(return_value=None)):
        for i in range(300):
            await r.delete(es._cooldown_key("user1", "library"))   # bypass 1h cooldown
            payload = await es.maybe_encounter(db_session, "user1", "library", rng=random.Random(i))
            counts[payload["resident_slug"]] += 1
    assert counts["ea"] > counts["eb"] * 2      # familiar resident surfaced far more
    assert counts["eb"] > 0                       # but strangers still possible


# ------------------------------ (c) gossip -------------------------------------

@pytest.mark.anyio
async def test_gossip_prefers_familiar_subject(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_relations_enabled", True)
    from app.services import gossip_service as gs
    monkeypatch.setattr(gs, "GOSSIP_PROBABILITY", 1.0)          # always proceed
    monkeypatch.setattr(gs, "_distort", AsyncMock(side_effect=lambda c: c))  # no LLM

    speaker = Resident(id="spk", slug="spk", name="S", creator_id="sys", status="idle", tile_x=1, tile_y=1)
    listener = Resident(id="lis", slug="lis", name="L", creator_id="sys", status="idle", tile_x=1, tile_y=1)
    db_session.add_all([speaker, listener])
    # Two rumors: about a close subject vs a barely-known subject (both importance 0.7).
    db_session.add(Memory(id="m_close", resident_id="spk", type="event", content="about close",
                          importance=0.7, source="chat", related_resident_id="subj_close"))
    db_session.add(Memory(id="m_far", resident_id="spk", type="event", content="about far",
                          importance=0.7, source="chat", related_resident_id="subj_far"))
    await db_session.commit()
    await rel.bump(db_session, "spk", "subj_close", d_familiarity=0.9)
    await rel.bump(db_session, "spk", "subj_far", d_familiarity=0.05)

    subjects = {"subj_close": 0, "subj_far": 0}
    for i in range(400):
        mem = await gs.maybe_gossip(db_session, speaker, listener, rng=random.Random(i))
        if mem is not None:
            subjects[mem.related_resident_id] += 1
        # clean up the new gossip memory so candidates stay stable
        if mem is not None:
            await db_session.delete(mem)
            await db_session.commit()
    assert subjects["subj_close"] > subjects["subj_far"] * 2   # flows along strong ties
    assert subjects["subj_far"] > 0                            # weak ties still possible


# ------------------------------ (b) greeting -----------------------------------

@pytest.fixture
def greet_env(db_engine):
    from app.services import greeting_service as gs
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch.object(gs, "async_session", factory):
        yield gs


async def _seed_greet(db):
    db.add(User(id="pu", name="P", email="pu@t.co"))
    # Two idle residents; player likes B more (higher affinity) but A has higher
    # relationship-memory importance (so pre-P2 "first idle" would greet A).
    db.add(Resident(id="ga", slug="ga", name="A", creator_id="sys", status="idle", tile_x=1, tile_y=1))
    db.add(Resident(id="gb", slug="gb", name="B", creator_id="sys", status="idle", tile_x=1, tile_y=1))
    db.add(Memory(id="rel_a", resident_id="ga", type="relationship", related_user_id="pu",
                  content="old acquaintance", importance=0.9, source="relationship"))
    db.add(Memory(id="rel_b", resident_id="gb", type="relationship", related_user_id="pu",
                  content="dear friend", importance=0.5, source="relationship"))
    await db.commit()


@pytest.mark.anyio
async def test_greeting_picks_highest_affinity(db_session, greet_env, monkeypatch):
    monkeypatch.setattr(settings, "realism_relations_enabled", True)
    gs = greet_env
    await _seed_greet(db_session)
    await rel.bump(db_session, "pu", "ga", d_affinity=0.1, type1="player", type2="resident")
    await rel.bump(db_session, "pu", "gb", d_affinity=0.9, type1="player", type2="resident")

    with patch.object(gs.manager, "is_online", new=AsyncMock(return_value=False)):
        await gs.maybe_greet("pu")
    # The greeting memory is written for whoever greeted → expect B (highest affinity).
    from sqlalchemy import select
    greeter = (await db_session.execute(
        select(Memory.resident_id).where(Memory.source == "greeting")
    )).scalars().all()
    assert greeter == ["gb"]


@pytest.mark.anyio
async def test_greeting_gated_off_is_first_idle(db_session, greet_env, monkeypatch):
    monkeypatch.setattr(settings, "realism_relations_enabled", False)
    gs = greet_env
    await _seed_greet(db_session)
    await rel.bump(db_session, "pu", "ga", d_affinity=0.1, type1="player", type2="resident")
    await rel.bump(db_session, "pu", "gb", d_affinity=0.9, type1="player", type2="resident")
    with patch.object(gs.manager, "is_online", new=AsyncMock(return_value=False)):
        await gs.maybe_greet("pu")
    from sqlalchemy import select
    greeter = (await db_session.execute(
        select(Memory.resident_id).where(Memory.source == "greeting")
    )).scalars().all()
    assert greeter == ["ga"]     # first by relationship-memory importance (pre-P2)
