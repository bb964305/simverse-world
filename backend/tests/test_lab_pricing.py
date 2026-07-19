"""Phase 4 (recovery plan) — minimum SC price derived from the effective budget.

Status report gap #6: lab_sc_per_usd was never used to establish a minimum SC
price, so a task could be funded below the compute it authorizes. The minimum is
ceil(effective_budget_usd(scopes) * lab_sc_per_usd); an underpriced task is
rejected BEFORE any hold (no charge on rejection).
"""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models.user import User
from app.models.resident import Resident
from app.lab import pricing
from app.services import coin_service
from app.services import lab_task_service as svc


@pytest.fixture
def lab_env(db_engine, monkeypatch):
    from app.config import settings
    for k, v in {
        "lab_enabled": True, "lab_adapter": "mock", "lab_platform_fee_rate": 0.1,
        "lab_default_budget_usd": 0.5, "lab_sc_per_usd": 100,
        "lab_daily_tasks_per_user": 20,
    }.items():
        monkeypatch.setattr(settings, k, v, raising=False)
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("app.lab.runner.async_session", factory), \
         patch("app.services.lab_task_service.async_session", factory), \
         patch("app.services.lab_task_service.emit", new_callable=AsyncMock):
        yield factory


async def _seed(factory, balance=1000):
    async with factory() as s:
        s.add(User(id="issuer", name="I", email="i@t.com", soul_coin_balance=balance))
        s.add(Resident(slug="sage", name="Sage", creator_id="system", resident_type="npc",
                       meta_json={"lab": {"access": True, "skills": ["web_search"]}}))
        await s.commit()


def test_minimum_reward_scales_with_scopes():
    single = pricing.minimum_reward_sc(["web_search"])
    multi = pricing.minimum_reward_sc(["web_search", "browse", "code", "http"])
    assert single >= 1
    assert multi >= single  # more scopes authorize more compute => higher floor
    # Never exceeds the flat run-budget ceiling converted to SC.
    from app.config import settings
    assert multi <= (settings.lab_default_budget_usd * settings.lab_sc_per_usd)


@pytest.mark.anyio
async def test_underpriced_task_rejected_before_hold(lab_env):
    factory = lab_env
    await _seed(factory)
    floor = pricing.minimum_reward_sc(["web_search", "browse", "code", "http"])
    async with factory() as s:
        with pytest.raises(svc.LabTaskError, match="minimum|reward"):
            await svc.create_task(
                s, issuer_id="issuer", title="太便宜", brief="...",
                scopes=["web_search", "browse", "code", "http"], reward_sc=floor - 1,
                researcher_slug="sage",
            )
    # No hold taken — balance untouched.
    async with factory() as s:
        assert await coin_service.get_balance(s, "issuer") == 1000


@pytest.mark.anyio
async def test_minimum_price_boundary_accepted(lab_env):
    factory = lab_env
    await _seed(factory)
    floor = pricing.minimum_reward_sc(["web_search", "browse", "code", "http"])
    async with factory() as s:
        task = await svc.create_task(
            s, issuer_id="issuer", title="刚好够", brief="...",
            scopes=["web_search", "browse", "code", "http"], reward_sc=floor,
            researcher_slug="sage",
        )
        assert task.status in ("assigned", "funded")
