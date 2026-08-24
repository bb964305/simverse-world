"""P0/P1 economy rollout guards: audited bootstrap and world-day claim."""

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models.economy_bootstrap import (
    EconomyBootstrapBatch,
    EconomyBootstrapGrant,
)
from app.models.resident import Resident
from app.models.resident_treasury import ResidentTreasury
from app.models.system_config import SystemConfig
from app.models.user import User
from app.services import coin_service, economy_bootstrap_service, treasury_service
from app.tasks.economy_cron import _claim_world_day

pytestmark = pytest.mark.anyio


async def test_bootstrap_is_audited_and_idempotent(db_session, monkeypatch):
    monkeypatch.setattr(settings, "economy_bootstrap_resident_floor_sc", 12)
    monkeypatch.setattr(settings, "economy_bootstrap_payroll_days", 7)
    monkeypatch.setattr(settings, "town_duty_funding_enabled", False)
    monkeypatch.setattr(settings, "npc_default_wage_sc", 5)
    monkeypatch.setattr(settings, "town_ledger_enabled", False)

    admin = User(
        id="bootstrap-admin",
        name="admin",
        email="bootstrap-admin@test.local",
        is_admin=True,
    )
    low = Resident(
        id="bootstrap-low",
        slug="bootstrap-low",
        name="低余额居民",
        resident_type="npc",
        meta_json={},
    )
    funded = Resident(
        id="bootstrap-funded",
        slug="bootstrap-funded",
        name="已有余额居民",
        resident_type="npc",
        meta_json={},
    )
    db_session.add_all(
        [
            admin,
            low,
            funded,
            ResidentTreasury(resident_slug=low.slug, balance_sc=2),
            ResidentTreasury(resident_slug=funded.slug, balance_sc=15),
        ]
    )
    await db_session.commit()

    plan = await economy_bootstrap_service.preview(db_session)
    assert plan["already_applied"] is False
    assert plan["resident_grant_sc"] == 10
    assert plan["town_target_sc"] == 70
    assert plan["town_grant_sc"] == 70

    first = await economy_bootstrap_service.apply(
        db_session, requested_by_user_id=admin.id
    )
    assert first["already_applied"] is True
    assert await coin_service.treasury_balance(db_session, low.slug) == 12
    assert await coin_service.treasury_balance(db_session, funded.slug) == 15
    assert await treasury_service.balance(db_session) == 70
    await db_session.refresh(low)
    assert low.meta_json["wallet"] == 12

    second = await economy_bootstrap_service.apply(
        db_session, requested_by_user_id=admin.id
    )
    assert second["batch_id"] == first["batch_id"]
    assert await coin_service.treasury_balance(db_session, low.slug) == 12
    assert await treasury_service.balance(db_session) == 70
    from app.services.economy_observability_service import snapshot

    operations = await snapshot(db_session)
    assert operations["town"]["daily_payroll_sc"] == 10
    assert operations["town"]["payroll_runway_world_days"] == 7
    assert (
        await db_session.execute(
            select(func.count()).select_from(EconomyBootstrapBatch)
        )
    ).scalar_one() == 1
    grants = (
        await db_session.execute(select(EconomyBootstrapGrant))
    ).scalars().all()
    assert [(row.resident_slug, row.amount_sc) for row in grants] == [
        (low.slug, 10)
    ]


async def test_world_day_claim_is_at_most_once(db_session):
    assert await _claim_world_day(db_session, "2099-01-02") is True
    assert await _claim_world_day(db_session, "2099-01-02") is False
    rows = (
        await db_session.execute(
            select(SystemConfig).where(
                SystemConfig.key == "npc_trade_world_day:2099-01-02"
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].group == "economy"
