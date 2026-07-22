"""P2 Task 1 — resident_relations + RelationService.

Covers: atomic-upsert accumulation (no lost update), canonical-key dedup
(order-independent single row), familiarity/affinity caps, and weekly decay of
idle relations.
"""
import asyncio
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select, func

from app.services import relation_service as rel
from app.models.resident_relation import ResidentRelation


async def _count(db) -> int:
    return (await db.execute(select(func.count()).select_from(ResidentRelation))).scalar_one()


@pytest.mark.asyncio
async def test_bump_creates_then_accumulates(db_session):
    await rel.bump(db_session, "a", "b", d_familiarity=0.05, d_affinity=0.03)
    r = await rel.get_pair(db_session, "a", "b")
    assert r is not None
    assert r.familiarity == pytest.approx(0.05)
    assert r.affinity == pytest.approx(0.03)
    assert r.interact_count == 1

    # Second bump accumulates on top of the stored value (atomic SET col = col+d,
    # not overwrite) — this is what makes concurrent bumps lossless.
    await rel.bump(db_session, "a", "b", d_familiarity=0.05, d_affinity=0.03)
    r = await rel.get_pair(db_session, "a", "b")
    assert r.familiarity == pytest.approx(0.10)
    assert r.affinity == pytest.approx(0.06)
    assert r.interact_count == 2


@pytest.mark.asyncio
async def test_canonical_key_dedup_order_independent(db_session):
    # (a,b) and (b,a) must land on ONE row.
    await rel.bump(db_session, "zoe", "amy", d_familiarity=0.05)
    await rel.bump(db_session, "amy", "zoe", d_familiarity=0.05)
    assert await _count(db_session) == 1
    r = await rel.get_pair(db_session, "amy", "zoe")
    assert r.familiarity == pytest.approx(0.10)
    # canonical: smaller id first
    assert (r.party_a, r.party_b) == ("amy", "zoe")


@pytest.mark.asyncio
async def test_no_lost_update_over_many_bumps(db_session):
    # 30 sequential bumps of 0.02 → exactly 0.60 (atomic accumulate). A
    # read-modify-write would be at risk of lost updates under concurrency;
    # the atomic UPDATE accumulates deterministically.
    for _ in range(30):
        await rel.bump(db_session, "a", "b", d_familiarity=0.02)
    r = await rel.get_pair(db_session, "a", "b")
    assert r.familiarity == pytest.approx(0.60)
    assert r.interact_count == 30


@pytest.mark.asyncio
async def test_familiarity_capped_at_one(db_session):
    for _ in range(40):  # 40 × 0.05 = 2.0 uncapped
        await rel.bump(db_session, "a", "b", d_familiarity=0.05)
    r = await rel.get_pair(db_session, "a", "b")
    assert r.familiarity == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_affinity_two_sided_cap(db_session):
    for _ in range(50):
        await rel.bump(db_session, "a", "b", d_affinity=-0.1)
    r = await rel.get_pair(db_session, "a", "b")
    assert r.affinity == pytest.approx(-1.0)
    for _ in range(80):
        await rel.bump(db_session, "a", "b", d_affinity=0.1)
    r = await rel.get_pair(db_session, "a", "b")
    assert r.affinity == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_self_pair_is_noop(db_session):
    await rel.bump(db_session, "a", "a", d_familiarity=0.5)
    assert await _count(db_session) == 0


@pytest.mark.asyncio
async def test_top_and_relations_for(db_session):
    await rel.bump(db_session, "hub", "x", d_familiarity=0.1, d_affinity=0.2)
    await rel.bump(db_session, "hub", "y", d_familiarity=0.5, d_affinity=-0.1)
    await rel.bump(db_session, "z", "hub", d_familiarity=0.3, d_affinity=0.4)

    top = await rel.top_relations(db_session, "hub", n=2, by="familiarity")
    assert [v.other_id for v in top] == ["y", "z"]  # 0.5, 0.3

    top_aff = await rel.top_relations(db_session, "hub", n=1, by="affinity")
    assert top_aff[0].other_id == "z"  # affinity 0.4

    mp = await rel.relations_for(db_session, "hub")
    assert set(mp.keys()) == {"x", "y", "z"}
    assert mp["y"].familiarity == pytest.approx(0.5)
    assert mp["z"].affinity == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_player_resident_pair_carries_types(db_session):
    await rel.bump(db_session, "res1", "user1", d_affinity=0.1, type1="resident", type2="player")
    r = await rel.get_pair(db_session, "user1", "res1", type1="player", type2="resident")
    assert r is not None
    types = {r.party_a_type, r.party_b_type}
    assert types == {"resident", "player"}


@pytest.mark.asyncio
async def test_weekly_decay_of_idle_relations(db_session):
    now = datetime.now(UTC)
    # Idle 40 days → decays.
    await rel.bump(db_session, "old", "friend", d_familiarity=0.8, d_affinity=0.5,
                   now=now - timedelta(days=40))
    # Recent → untouched.
    await rel.bump(db_session, "new", "friend", d_familiarity=0.8, d_affinity=0.5,
                   now=now - timedelta(days=5))

    n = await rel.decay(db_session, now=now)
    assert n == 1

    old = await rel.get_pair(db_session, "old", "friend")
    assert old.familiarity == pytest.approx(0.8 * 0.95)
    assert old.affinity == pytest.approx(0.5 * 0.98)

    fresh = await rel.get_pair(db_session, "new", "friend")
    assert fresh.familiarity == pytest.approx(0.8)
    assert fresh.affinity == pytest.approx(0.5)
