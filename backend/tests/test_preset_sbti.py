"""P2-1: preset residents carry differentiated SBTI dimensions.

Regression guard for the systematic option-0 bias: before this fix the built-in
cast had NO ``sbti.dimensions`` in ``meta_json``, so ``civic_service._npc_choice``
read ``A2`` as the "M" default for everyone — the conservative/rebel branches
never fired and every NPC vote piled onto option 0 (the proposer's lead). This
suite pins (a) full 15-dim coverage, (b) real dimension spread, (c) NPC votes
no longer monopolise option 0, and (d) elections pick ambitious candidates via
Ac1/So1 rather than falling back to heat order.
"""
import pytest
from sqlalchemy import select

from app.models.user import User
from app.models.resident import Resident
from app.services import civic_service, election_service
from app.services.sbti_service import DIMENSION_CODES


async def _seed_system_user(db):
    db.add(User(
        id="00000000-0000-0000-0000-000000000001",
        name="System", email="system@skills.world", soul_coin_balance=0,
    ))
    await db.commit()


async def _seed(db):
    from seed.preset_characters import seed_presets
    await _seed_system_user(db)
    await seed_presets(db)
    return (await db.execute(
        select(Resident).where(Resident.resident_type == "npc")
    )).scalars().all()


@pytest.mark.anyio
async def test_every_preset_has_full_15_dim_sbti(db_session):
    """(a) all 11 built-in residents carry a complete, valid 15-dim profile."""
    residents = await _seed(db_session)
    assert len(residents) == 11
    for r in residents:
        sbti = (r.meta_json or {}).get("sbti")
        assert sbti is not None, f"{r.slug} has no sbti block"
        dims = sbti.get("dimensions")
        assert dims is not None, f"{r.slug} has no sbti.dimensions"
        assert set(dims) == set(DIMENSION_CODES), f"{r.slug} dims incomplete: {sorted(dims)}"
        assert all(v in ("L", "M", "H") for v in dims.values()), f"{r.slug} bad values: {dims}"
        # type series populated from match_type() (not hand-coded)
        assert sbti.get("type") and sbti.get("type_name") and sbti.get("type_en")
        assert isinstance(sbti.get("similarity"), int)
        assert isinstance(sbti.get("exact"), int)


@pytest.mark.anyio
async def test_key_dimensions_are_differentiated(db_session):
    """(b) A2/Ac1/So1/A1 are not constant across the cast — the whole point of
    the fix. A2 in particular must contain both H and non-H so the conservative
    branch in _npc_choice can fire for some residents but not all."""
    residents = await _seed(db_session)

    def col(code):
        return [(r.meta_json or {}).get("sbti", {}).get("dimensions", {}).get(code) for r in residents]

    for code in ("A2", "Ac1", "So1", "A1"):
        vals = set(col(code))
        assert len(vals) >= 2, f"{code} is constant ({vals}) — no differentiation"

    a2 = col("A2")
    assert "H" in a2 and any(v != "H" for v in a2), f"A2 lacks H/non-H spread: {a2}"
    # A1 must span optimistic and skeptical for the lecture-debate contrast path.
    a1 = col("A1")
    assert "H" in a1 and "L" in a1, f"A1 lacks worldview contrast: {a1}"


@pytest.mark.anyio
async def test_npc_votes_no_longer_monopolise_option_0(db_session):
    """(c) behavioural regression on the real cast: a plain effect-vs-status-quo
    poll no longer sends every NPC vote to option 0. Conservative (A2=H)
    residents now split off to the no-effect option."""
    await _seed(db_session)
    poll = await civic_service.propose(
        db_session, "要不要大改",
        [{"label": "大改", "effect": {"type": "narrative", "event": {"title": "x"}}},
         {"label": "维持现状", "effect": None}],
    )
    cast = await civic_service.run_npc_voting(db_session)
    await db_session.refresh(poll)
    opts = poll.options_json
    v0 = int(opts[0].get("npc_votes", 0))  # effect / lead option
    v1 = int(opts[1].get("npc_votes", 0))  # no-effect / status quo

    assert cast == 11
    assert v0 + v1 == 11
    # The core assertion: NOT the pre-fix 11-0 monopoly on option 0.
    assert v0 < 11, f"all votes still on option 0 ({v0}) — bias not fixed"
    # Conservatives (A2=H) peel off to the status-quo option.
    assert v1 >= 5, f"status-quo got too few votes ({v1}) — A2 branch not firing"


@pytest.mark.anyio
async def test_election_picks_ambitious_candidates_not_heat_fallback(db_session):
    """(d) election candidate selection uses the SBTI Ac1=H / So1=H path instead
    of falling back to heat order (all heat=0 before → first-N by insertion)."""
    residents = await _seed(db_session)
    by_slug = {r.slug: r for r in residents}

    poll = await election_service.open_election(db_session)
    assert poll is not None
    assert len(poll.options_json) >= 2
    for o in poll.options_json:
        slug = o["effect"]["slug"]
        dims = (by_slug[slug].meta_json or {}).get("sbti", {}).get("dimensions", {})
        assert dims.get("Ac1") == "H" or dims.get("So1") == "H", (
            f"candidate {slug} is neither ambitious nor socially active — "
            f"election fell back to heat order"
        )
