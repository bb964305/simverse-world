"""S2-1 §6 — burn-in office probes: occupancy/vacancy, turnover (updated_at
aggregation), mayor identity consistency across the three stores. Pure-function
assertions + a seeded end-to-end fixture that demonstrates the numbers
(recorded in the S2-1 report). Zero LLM — reads offices + system_config only.
"""
from datetime import datetime, timedelta, UTC

import pytest

from app.config import settings
from app.models.resident import Resident
from scripts.burnin_report import (
    fetch_office_snapshot, office_occupancy, mayor_consistency,
    office_turnover, render_probes_offices,
)


def _snap(offices, config_mayor=None, meta_mayors=None, available=True):
    return {
        "available": available,
        "offices": offices,
        "config_mayor": config_mayor,
        "meta_mayors": meta_mayors or [],
    }


def _row(key, holder, updated_at=None, institution="town_hall",
         fill_strategy="seed", term_ends_at=None):
    return {
        "office_key": key, "holder_slug": holder, "institution": institution,
        "fill_strategy": fill_strategy, "term_started_at": None,
        "term_ends_at": term_ends_at,
        "updated_at": updated_at or datetime.now(UTC),
    }


# ------------------------------ occupancy ------------------------------

def test_occupancy_flags_vacancy_and_duration():
    now = datetime.now(UTC)
    snap = _snap([
        _row("mayor", "he-qiaoyun", updated_at=now - timedelta(days=10)),
        _row("doctor", None, updated_at=now - timedelta(days=3),
             institution="clinic", fill_strategy="appointment"),
    ])
    occ = office_occupancy(snap, now=now)
    by_key = {o["office_key"]: o for o in occ}
    assert by_key["mayor"]["occupied"] is True
    assert by_key["mayor"]["vacant_days"] is None
    assert by_key["doctor"]["occupied"] is False
    assert by_key["doctor"]["vacant_days"] == 3


# ------------------------------ turnover -------------------------------

def test_turnover_counts_changes_in_window():
    now = datetime.now(UTC)
    snap = _snap([
        _row("mayor", "a", updated_at=now - timedelta(days=1)),      # in window
        _row("town_clerk", "b", updated_at=now - timedelta(days=40)),  # stale
        _row("postman", None, updated_at=now - timedelta(hours=2)),  # in window
    ])
    t = office_turnover(snap, window_days=7, now=now)
    assert t["changed_in_window"] == 2
    assert t["per_office"]["mayor"] is True
    assert t["per_office"]["town_clerk"] is False
    assert t["per_office"]["postman"] is True


# ----------------------------- consistency -----------------------------

def test_mayor_consistency_gate_on_all_three_agree():
    snap = _snap([_row("mayor", "he-qiaoyun")],
                 config_mayor="he-qiaoyun", meta_mayors=["he-qiaoyun"])
    c = mayor_consistency(snap, gate_on=True)
    assert c["consistent"] is True
    assert c["office"] == "he-qiaoyun" and c["config"] == "he-qiaoyun"


def test_mayor_consistency_gate_on_divergence_alarms():
    snap = _snap([_row("mayor", "he-qiaoyun")],
                 config_mayor="he-qiaoyun", meta_mayors=["someone-else"])
    assert mayor_consistency(snap, gate_on=True)["consistent"] is False
    snap2 = _snap([_row("mayor", "a")], config_mayor="b", meta_mayors=["a"])
    assert mayor_consistency(snap2, gate_on=True)["consistent"] is False


def test_mayor_consistency_gate_off_compares_two_stores_only():
    # offices intentionally out of the comparison when the gate is off
    snap = _snap([_row("mayor", "stale-office-value")],
                 config_mayor="he-qiaoyun", meta_mayors=["he-qiaoyun"])
    c = mayor_consistency(snap, gate_on=False)
    assert c["consistent"] is True


# --------------------------- seeded end-to-end -------------------------

@pytest.mark.anyio
async def test_office_probe_numbers_from_seeded_fixture(db_session, monkeypatch):
    """The §6 demo: a seeded world produces the probe numbers (four offices,
    three occupied, doctor vacant, three-store mayor identity consistent)."""
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    from app.services.config_service import ConfigService
    from app.services.office_service import OfficeService

    db_session.add_all([
        Resident(slug="he-qiaoyun", name="何巧云", district="shop", status="idle",
                 resident_type="npc", creator_id="sys", tile_x=1, tile_y=1,
                 meta_json={"mayor": True}),
        Resident(slug="zhao-qiwen", name="赵启文", district="town_hall", status="idle",
                 resident_type="npc", creator_id="sys", tile_x=2, tile_y=2,
                 meta_json={"duty": {"key": "town_clerk"}}),
        Resident(slug="luo-xiaozhou", name="骆小舟", district="post", status="idle",
                 resident_type="npc", creator_id="sys", tile_x=3, tile_y=3,
                 meta_json={"duty": {"key": "postman"}}),
    ])
    await db_session.commit()
    await ConfigService(db_session).set(
        "current_mayor", "he-qiaoyun", group="civic", updated_by="test")
    svc = OfficeService(db_session)
    await svc.appoint("mayor", "he-qiaoyun", fill_strategy="election")
    await svc.appoint("town_clerk", "zhao-qiwen", fill_strategy="seed")
    await svc.appoint("postman", "luo-xiaozhou", fill_strategy="seed")
    await svc.appoint("doctor", "temp-doc", fill_strategy="appointment")
    await svc.vacate("doctor")   # doctor slot exists, vacant (greenfield state)

    snap = await fetch_office_snapshot(db_session)
    assert snap["available"] is True
    assert len(snap["offices"]) == 4

    occ = office_occupancy(snap)
    occupied = [o for o in occ if o["occupied"]]
    assert len(occupied) == 3
    vacant = [o for o in occ if not o["occupied"]]
    assert [o["office_key"] for o in vacant] == ["doctor"]

    c = mayor_consistency(snap, gate_on=True)
    assert c["consistent"] is True

    t = office_turnover(snap, window_days=7)
    assert t["changed_in_window"] == 4  # all four rows touched just now

    text = render_probes_offices(snap, gate_on=True, window_days=7)
    assert "职位占用" in text and "镇长身份一致性" in text
    assert "doctor" in text


@pytest.mark.anyio
async def test_office_probe_tolerates_missing_table(db_session, monkeypatch):
    """Probe must fail-open when the offices table does not exist (pre-S2-1
    burn-in DB) — render a hint, not a crash."""
    from sqlalchemy import text as sql_text
    await db_session.execute(sql_text("DROP TABLE IF EXISTS offices"))
    await db_session.commit()
    snap = await fetch_office_snapshot(db_session)
    assert snap["available"] is False
    out = render_probes_offices(snap, gate_on=False, window_days=7)
    assert "offices" in out  # hint line, no exception
