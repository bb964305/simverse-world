"""S1-5 §6 — burn-in town-treasury probes: balance snapshot / money split /
wage-funding runway, plus the gate-off control group. Pure-function assertions
plus a seeded end-to-end fixture whose numbers are recorded in the S1-5 report.
Zero LLM — reads town_treasuries + resident_treasuries + system_config only.
"""
import pytest

from app.config import settings
from app.models.resident import Resident
from app.services import coin_service, treasury_service
from scripts.burnin_report import (
    fetch_treasury_snapshot, render_probes_s15, treasury_money_split,
    treasury_wage_runway,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _npc(slug, name, duty=None, **kw):
    meta = {"duty": duty} if duty else {}
    d = dict(slug=slug, name=name, district="workshop", status="idle",
             resident_type="npc", tile_x=116, tile_y=27, meta_json=meta)
    d.update(kw)
    return Resident(**d)


# ── pure functions ─────────────────────────────────────────────────────

def test_money_split_and_share():
    split = treasury_money_split({"town_balance_sc": 40, "resident_total_sc": 60})
    assert split["npc_money_supply_sc"] == 100
    assert split["town_share"] == 0.4


def test_money_split_empty_world_does_not_divide_by_zero():
    split = treasury_money_split({})
    assert split == {"town_sc": 0, "resident_sc": 0,
                     "npc_money_supply_sc": 0, "town_share": 0.0}


def test_wage_runway_flags_the_at_risk_window():
    ok = treasury_wage_runway({"town_balance_sc": 100, "daily_wage_bill_sc": 20,
                               "duty_holders": 4})
    assert ok["runway_days"] == 5.0 and ok["at_risk"] is False
    short = treasury_wage_runway({"town_balance_sc": 5, "daily_wage_bill_sc": 20,
                                  "duty_holders": 4})
    assert short["runway_days"] == 0.25 and short["at_risk"] is True


def test_wage_runway_none_without_a_wage_bill():
    """No sitting duty holders → no denominator; the probe reports '-' rather
    than inventing one."""
    assert treasury_wage_runway({"town_balance_sc": 100, "daily_wage_bill_sc": 0}) is None


def test_render_handles_missing_table():
    out = render_probes_s15({"available": False}, gate_on=True)
    assert "town_treasuries 表不存在" in out


# ── seeded end-to-end snapshot (the numbers quoted in the report) ───────

@pytest.mark.anyio
async def test_seeded_treasury_probe_numbers(db_session, monkeypatch):
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_tax_rate_sales", 0.1)
    monkeypatch.setattr(settings, "election_enabled", True)
    monkeypatch.setattr(settings, "election_mayor_wage_bonus", 1.2)
    monkeypatch.setattr(settings, "town_public_works_daily_sc", 0)

    mayor = _npc("mayor", "镇长", {"key": "workshop_fixer", "perks": {"wage_sc": 10}})
    mayor.meta_json = {**mayor.meta_json, "mayor": True}
    clerk = _npc("clerk", "文书", {"key": "chronicle_editor", "perks": {"wage_sc": 5}})
    plain = _npc("plain", "闲人")
    db_session.add_all([mayor, clerk, plain])
    await db_session.commit()

    await treasury_service.tax(db_session, 120, reason="sales_tax:seed")
    await coin_service.treasury_credit(db_session, "mayor", 30, reason="seed")
    await coin_service.treasury_credit(db_session, "clerk", 10, reason="seed")
    await treasury_service.run_public_spending(db_session)

    snap = await fetch_treasury_snapshot(db_session)
    assert snap["available"] is True
    assert snap["town_balance_sc"] == 120
    assert snap["resident_total_sc"] == 40 and snap["resident_accounts"] == 2
    # 12 (mayor 10 × 1.2) + 5 (clerk); the duty-less resident contributes nothing
    assert snap["duty_holders"] == 2 and snap["daily_wage_bill_sc"] == 17
    assert snap["last_spend_at"] is not None

    split = treasury_money_split(snap)
    assert split["npc_money_supply_sc"] == 160 and split["town_share"] == 0.75
    runway = treasury_wage_runway(snap)
    assert runway["runway_days"] == round(120 / 17, 2) and runway["at_risk"] is False

    text = render_probes_s15(snap, gate_on=True)
    assert "镇财政余额 = 120 SC" in text
    assert "镇占比 0.75" in text
    assert "财政续航 7.06 天" in text


@pytest.mark.anyio
async def test_control_group_flat_zero_when_disabled(db_session, monkeypatch):
    """Gate off → the control-group shape: town balance 0, share 0, and every
    coin sits with the residents (minted, never taxed)."""
    monkeypatch.setattr(settings, "town_treasury_enabled", False)
    db_session.add(_npc("clerk", "文书", {"key": "chronicle_editor",
                                          "perks": {"wage_sc": 5}}))
    await db_session.commit()
    await coin_service.treasury_credit(db_session, "clerk", 25, reason="wage")

    snap = await fetch_treasury_snapshot(db_session)
    assert snap["town_balance_sc"] == 0 and snap["last_spend_at"] is None
    split = treasury_money_split(snap)
    assert split["town_share"] == 0.0 and split["npc_money_supply_sc"] == 25
    text = render_probes_s15(snap, gate_on=False)
    assert "对照组" in text and "MINT" in text
