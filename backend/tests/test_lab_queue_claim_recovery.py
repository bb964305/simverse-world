"""Focused protocol-v2 queue ownership and crash-recovery regressions."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.lab import control_plane, queue
from app.models.lab_control import LabQueueClaim
from app.models.lab_run import LabRun


async def _factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _seed_run(factory, run_id: str, *, status: str = "queued") -> None:
    async with factory() as db:
        db.add(
            LabRun(
                id=run_id,
                task_id=f"task-{run_id}",
                researcher_slug="sage",
                adapter="simverse_ref",
                protocol_version=2,
                status=status,
            )
        )
        await db.commit()


async def _move_to_processing(run_id: str) -> None:
    await queue.enqueue_run(run_id, protocol_version=2)
    assert await queue.dequeue_run(protocol_version=2, timeout=1) == run_id


@pytest.mark.anyio
async def test_reconcile_recovers_crash_between_redis_move_and_db_claim(db_engine):
    factory = await _factory(db_engine)
    await _seed_run(factory, "claim-gap")
    await _move_to_processing("claim-gap")

    async with factory() as db:
        stats = await control_plane.reconcile_v2_processing(db)

    assert stats == {"examined": 1, "retained": 0, "requeued": 1, "removed": 0}
    assert await queue.list_processing(protocol_version=2) == []
    assert await queue.dequeue_run(protocol_version=2, timeout=1) == "claim-gap"

    # Repeated recovery cannot manufacture a second pending copy.
    await queue.requeue_run("claim-gap", protocol_version=2)
    async with factory() as db:
        await control_plane.reconcile_v2_processing(db)
    assert await queue.dequeue_run(protocol_version=2, timeout=1) == "claim-gap"
    assert await queue.dequeue_run(protocol_version=2, timeout=0.01) is None


@pytest.mark.anyio
async def test_live_claim_is_retained_then_expired_claim_requeues_once(db_engine):
    factory = await _factory(db_engine)
    await _seed_run(factory, "claim-expiry")
    await _move_to_processing("claim-expiry")
    now = datetime.now(UTC)

    async with factory() as db:
        token = await control_plane.claim_queue_run(
            db,
            run_id="claim-expiry",
            protocol_version=2,
            owner_id="runner-one",
            now=now,
            lease_s=60,
        )
    assert token

    async with factory() as db:
        retained = await control_plane.reconcile_v2_processing(
            db, now=now + timedelta(seconds=59)
        )
    assert retained["retained"] == 1
    assert await queue.list_processing(protocol_version=2) == ["claim-expiry"]

    async with factory() as db:
        recovered = await control_plane.reconcile_v2_processing(
            db, now=now + timedelta(seconds=61)
        )
    assert recovered["requeued"] == 1
    async with factory() as db:
        claim = await db.get(LabQueueClaim, "claim-expiry")
        assert claim.status == "expired"

    assert await queue.dequeue_run(protocol_version=2, timeout=1) == "claim-expiry"
    await queue.requeue_run("claim-expiry", protocol_version=2)
    async with factory() as db:
        await control_plane.reconcile_v2_processing(
            db, now=now + timedelta(seconds=62)
        )
    assert await queue.dequeue_run(protocol_version=2, timeout=1) == "claim-expiry"
    assert await queue.dequeue_run(protocol_version=2, timeout=0.01) is None


@pytest.mark.anyio
async def test_terminal_processing_entry_is_removed_and_claim_completed(db_engine):
    factory = await _factory(db_engine)
    await _seed_run(factory, "claim-terminal", status="succeeded")
    await _move_to_processing("claim-terminal")
    now = datetime.now(UTC)

    async with factory() as db:
        token = await control_plane.claim_queue_run(
            db,
            run_id="claim-terminal",
            protocol_version=2,
            owner_id="runner-terminal",
            now=now,
        )
    assert token

    async with factory() as db:
        stats = await control_plane.reconcile_v2_processing(db, now=now)
    assert stats["removed"] == 1
    assert await queue.list_processing(protocol_version=2) == []

    async with factory() as db:
        claim = await db.get(LabQueueClaim, "claim-terminal")
        assert claim.status == "completed"
        assert claim.completed_at is not None
