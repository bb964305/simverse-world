"""Phase 2 (recovery plan) — task funding is transactional (gap #9, funding part).

create_task previously committed the task (draft) separately from the escrow
hold, so a crash between the two could leave a task with no hold, or a hold with
no task link. The transactional variant commits the task + hold + debit + ledger
row together via a flush-only hold, so funding is all-or-nothing: on success the
task is always linked to its hold; on insufficient balance nothing persists.
"""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models.user import User
from app.models.resident import Resident
from app.models.lab_task import LabTask
from app.models.coin_hold import CoinHold
from app.services import coin_service
from app.services import lab_task_service as svc


@pytest.fixture
def lab_env(db_engine, monkeypatch):
    from app.config import settings
    for k, v in {
        "lab_enabled": True, "lab_adapter": "mock", "lab_platform_fee_rate": 0.1,
        "lab_default_budget_usd": 0.5, "lab_sc_per_usd": 100, "lab_daily_tasks_per_user": 20,
    }.items():
        monkeypatch.setattr(settings, k, v, raising=False)
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("app.lab.runner.async_session", factory), \
         patch("app.services.lab_task_service.async_session", factory), \
         patch("app.services.lab_task_service.emit", new_callable=AsyncMock):
        yield factory


async def _seed(factory, balance):
    async with factory() as s:
        s.add(User(id="issuer", name="I", email="i@t.com", soul_coin_balance=balance))
        s.add(Resident(slug="sage", name="Sage", creator_id="system", resident_type="npc",
                       meta_json={"lab": {"access": True}}))
        await s.commit()


@pytest.mark.anyio
async def test_funding_links_task_to_hold_atomically(lab_env):
    factory = lab_env
    await _seed(factory, 1000)
    async with factory() as s:
        task = await svc.create_task(
            s, issuer_id="issuer", title="资助原子性", brief="...",
            scopes=["web_search"], reward_sc=100, researcher_slug="sage",
        )
        tid = task.id

    async with factory() as s:
        task = await s.get(LabTask, tid)
        assert task.hold_id is not None                 # never a funded task without a hold
        hold = await s.get(CoinHold, task.hold_id)
        assert hold is not None and hold.status == "held" and hold.amount == 110
        assert await coin_service.get_balance(s, "issuer") == 890  # reward + fee debited


@pytest.mark.anyio
async def test_insufficient_balance_persists_no_task_and_no_hold(lab_env):
    factory = lab_env
    await _seed(factory, 5)
    async with factory() as s:
        with pytest.raises(svc.LabTaskError):
            await svc.create_task(
                s, issuer_id="issuer", title="穷", brief="...",
                scopes=["web_search"], reward_sc=100, researcher_slug="sage",
            )
    async with factory() as s:
        # No task row and no hold row survived the abandoned funding transaction.
        n_tasks = (await s.execute(select(func.count()).select_from(LabTask))).scalar()
        n_holds = (await s.execute(select(func.count()).select_from(CoinHold))).scalar()
        assert n_tasks == 0 and n_holds == 0
        assert await coin_service.get_balance(s, "issuer") == 5  # untouched
