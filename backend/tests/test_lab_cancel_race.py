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
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.lab.sandbox.base import StepEvent
from app.models.user import User
from app.models.resident import Resident
from app.models.lab_task import LabTask
from app.models.lab_run import LabRun, LabRunStep
from app.lab import leases, transitions
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
        "lab_agent_v1_enabled": False,
        "lab_approval_timeout_s": 30,
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


async def _new_task(factory, reward=100, scopes=("web_search",)):
    async with factory() as s:
        task = await svc.create_task(
            s, issuer_id="issuer", title="取消竞态", brief="...",
            scopes=list(scopes), reward_sc=reward, researcher_slug="sage",
        )
        return task.id, task.accepted_run_id


class _PausingLegacyAdapter:
    """Yield one step, then expose a deterministic cancellation boundary."""

    def __init__(self):
        self.waiting_for_second = asyncio.Event()
        self.release_second = asyncio.Event()
        self.stop_called = False
        self.collect_called = False
        self.approvals: list[tuple[str, bool]] = []

    async def start(self, _spec):
        return self

    async def submit_goal(self, _handle, _brief, _scopes):
        return None

    async def step_stream(self, _handle):
        yield StepEvent(phase="message", summary="first")
        self.waiting_for_second.set()
        await self.release_second.wait()
        yield StepEvent(phase="message", summary="must-not-land")

    async def approve(self, _handle, approval_id, decision):
        self.approvals.append((approval_id, decision))

    async def collect_artifacts(self, _handle):
        self.collect_called = True
        return []

    async def stop(self, _handle):
        self.stop_called = True


class _ApprovalLegacyAdapter(_PausingLegacyAdapter):
    async def step_stream(self, _handle):
        yield StepEvent(
            phase="tool_call",
            tool="browser.login",
            summary="ask to log in",
            approval={"id": "legacy-approval"},
        )
        yield StepEvent(phase="message", summary="must-not-resume")


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


@pytest.mark.anyio
async def test_legacy_runner_rechecks_cancel_before_every_stream_step(lab_env):
    """A terminal DB write stops the next yielded event and cannot be revived."""
    from app.lab import runner

    factory = lab_env
    await _seed(factory)
    task_id, run_id = await _new_task(factory)
    adapter = _PausingLegacyAdapter()

    with patch("app.lab.runner.get_adapter", return_value=adapter):
        running = asyncio.create_task(runner.run_one(run_id))
        await asyncio.wait_for(adapter.waiting_for_second.wait(), timeout=2)

        async with factory() as db:
            await svc.cancel_task(db, task_id, "issuer")

        adapter.release_second.set()
        await asyncio.wait_for(running, timeout=2)

    async with factory() as db:
        run = await db.get(LabRun, run_id)
        task = await db.get(LabTask, task_id)
        steps = (await db.execute(
            select(LabRunStep.summary).where(LabRunStep.run_id == run_id)
        )).scalars().all()
        assert run.status == "cancelled"
        assert task.status == "cancelled"
        assert steps == ["first"]
        assert await coin_service.get_balance(db, "issuer") == 1000
    assert adapter.stop_called is True
    assert adapter.collect_called is False


@pytest.mark.anyio
async def test_legacy_approval_wait_cannot_restore_cancelled_run(
    lab_env, monkeypatch,
):
    """Cancel during approval wait must never write ``running`` on return."""
    from app.lab import runner

    factory = lab_env
    await _seed(factory)
    task_id, run_id = await _new_task(factory, scopes=("browse",))
    adapter = _ApprovalLegacyAdapter()
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def delayed_decision(
        _db, _task, _run, _approval_id, *, expected_epoch,
    ):
        assert expected_epoch == 0
        waiting.set()
        await release.wait()
        return True

    monkeypatch.setattr(runner, "_await_decision", delayed_decision)
    with patch("app.lab.runner.get_adapter", return_value=adapter):
        running = asyncio.create_task(runner.run_one(run_id))
        await asyncio.wait_for(waiting.wait(), timeout=2)

        async with factory() as db:
            run = await db.get(LabRun, run_id)
            assert run.status == "needs_approval"
            await svc.cancel_task(db, task_id, "issuer")

        release.set()
        await asyncio.wait_for(running, timeout=2)

    async with factory() as db:
        assert (await db.get(LabRun, run_id)).status == "cancelled"
        assert (await db.get(LabTask, task_id)).status == "cancelled"
        assert (await db.scalar(
            select(func.count()).select_from(LabRunStep).where(
                LabRunStep.run_id == run_id,
            )
        )) == 0
    assert adapter.approvals == []
    assert adapter.stop_called is True


@pytest.mark.anyio
async def test_legacy_runner_obeys_runtime_kill_switch_mid_stream(lab_env):
    """Redis kill switch stops consumption before supervision DB cleanup lands."""
    from app.lab import runner, set_lab_runtime_enabled

    factory = lab_env
    await _seed(factory)
    task_id, run_id = await _new_task(factory)
    adapter = _PausingLegacyAdapter()

    with patch("app.lab.runner.get_adapter", return_value=adapter):
        running = asyncio.create_task(runner.run_one(run_id))
        await asyncio.wait_for(adapter.waiting_for_second.wait(), timeout=2)
        await set_lab_runtime_enabled(False)
        adapter.release_second.set()
        await asyncio.wait_for(running, timeout=2)

    async with factory() as db:
        run = await db.get(LabRun, run_id)
        task = await db.get(LabTask, task_id)
        steps = (await db.execute(
            select(LabRunStep.summary).where(LabRunStep.run_id == run_id)
        )).scalars().all()
        # kill_switch_all owns terminalization; the stale worker owns no write.
        assert run.status == "running"
        assert task.status == "running"
        assert steps == ["first"]
    assert adapter.stop_called is True
    assert adapter.collect_called is False


@pytest.mark.anyio
async def test_legacy_runner_obeys_lease_epoch_fence_mid_stream(lab_env):
    """An epoch bump stops a legacy owner even before run status changes."""
    from app.lab import runner

    factory = lab_env
    await _seed(factory)
    task_id, run_id = await _new_task(factory)
    adapter = _PausingLegacyAdapter()

    # A legacy run normally has no lease row. If supervision/control has one,
    # snapshotting its epoch must still make a later fence authoritative.
    async with factory() as db:
        await leases.acquire_lease(db, run_id=run_id, owner_id="legacy-owner")

    with patch("app.lab.runner.get_adapter", return_value=adapter):
        running = asyncio.create_task(runner.run_one(run_id))
        await asyncio.wait_for(adapter.waiting_for_second.wait(), timeout=2)
        async with factory() as db:
            await transitions.bump_run_epoch(db, run_id)
            await db.commit()
        adapter.release_second.set()
        await asyncio.wait_for(running, timeout=2)

    async with factory() as db:
        assert (await db.get(LabRun, run_id)).status == "running"
        assert (await db.get(LabTask, task_id)).status == "running"
        assert (await leases.current_epoch(db, run_id)) == 1
        assert (await db.scalar(
            select(func.count()).select_from(LabRunStep).where(
                LabRunStep.run_id == run_id,
            )
        )) == 1
    assert adapter.stop_called is True
    assert adapter.collect_called is False
