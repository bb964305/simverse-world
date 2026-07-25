"""Regression guard: `civic_service._npc_choice` structural option-0 bias.

Production evidence (docs/reports/ops-audit-2026-07-25B.md §A): on vm212 all
three open civic polls carried **14/14 NPC votes on option 0** (normalised
entropy 0). The 2026-07-25 SBTI backfill closed the *data* gap (26/26 residents
now carry a full 15-dim profile) but a static replay of the shipped scoring
function against the backfilled data still predicted 92.9%–100% on option 0 —
so the monopoly is **structural in the algorithm**, not a data problem:

1. ``A2 == "M"`` is a zero signal (only H-without-effect and L-with-effect
   score), and the production cast is **M=10 / L=3 / H=1** — 10 of 14 NPCs
   score 0.0 on every option;
2. the tie-break ``max(..., key=lambda i: (scores[i], -i))`` is pure index
   order, so every all-zero row lands on index 0;
3. an election-shaped poll gives **every** option an ``effect``, which kills
   the ``H and not eff`` branch and makes the ``L and eff`` bonus uniform —
   a full tie again, back to index 0.

This module pins the production shape (14 NPCs, A2 = M10/L3/H1, ``duty`` all
NULL, 4 options that all carry an effect, proposer not an NPC) and the
acceptance criteria written down *before* the fix:

  * option-0 share ≤ 45%
  * at least 3 of the 4 options receive votes
  * the outcome is deterministic (identical ballot-by-ballot across runs and
    across processes — no ``random``/``hash()`` salt)
"""
import math

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.resident import Resident
from app.models.season import Poll
from app.services import civic_service, relation_service

# Exact production A2 distribution from ops-audit-2026-07-25B §A.4
# (slug, A2); duty is NULL for all 14.
PRODUCTION_NPCS: list[tuple[str, str]] = [
    ("夏洛克-福尔摩斯", "H"),
    ("isabella", "L"),
    ("klaus", "L"),
    ("阿达-洛芙莱斯", "L"),
    ("adam", "M"),
    ("mei", "M"),
    ("tamara", "M"),
    ("夜风侦探", "M"),
    ("夜风侦探-46ff1f", "M"),
    ("夜风侦探-a23160", "M"),
    ("林晚秋", "M"),
    ("格蕾丝-霍珀", "M"),
    ("部署回归图灵0724", "M"),
    ("陈默", "M"),
]

# The mayor-election poll shape: 4 options, every one of them carries an
# effect (this is what disables both SBTI branches in the shipped scorer).
ELECTION_OPTIONS = [
    {"label": "候选甲", "effect": {"type": "mayor", "slug": "cand-a"}},
    {"label": "候选乙", "effect": {"type": "mayor", "slug": "cand-b"}},
    {"label": "候选丙", "effect": {"type": "mayor", "slug": "cand-c"}},
    {"label": "候选丁", "effect": {"type": "mayor", "slug": "cand-d"}},
]


def _npc(slug: str, a2: str) -> Resident:
    return Resident(
        slug=slug, name=slug, district="town_hall", status="idle",
        resident_type="npc", creator_id="sys", tile_x=119, tile_y=53,
        meta_json={"sbti": {"dimensions": {"A2": a2}}},  # duty absent → NULL
    )


async def _seed_production_cast(db) -> None:
    db.add_all([_npc(slug, a2) for slug, a2 in PRODUCTION_NPCS])
    # The proposer on the production building polls (jiang-lin / zhou-dahe) is
    # NOT resident_type='npc', so `by_slug` never resolves it and the whole
    # relationship branch is dead. Reproduce that exactly.
    db.add(Resident(
        slug="jiang-lin", name="江临", district="town_hall", status="idle",
        resident_type="player", creator_id="sys", tile_x=119, tile_y=53,
    ))
    await db.commit()


async def _tally(db, poll: Poll) -> list[int]:
    await db.refresh(poll)
    return [int(o.get("npc_votes", 0)) for o in poll.options_json]


async def _ballots(db, poll: Poll, opts: list[dict]) -> list[int]:
    """Ask `_npc_choice` for every NPC's pick, in a fixed slug order."""
    residents = (await db.execute(
        select(Resident).where(Resident.resident_type == "npc")
    )).scalars().all()
    residents.sort(key=lambda r: r.slug)
    by_slug = {r.slug: r for r in residents}
    return [
        await civic_service._npc_choice(db, r, poll, opts, relation_service, by_slug)
        for r in residents
    ]


@pytest.mark.anyio
async def test_production_shape_option0_share_and_spread(db_session):
    """The acceptance gate: 14 NPCs / 4 all-effect options.

    Pre-fix measurement (2026-07-25, base d6ed5b6): tally [14, 0, 0, 0] →
    option-0 share 100.0%, 1 option with votes. This test was RED then.
    """
    await _seed_production_cast(db_session)
    poll = await civic_service.propose(
        db_session, "镇长选举:谁来当下一任镇长?", ELECTION_OPTIONS,
        proposer_slug="jiang-lin",
    )
    cast = await civic_service.run_npc_voting(db_session)
    assert cast == 14

    tally = await _tally(db_session, poll)
    assert sum(tally) == 14
    share0 = tally[0] / 14
    assert share0 <= 0.45, f"option-0 still dominant: {tally} → {share0:.1%}"
    assert sum(1 for n in tally if n > 0) >= 3, f"votes too concentrated: {tally}"


@pytest.mark.anyio
async def test_production_shape_is_deterministic(db_session):
    """Same fixture, two passes → identical ballot-by-ballot (no RNG, no
    PYTHONHASHSEED-salted builtin hash())."""
    await _seed_production_cast(db_session)
    poll = await civic_service.propose(
        db_session, "镇长选举:谁来当下一任镇长?", ELECTION_OPTIONS,
        proposer_slug="jiang-lin",
    )
    opts = list(poll.options_json)
    first = await _ballots(db_session, poll, opts)
    second = await _ballots(db_session, poll, opts)
    assert first == second, f"non-deterministic ballots: {first} vs {second}"


@pytest.mark.anyio
async def test_building_shape_is_not_a_mirror_monopoly(db_session):
    """The other production poll shape (effect vs status-quo) must not swap one
    monopoly for another: both options have to draw real support."""
    await _seed_production_cast(db_session)
    poll = await civic_service.propose(
        db_session, "在南苑空地兴建一座邮局",
        [{"label": "赞成兴建", "effect": {"type": "dynamic_location", "data": {
            "slug": "post_office", "name": "邮局", "type": "public",
            "bounds": [44, 100, 48, 106], "center": [46, 103],
            "entrance": [46, 100], "description": "小镇邮局",
            "boosted_actions": ["WORK"]}}},
         {"label": "暂缓,维持现状", "effect": None}],
        proposer_slug="jiang-lin",
    )
    await civic_service.run_npc_voting(db_session)
    tally = await _tally(db_session, poll)
    assert sum(tally) == 14
    assert min(tally) >= 3, f"one option monopolises the building poll: {tally}"


@pytest.mark.anyio
async def test_election_shape_entropy_is_not_zero(db_session):
    """Normalised entropy H/lnK — the ops-audit metric, 0 on production."""
    await _seed_production_cast(db_session)
    poll = await civic_service.propose(
        db_session, "镇长选举:谁来当下一任镇长?", ELECTION_OPTIONS,
        proposer_slug="jiang-lin",
    )
    await civic_service.run_npc_voting(db_session)
    tally = await _tally(db_session, poll)
    total = sum(tally)
    h = -sum((n / total) * math.log(n / total) for n in tally if n)
    assert h / math.log(len(tally)) >= 0.75, f"entropy still low: {tally}"


# ── kill switch: both paths are covered ────────────────────────────────

@pytest.mark.anyio
async def test_legacy_kill_switch_restores_the_old_scorer(db_session, monkeypatch):
    """``CIVIC_NPC_CHOICE_LEGACY=true`` must reproduce the pre-fix behaviour
    byte-for-byte — that is what makes it a usable rollback."""
    monkeypatch.setattr(settings, "civic_npc_choice_legacy", True)
    await _seed_production_cast(db_session)
    poll = await civic_service.propose(
        db_session, "镇长选举:谁来当下一任镇长?", ELECTION_OPTIONS,
        proposer_slug="jiang-lin",
    )
    await civic_service.run_npc_voting(db_session)
    assert await _tally(db_session, poll) == [14, 0, 0, 0]


def test_civic_npc_choice_legacy_defaults_off():
    """A bug fix that ships default-off leaves production broken."""
    assert settings.civic_npc_choice_legacy is False


def test_stable_unit_is_reproducible_across_processes():
    """Pins the digest: a regression to ``hash()`` (PYTHONHASHSEED-salted) or to
    ``random`` would move these numbers between runs."""
    v = civic_service._stable_unit("mei", "镇长选举:谁来当下一任镇长?", "0:候选甲:cand-a")
    assert 0.0 <= v < 1.0
    assert round(v, 12) == 0.260063755221, v
    assert civic_service._stable_unit("a", "b", 1) == civic_service._stable_unit("a", "b", 1)
    assert civic_service._stable_unit("a", "b", 1) != civic_service._stable_unit("a", "b", 2)
