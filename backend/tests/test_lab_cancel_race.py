"""Phase 2 (recovery plan) — cancellation can never revive a task or double-refund.

The status report's correctness gap #2: ``cancel_task()`` refunded and marked a
task cancelled WITHOUT fencing its run, so a completing orchestrator could later
call ``mark_review()`` and revive the terminal task. These tests lock the
invariant end to end:

* a cancelled task fences its active run (lease epoch bumped, run -> cancelled);
* ``mark_review`` is a no-op on any already-terminal task (stale runner/orchestrator
  cannot revive it);
* a still-queued run is skipped by ``run_one_v1`` after cancellation;
* duplicate cancellation refunds exactly once.
"""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models.user import User
from app.models.resident import Resident
from app.models.lab_task import LabTask
from app.models.lab_run import LabRun
from app.lab import leases
from app.services import coin_service
from app.services import lab_task_service as svc


@pytest.fixture
def lab_env(db_engine, monkeypatch):
    from app.config import settings
    for k, v in {
        "lab_enabled": True, "lab_adapter": "mock", "lab_creator_share": 0.2,
        "lab_platform_fee_rate": 0.1, "lab_default_budget_usd": 0.5,
        "lab_daily_tasks_per_user": 20, "lab_auto_release_hours": 72,
        "lab_task_deadline_hours": 24,
    }.items():
        monkeypatch.setattr(settings, k, v, raising=False)
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("app.lab.runner.async_session", factory), \
         patch("app.lab.orchestrator.async_session", factory), \
         patch("app.services.lab_task_service.async_session", factory), \
         patch("app.services.lab_task_service.emit", new_callable=AsyncMock):
        yield factory


async def _seed(factory, *, issuer_balance=1000):
    async with factory() as s:
        s.add(User(id="issuer", name="Issuer", email="i@t.com", soul_coin_balance=issuer_balance))
        s.add(User(id="creator_user", name="Creator", email="c@t.com", soul_coin_balance=0))
        s.add(Resident(
            slug="sage", name="Sage", creator_id="creator_user", resident_type="npc",
            meta_json={"lab": {"access": True, "tier": "senior", "skills": ["web_search"]}},
        ))
        await s.commit()


async def _new_task(factory, reward=100):
    async with factory() as s:
        task = await svc.create_task(
            s, issuer_id="issuer", title="取消竞态", brief="...",
            scopes=["web_search"], reward_sc=reward, researcher_slug="sage",
        )
        return task.id, task.accepted_run_id


@pytest.mark.anyio
async def test_mark_review_cannot_revive_cancelled_task(lab_env):
    """A stale runner/orchestrator that finished AFTER the task was cancelled must
    not flip it back to review. This is the headline revival invariant."""
    factory = lab_env
    await _seed(factory)
    tid, rid = await _new_task(factory)

    async with factory() as s:
        task = await svc.cancel_task(s, tid, "issuer")
        assert task.status == "cancelled"

    # A completing run now calls mark_review on the cancelled task.
    async with factory() as s:
        task = await s.get(LabTask, tid)
        run = await s.get(LabRun, rid)
        reviewed = await svc.mark_review(s, task, run, result_summary="done")
        assert reviewed is False  # no-op

    async with factory() as s:
        task = await s.get(LabTask, tid)
        assert task.status == "cancelled"  # never revived
        # Refunded exactly once, not settled.
        assert await coin_service.get_balance(s, "issuer") == 1000


@pytest.mark.anyio
async def test_cancel_running_task_fences_run(lab_env):
    """Cancelling a running task bumps the lease epoch (fencing a live
    orchestrator) and drives the run to cancelled, before the refund is final."""
    factory = lab_env
    await _seed(factory)
    tid, rid = await _new_task(factory)

    # Simulate an in-flight run: task+run running, a live lease at epoch 0.
    async with factory() as s:
        task = await s.get(LabTask, tid)
        run = await s.get(LabRun, rid)
        task.status = "running"
        run.status = "running"
        await s.commit()
        await leases.acquire_lease(s, run_id=rid, owner_id="owner-A")
        assert await leases.current_epoch(s, rid) == 0

    async with factory() as s:
        task = await svc.cancel_task(s, tid, "issuer")
        assert task.status == "cancelled"

    async with factory() as s:
        run = await s.get(LabRun, rid)
        assert run.status == "cancelled"
        assert await leases.current_epoch(s, rid) == 1  # epoch bumped => fenced
        assert await coin_service.get_balance(s, "issuer") == 1000  # refunded once


@pytest.mark.anyio
async def test_duplicate_cancel_refunds_once(lab_env):
    factory = lab_env
    await _seed(factory)
    tid, _ = await _new_task(factory)

    async with factory() as s:
        await svc.cancel_task(s, tid, "issuer")
    async with factory() as s:
        with pytest.raises(svc.LabTaskError):
            await svc.cancel_task(s, tid, "issuer")  # already finalized

    async with factory() as s:
        assert await coin_service.get_balance(s, "issuer") == 1000  # single refund


@pytest.mark.anyio
async def test_cancel_queued_run_is_skipped_by_orchestrator(lab_env):
    """A queued run that is cancelled before pickup must be skipped by the
    orchestrator (its queued-guard sees a non-queued run)."""
    from app.lab import orchestrator
    factory = lab_env
    await _seed(factory)
    tid, rid = await _new_task(factory)

    async with factory() as s:
        await svc.cancel_task(s, tid, "issuer")
    async with factory() as s:
        run = await s.get(LabRun, rid)
        assert run.status == "cancelled"

    # Orchestrator picks the (now cancelled) run up: it must not execute it.
    await orchestrator.run_one_v1(rid)

    async with factory() as s:
        run = await s.get(LabRun, rid)
        assert run.status == "cancelled"  # untouched, never ran
        task = await s.get(LabTask, tid)
        assert task.status == "cancelled"
