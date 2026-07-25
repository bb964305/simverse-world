"""Roadmap #11: production-resident SBTI backfill tool (scripts/sbti_backfill.py).

Covers the three production states found in the archived S0 audit
(archive/2026-07-25/docs/PROGRESS.md 遗留跟进 (a)): residents with NO sbti
block at all, residents with a forge-time
``type`` but sparse ``dimensions`` (0/26 prod residents had an A2 key → the
`_npc_choice` option-0 voting bias), and residents already complete.

Invariants under test:
- dry-run (default) reports the diff but writes nothing;
- rule mode fills missing dims from the declared type's own pattern
  (reverse of sbti_service.match_type, mirroring seed/preset_characters.py);
- existing dimension values are preserved, only holes are filled;
- idempotent: a second --apply run is all skip_complete;
- --slug narrows the scan; --llm recomputes from persona via compute_sbti
  and stops at budget PLAYER_ONLY (circuit breaker).
"""
import pytest
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.models.resident import Resident
from app.services.sbti_service import DIMENSION_CODES, match_type
from scripts import sbti_backfill
from scripts.sbti_backfill import backfill, classify, rule_fill

pytestmark = pytest.mark.anyio


# CTRL pattern "HHH-HMH-MHH-HHH-MHM" in DIMENSION_CODES order.
CTRL_DIMS = {
    "S1": "H", "S2": "H", "S3": "H",
    "E1": "H", "E2": "M", "E3": "H",
    "A1": "M", "A2": "H", "A3": "H",
    "Ac1": "H", "Ac2": "H", "Ac3": "H",
    "So1": "M", "So2": "H", "So3": "M",
}

FULL_DIMS = {code: "M" for code in DIMENSION_CODES} | {"S1": "H", "A2": "L"}


def _resident(slug: str, meta_json: dict | None) -> Resident:
    return Resident(
        slug=slug, name=slug, district="free", status="idle",
        ability_md="能力：一个有足够长文本的测试居民，写满五十个字符以上以通过 compute_sbti 的长度门。",
        persona_md="人格：温和但有主见。", soul_md="灵魂：想被这个小镇记住。",
        meta_json=meta_json,
    )


@pytest.fixture
async def three_state_residents(db_session):
    """Seed the three production states: missing / partial(type,no A2) / complete."""
    r_missing = _resident("bf-missing", {"duty": {"key": "shop_keeper"}})
    r_partial = _resident("bf-partial", {"sbti": {
        "type": "CTRL", "type_name": "拿捏者", "type_en": "The Handler",
        # forge-time sparse dims: has a few keys, NO A2 (the prod signature)
        "dimensions": {"S1": "L", "So1": "H"},
        "similarity": 70, "exact": 9,
    }})
    complete = match_type(FULL_DIMS)
    r_complete = _resident("bf-complete", {"sbti": {
        "type": complete["type"], "type_name": complete["type_name"],
        "type_en": complete["type_en"], "dimensions": dict(FULL_DIMS),
        "similarity": complete["similarity"], "exact": complete["exact"],
    }})
    db_session.add_all([r_missing, r_partial, r_complete])
    await db_session.commit()
    return r_missing, r_partial, r_complete


async def _fresh_meta(db_session, slug: str) -> dict | None:
    r = (await db_session.execute(
        select(Resident).where(Resident.slug == slug)
    )).scalar_one()
    await db_session.refresh(r)
    return r.meta_json


# ---------- classify ----------

def test_classify_three_states():
    assert classify(None) == "missing"
    assert classify({}) == "missing"
    assert classify({"type": "CTRL", "dimensions": {"S1": "H"}}) == "partial"
    assert classify({"type": "CTRL"}) == "partial"
    assert classify({"type": "X", "dimensions": dict(FULL_DIMS)}) == "complete"


# ---------- rule_fill ----------

def test_rule_fill_fills_holes_from_type_pattern_and_keeps_existing():
    filled = rule_fill({"type": "CTRL", "dimensions": {"S1": "L", "So1": "H"}})
    assert filled is not None
    dims = filled["dimensions"]
    assert set(dims) == set(DIMENSION_CODES)
    # holes come from the CTRL pattern...
    assert dims["A2"] == CTRL_DIMS["A2"] == "H"
    assert dims["E2"] == CTRL_DIMS["E2"] == "M"
    # ...but existing (forge-computed) values are preserved
    assert dims["S1"] == "L"
    assert dims["So1"] == "H"
    # declared type identity is kept stable
    assert filled["type"] == "CTRL"
    assert filled["type_name"] == "拿捏者"
    assert 0 <= filled["similarity"] <= 100
    assert 0 <= filled["exact"] <= 15


def test_rule_fill_exact_pattern_scores_perfect():
    filled = rule_fill({"type": "CTRL", "dimensions": {}})
    assert filled["dimensions"] == CTRL_DIMS
    assert filled["similarity"] == 100
    assert filled["exact"] == 15


def test_rule_fill_impossible_without_known_pattern():
    assert rule_fill(None) is None                      # no sbti at all
    assert rule_fill({"dimensions": {"S1": "H"}}) is None  # no type
    assert rule_fill({"type": "HHHH"}) is None          # special type, no pattern
    assert rule_fill({"type": "DRUNK"}) is None
    assert rule_fill({"type": "NOPE"}) is None          # unknown type


# ---------- dry-run (default) ----------

async def test_dry_run_reports_diff_but_writes_nothing(db_session, three_state_residents):
    report = await backfill(db_session, apply=False)
    by_slug = {e["slug"]: e for e in report}

    assert by_slug["bf-complete"]["action"] == "skip_complete"
    assert by_slug["bf-partial"]["action"] == "would_fill"
    assert by_slug["bf-partial"]["source"] == "rule"
    assert "A2" in by_slug["bf-partial"]["missing_dims"]
    assert by_slug["bf-missing"]["action"] == "needs_llm"

    # nothing was written
    partial_meta = await _fresh_meta(db_session, "bf-partial")
    assert "A2" not in partial_meta["sbti"]["dimensions"]
    missing_meta = await _fresh_meta(db_session, "bf-missing")
    assert "sbti" not in (missing_meta or {})


# ---------- apply ----------

async def test_apply_fills_partial_and_is_idempotent(db_session, three_state_residents):
    report = await backfill(db_session, apply=True)
    by_slug = {e["slug"]: e for e in report}
    assert by_slug["bf-partial"]["action"] == "filled"
    assert by_slug["bf-complete"]["action"] == "skip_complete"
    assert by_slug["bf-missing"]["action"] == "needs_llm"  # rule cannot invent dims

    meta = await _fresh_meta(db_session, "bf-partial")
    dims = meta["sbti"]["dimensions"]
    assert set(dims) == set(DIMENSION_CODES)
    assert dims["A2"] == "H"          # from the CTRL pattern
    assert dims["S1"] == "L"          # preserved forge value
    assert meta["sbti"]["type"] == "CTRL"

    # complete resident untouched
    meta_c = await _fresh_meta(db_session, "bf-complete")
    assert meta_c["sbti"]["dimensions"] == FULL_DIMS

    # idempotent: second run has nothing left to do
    report2 = await backfill(db_session, apply=True)
    by_slug2 = {e["slug"]: e for e in report2}
    assert by_slug2["bf-partial"]["action"] == "skip_complete"


async def test_slug_filter_limits_scan(db_session, three_state_residents):
    report = await backfill(db_session, slugs=["bf-partial"], apply=False)
    assert [e["slug"] for e in report] == ["bf-partial"]


# ---------- --llm mode ----------

async def test_llm_mode_recomputes_missing_from_persona(
    db_session, three_state_residents, monkeypatch
):
    llm_result = match_type(dict(CTRL_DIMS))
    llm_result["dimensions"] = dict(CTRL_DIMS)
    fake_compute = AsyncMock(return_value=llm_result)
    monkeypatch.setattr(sbti_backfill, "compute_sbti", fake_compute)

    report = await backfill(db_session, use_llm=True, apply=True)
    by_slug = {e["slug"]: e for e in report}
    assert by_slug["bf-missing"]["action"] == "filled"
    assert by_slug["bf-missing"]["source"] == "llm"
    # partial one still prefers the zero-cost rule path
    assert by_slug["bf-partial"]["source"] == "rule"
    assert fake_compute.await_count == 1

    meta = await _fresh_meta(db_session, "bf-missing")
    assert set(meta["sbti"]["dimensions"]) == set(DIMENSION_CODES)


async def test_llm_mode_stops_on_budget_player_only(
    db_session, three_state_residents, monkeypatch
):
    from app.llm.budget import BudgetTier
    fake_compute = AsyncMock()
    monkeypatch.setattr(sbti_backfill, "compute_sbti", fake_compute)
    monkeypatch.setattr(
        sbti_backfill, "background_tier", AsyncMock(return_value=BudgetTier.PLAYER_ONLY)
    )

    report = await backfill(db_session, use_llm=True, apply=True)
    by_slug = {e["slug"]: e for e in report}
    assert by_slug["bf-missing"]["action"] == "skip_budget_exhausted"
    fake_compute.assert_not_awaited()
    meta = await _fresh_meta(db_session, "bf-missing")
    assert "sbti" not in (meta or {})


async def test_llm_failure_is_reported_not_written(
    db_session, three_state_residents, monkeypatch
):
    monkeypatch.setattr(sbti_backfill, "compute_sbti", AsyncMock(return_value=None))
    report = await backfill(db_session, use_llm=True, apply=True)
    by_slug = {e["slug"]: e for e in report}
    assert by_slug["bf-missing"]["action"] == "llm_failed"
    meta = await _fresh_meta(db_session, "bf-missing")
    assert "sbti" not in (meta or {})
