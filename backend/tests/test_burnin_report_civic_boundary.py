"""线 A hotfix-4 — read-only probe that would have caught the suffrage leak.

The 07-25 audit found the leaked voters by hand. The probe below makes the same
finding automatic and zero-LLM: it groups residents by ``resident_type`` and
reports, per group, how many are inside ``CIVIC_VOTER_TYPES`` (political rights)
versus ``SIM_RESIDENT_TYPES`` (world population). A UGC resident showing up in
the voter column, or an unknown type falling outside *both* columns, is flagged.
"""
import pytest
from sqlalchemy import select

from app.models.resident import Resident
from app.services.civic_membership import UGC_RESIDENT_TYPE
from scripts.burnin_report import (
    civic_boundary_breakdown,
    fetch_civic_boundary_snapshot,
    render_probes_civic_boundary,
)


def _res(slug, rtype, **kw):
    d = dict(slug=slug, name=slug, district="town_hall", status="idle",
             resident_type=rtype, creator_id="sys", tile_x=1, tile_y=1)
    d.update(kw)
    return Resident(**d)


@pytest.mark.anyio
async def test_snapshot_groups_by_resident_type(db_session):
    db_session.add_all([
        _res("b1", "npc"), _res("b2", "npc"),
        _res("u1", UGC_RESIDENT_TYPE),
        _res("a1", "player"),
    ])
    await db_session.commit()

    snap = await fetch_civic_boundary_snapshot(db_session)
    assert snap["available"] is True
    assert snap["by_type"] == {"npc": 2, UGC_RESIDENT_TYPE: 1, "player": 1}


def test_breakdown_separates_voters_from_population():
    snap = {"available": True,
            "by_type": {"npc": 10, UGC_RESIDENT_TYPE: 4, "player": 3}}
    d = civic_boundary_breakdown(snap)

    assert d["total"] == 17
    assert d["voters"] == 10          # only the built-in cast
    assert d["population"] == 14      # built-ins + UGC residents
    assert d["outside_both"] == 3     # the player avatars
    assert d["unknown_types"] == {}
    assert d["leaked_voter_types"] == []


def test_breakdown_flags_a_ugc_type_that_gained_the_ballot():
    """The regression this probe exists for: if UGC residents ever land back
    inside the voter set, say so loudly instead of waiting for an audit."""
    snap = {"available": True, "by_type": {"npc": 10, UGC_RESIDENT_TYPE: 4}}
    d = civic_boundary_breakdown(snap, voter_types=frozenset({"npc", UGC_RESIDENT_TYPE}))
    assert d["leaked_voter_types"] == [UGC_RESIDENT_TYPE]
    assert d["voters"] == 14


def test_breakdown_flags_a_type_in_neither_set():
    """A type outside both sets is a resident nobody's code accounts for —
    ``"preset"`` (admin-created) is the live example and is a *pending product
    decision*, not a bug, so it is reported rather than gated on."""
    snap = {"available": True, "by_type": {"npc": 10, "preset": 2, "mystery": 1}}
    d = civic_boundary_breakdown(snap)
    assert d["unknown_types"] == {"preset": 2, "mystery": 1}
    assert d["outside_both"] == 3


def test_render_is_readable_and_flags_the_leak():
    clean = render_probes_civic_boundary(
        {"available": True, "by_type": {"npc": 10, UGC_RESIDENT_TYPE: 4}})
    assert "resident_type" in clean
    assert "🔴" not in clean

    leaked = render_probes_civic_boundary(
        {"available": True, "by_type": {"npc": 10, UGC_RESIDENT_TYPE: 4}},
        voter_types=frozenset({"npc", UGC_RESIDENT_TYPE}))
    assert "🔴" in leaked
    assert UGC_RESIDENT_TYPE in leaked


def test_render_degrades_when_the_table_is_missing():
    out = render_probes_civic_boundary({"available": False, "by_type": {}})
    assert "🔴" not in out
    assert out.strip() != ""


def test_render_handles_an_empty_world():
    out = render_probes_civic_boundary({"available": True, "by_type": {}})
    assert "🔴" not in out


@pytest.mark.anyio
async def test_probe_is_read_only(db_session):
    """Zero LLM and zero writes — a burn-in probe must never mutate the world."""
    db_session.add(_res("b1", "npc"))
    await db_session.commit()
    before = (await db_session.execute(select(Resident.slug, Resident.meta_json))).all()

    await fetch_civic_boundary_snapshot(db_session)

    assert (await db_session.execute(select(Resident.slug, Resident.meta_json))).all() == before
    assert not db_session.dirty and not db_session.new and not db_session.deleted
