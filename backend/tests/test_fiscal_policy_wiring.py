"""S1-5 treasury ↔ S2-5 fiscal-policy wiring regression tests."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.policy import Policy


@pytest.fixture
def fiscal_gates(monkeypatch):
    monkeypatch.setattr(settings, "polis_policy_enabled", True)
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    return settings


async def _seed(db):
    from app.services.policy_service import PolicyService

    svc = PolicyService(db)
    await svc.seed_defaults()
    return svc


def test_all_reserved_fiscal_entries_are_wired_not_pending():
    from app.services.policy_service import (
        CATALOG_BY_KEY,
        FISCAL_PENDING_KEYS,
        FISCAL_POLICY_KEYS,
    )

    assert FISCAL_POLICY_KEYS == {
        "tax_rate",
        "medical_subsidy_sc",
        "npc_default_wage_sc",
        "housing_development_scale",
    }
    assert not FISCAL_PENDING_KEYS
    assert all(CATALOG_BY_KEY[key]["group"] == "fiscal"
               for key in FISCAL_POLICY_KEYS)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("tax_rate", -0.01),
        ("tax_rate", 1.01),
        ("tax_rate", True),
        ("medical_subsidy_sc", -1),
        ("npc_default_wage_sc", 1.5),
        ("housing_development_scale", "3"),
    ],
)
async def test_invalid_fiscal_amend_is_rejected_without_version_drift(
    db_session, fiscal_gates, key, value,
):
    from app.services.policy_service import PolicyValueError

    svc = await _seed(db_session)
    with pytest.raises(PolicyValueError):
        await svc.apply_amend(key, value, expected_version=1, updated_by="test")
    row = (await db_session.execute(
        select(Policy).where(Policy.key == key)
    )).scalar_one()
    assert row.version == 1


@pytest.mark.anyio
async def test_tax_rate_policy_drives_existing_treasury_tax_interface(
    db_session, fiscal_gates,
):
    from app.services import shop_effects, treasury_service

    svc = await _seed(db_session)
    assert await svc.apply_amend(
        "tax_rate", 0.25, expected_version=1, updated_by="poll:1",
    )

    cut = await shop_effects._skim_town_tax(
        db_session, 100, 0.03, "sales_tax:test",
    )
    assert cut == 25
    assert await treasury_service.balance(db_session) == 25


@pytest.mark.anyio
async def test_tax_gate_off_keeps_legacy_channel_rate(
    db_session, monkeypatch,
):
    from app.services import shop_effects, treasury_service

    monkeypatch.setattr(settings, "polis_policy_enabled", False)
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    cut = await shop_effects._skim_town_tax(
        db_session, 100, 0.10, "legacy_tax:test",
    )
    assert cut == 10
    assert await treasury_service.balance(db_session) == 10


@pytest.mark.anyio
async def test_policy_default_wage_flows_into_funded_wage(
    db_session, fiscal_gates, monkeypatch,
):
    from app.services import coin_service, duty_service, fiscal_policy_service
    from app.services import treasury_service

    svc = await _seed(db_session)
    assert await svc.apply_amend(
        "npc_default_wage_sc", 17, expected_version=1, updated_by="poll:2",
    )
    monkeypatch.setattr(settings, "npc_economy_enabled", True)
    monkeypatch.setattr(settings, "election_enabled", False)

    paid = []
    async def fake_town_to_resident(db, slug, amount, *, reason, **kwargs):
        paid.append((slug, amount, reason))
        return True

    async def fake_balance(db, slug):
        return 17

    async def fake_feed(*args, **kwargs):
        return None

    monkeypatch.setattr(
        treasury_service, "town_to_resident", fake_town_to_resident,
    )
    monkeypatch.setattr(coin_service, "treasury_balance", fake_balance)
    monkeypatch.setattr(duty_service, "_feed", fake_feed)

    resident = SimpleNamespace(id="r1", slug="worker", meta_json={})
    assert await fiscal_policy_service.default_wage_sc(
        db_session, fallback=5,
    ) == 17
    await duty_service._pay_wage(db_session, resident)

    assert paid == [("worker", 17, "wage:worker")]
    assert resident.meta_json["wallet"] == 17


@pytest.mark.anyio
async def test_medical_and_housing_disbursements_are_atomic(
    db_session, fiscal_gates,
):
    from app.services import fiscal_policy_service, treasury_service

    svc = await _seed(db_session)
    assert await svc.apply_amend(
        "medical_subsidy_sc", 40, expected_version=1, updated_by="poll:3",
    )
    assert await svc.apply_amend(
        "housing_development_scale", 3,
        expected_version=1, updated_by="poll:4",
    )
    await treasury_service.tax(db_session, 100, reason="fixture")

    paid = await fiscal_policy_service.pay_medical_subsidy(
        db_session, cost_sc=30,
    )
    assert paid == 30
    assert await treasury_service.balance(db_session) == 70

    units = await fiscal_policy_service.fund_housing_development(
        db_session, unit_cost_sc=20,
    )
    assert units == 3
    assert await treasury_service.balance(db_session) == 10

    # A short account rejects the entire next batch; no partial capacity grant.
    assert await fiscal_policy_service.fund_housing_development(
        db_session, unit_cost_sc=20,
    ) == 0
    assert await treasury_service.balance(db_session) == 10


@pytest.mark.anyio
async def test_new_public_disbursements_are_noops_when_policy_gate_is_off(
    db_session, monkeypatch,
):
    from app.services import fiscal_policy_service, treasury_service

    monkeypatch.setattr(settings, "polis_policy_enabled", False)
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    await treasury_service.tax(db_session, 100, reason="fixture")

    assert await fiscal_policy_service.pay_medical_subsidy(
        db_session, cost_sc=30,
    ) == 0
    assert await fiscal_policy_service.fund_housing_development(
        db_session, unit_cost_sc=20,
    ) == 0
    assert await treasury_service.balance(db_session) == 100
