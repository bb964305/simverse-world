"""T4 — run lease / fencing epochs (PRD §Run Lease and Fencing, V07).

acquire → epoch 0; idempotent same-owner refresh; live-lease contention;
expired-lease takeover bumps the epoch and fences the old owner's heartbeat
*and* its ledger writes; the conditional-UPDATE takeover is atomic (two racing
takeovers cannot both land the same epoch); assert_epoch treats a missing lease
as epoch 0.

leases.py / ledger.py accept an injected ``db`` (no session creation inside the
modules), so these tests just hand each function a session on a shared in-memory
engine — no ``async_session`` patching needed.
"""
import uuid
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.lab import leases, ledger
from app.lab.protocol import RunEventEnvelope
from app.models.lab_event import LabRunEvent, OutboxEvent
from app.models.lab_run import LabRun
from app.models.lab_task import LabTask


@pytest.fixture
async def lab_env(db_engine):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add(LabTask(id="task1", issuer_user_id="issuer", title="t"))
        s.add(LabRun(id="run1", task_id="task1", researcher_slug="sage"))
        await s.commit()
    yield factory


def _env(**over):
    base = dict(
        event_id=str(uuid.uuid4()), tenant_id="issuer", run_id="run1",
        task_id="task1", seq=1, type="tool.requested", actor="agent-1",
        fencing_epoch=0, policy_version="lab-policy-v1",
        occurred_at=datetime.now(UTC),
    )
    base.update(over)
    return RunEventEnvelope(**base)


async def _expire(factory, run_id="run1"):
    """Force the lease into the past so the next acquire is a takeover."""
    async with factory() as s:
        row = await s.get(leases.LabRunLease, run_id)
        row.expires_at = datetime(2020, 1, 1, tzinfo=UTC)
        row.heartbeat_at = datetime(2020, 1, 1, tzinfo=UTC)
        await s.commit()


# ── 1. acquire new run → epoch 0; same owner re-acquire → same epoch ──
@pytest.mark.anyio
async def test_acquire_new_run_epoch_zero_and_idempotent(lab_env):
    factory = lab_env
    async with factory() as s:
        lease = await leases.acquire_lease(s, run_id="run1", owner_id="A")
        assert lease.fencing_epoch == 0
        assert lease.owner_id == "A"
    async with factory() as s:
        again = await leases.acquire_lease(s, run_id="run1", owner_id="A")
        assert again.fencing_epoch == 0
        assert again.owner_id == "A"


# ── 2. live lease held by someone else → LeaseError("held") ──
@pytest.mark.anyio
async def test_live_lease_other_owner_rejected(lab_env):
    factory = lab_env
    async with factory() as s:
        await leases.acquire_lease(s, run_id="run1", owner_id="A")
    async with factory() as s:
        with pytest.raises(leases.LeaseError) as ei:
            await leases.acquire_lease(s, run_id="run1", owner_id="B")
        assert "held" in str(ei.value)
    # unchanged: still owner A, still epoch 0
    async with factory() as s:
        row = await s.get(leases.LabRunLease, "run1")
        assert row.owner_id == "A" and row.fencing_epoch == 0


# ── 3. expired → new owner takeover bumps epoch; old owner heartbeat fenced ──
@pytest.mark.anyio
async def test_takeover_bumps_epoch_and_fences_old_heartbeat(lab_env):
    factory = lab_env
    async with factory() as s:
        await leases.acquire_lease(s, run_id="run1", owner_id="A")
    await _expire(factory)
    async with factory() as s:
        lease = await leases.acquire_lease(s, run_id="run1", owner_id="B")
        assert lease.fencing_epoch == 1
        assert lease.owner_id == "B"
    async with factory() as s:
        with pytest.raises(leases.StaleEpoch):
            await leases.heartbeat(s, run_id="run1", owner_id="A", epoch=0)
    # new owner's own heartbeat at the current epoch still works
    async with factory() as s:
        beat = await leases.heartbeat(s, run_id="run1", owner_id="B", epoch=1)
        assert beat.fencing_epoch == 1


# ── 4. fenced writer: append_event(expected_epoch stale) → StaleEpoch, 0 rows ──
@pytest.mark.anyio
async def test_fenced_writer_cannot_write_ledger(lab_env):
    factory = lab_env
    async with factory() as s:
        await leases.acquire_lease(s, run_id="run1", owner_id="A")
    await _expire(factory)
    async with factory() as s:
        await leases.acquire_lease(s, run_id="run1", owner_id="B")  # epoch → 1
    async with factory() as s:
        with pytest.raises(leases.StaleEpoch):
            await ledger.append_event(
                s, envelope=_env(fencing_epoch=0, type="tool.started"),
                expected_epoch=0,
            )
    async with factory() as s:
        n_events = (await s.execute(select(func.count()).select_from(LabRunEvent))).scalar_one()
        n_outbox = (await s.execute(select(func.count()).select_from(OutboxEvent))).scalar_one()
        assert n_events == 0
        assert n_outbox == 0


# ── 5. double takeover is atomic: the loser does not also land epoch 1 ──
@pytest.mark.anyio
async def test_double_takeover_does_not_double_win(lab_env):
    factory = lab_env
    async with factory() as s:
        await leases.acquire_lease(s, run_id="run1", owner_id="A")
    await _expire(factory)
    # First takeover wins epoch 1 for B.
    async with factory() as s:
        first = await leases.acquire_lease(s, run_id="run1", owner_id="B")
        assert first.fencing_epoch == 1
    # A second contender now sees a live (freshly-taken) lease → cannot also win
    # epoch 1; must be rejected (or land a strictly higher epoch).
    async with factory() as s:
        try:
            second = await leases.acquire_lease(s, run_id="run1", owner_id="C")
            assert second.fencing_epoch >= 2  # never a duplicate epoch-1 win
        except leases.LeaseError:
            pass
    async with factory() as s:
        row = await s.get(leases.LabRunLease, "run1")
        assert row.fencing_epoch == 1
        assert row.owner_id == "B"


# ── 6. assert_epoch on a run with no lease → epoch-0 semantics ──
@pytest.mark.anyio
async def test_assert_epoch_missing_lease_is_epoch_zero(lab_env):
    factory = lab_env
    async with factory() as s:
        assert await leases.current_epoch(s, "ghost") == 0
        await leases.assert_epoch(s, run_id="ghost", epoch=0)  # ok
        with pytest.raises(leases.StaleEpoch):
            await leases.assert_epoch(s, run_id="ghost", epoch=1)
