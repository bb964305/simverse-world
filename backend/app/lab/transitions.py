"""Atomic terminal-state transitions for Lab tasks/runs (recovery plan Phase 2).

Every terminal write is a compare-and-set over the ``status`` column:
``UPDATE ... WHERE id=? AND status IN (expected)`` + a rowcount check. Zero
updated rows means a stale owner lost the race — the row was already advanced or
cancelled by someone else — so the caller must NOT proceed as if it won (this is
what stops a completing runner from reviving a cancelled task, gap #2). Mirrors
the lease/broker conditional-UPDATE idiom and bypasses the ORM identity map with
``synchronize_session=False``.

This module owns no session and never commits — the caller owns the transaction
boundary, so a task terminal write and its escrow/fence can be composed into one
transaction. It imports only models, so services that fence a run (which would
be a circular import against ``supervision``) can reach the epoch bump here.
"""
from __future__ import annotations

from datetime import datetime, UTC
from typing import Iterable

from sqlalchemy import update

from app.models.lab_lease import LabRunLease
from app.models.lab_run import LabRun
from app.models.lab_task import LabTask
from app.models.world_change_proposal import WorldChangeProposal


async def cas_task_status(
    db, *, task_id: str, expected: Iterable[str], new: str, **extra
) -> bool:
    """Move a task from any ``expected`` status to ``new`` atomically. Returns
    True iff exactly one row moved (this caller won the transition). ``extra``
    sets additional columns in the same UPDATE. ``updated_at`` is always stamped.
    Does not commit."""
    values = {"status": new, "updated_at": datetime.now(UTC), **extra}
    result = await db.execute(
        update(LabTask)
        .where(LabTask.id == task_id, LabTask.status.in_(tuple(expected)))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return (result.rowcount or 0) == 1


async def cas_run_status(
    db, *, run_id: str, expected: Iterable[str], new: str, **extra
) -> bool:
    """Move a run from any ``expected`` status to ``new`` atomically. Returns True
    iff exactly one row moved. Does not commit."""
    values = {"status": new, **extra}
    result = await db.execute(
        update(LabRun)
        .where(LabRun.id == run_id, LabRun.status.in_(tuple(expected)))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return (result.rowcount or 0) == 1


async def cas_proposal_status(
    db, *, proposal_id: str, expected: Iterable[str], new: str, **extra
) -> bool:
    """Move a WorldChangeProposal from any ``expected`` status to ``new``
    atomically. Returns True iff exactly one row moved — two racing admins
    cannot both apply/revert the same proposal (recovery plan Phase 3). Does not
    commit."""
    result = await db.execute(
        update(WorldChangeProposal)
        .where(WorldChangeProposal.id == proposal_id, WorldChangeProposal.status.in_(tuple(expected)))
        .values(status=new, **extra)
        .execution_options(synchronize_session=False)
    )
    return (result.rowcount or 0) == 1


async def bump_run_epoch(db, run_id: str) -> None:
    """Structurally fence a run's current owner: ``fencing_epoch += 1`` on its
    lease (takeover semantics). The old owner's next heartbeat / event append
    carries the stale epoch and is rejected (``leases.StaleEpoch``). No lease row
    (the run never started) → no-op, epoch stays 0. Does not commit — mirrors
    ``supervision._bump_epoch`` but leaves the commit to the caller so the fence
    composes into the same transaction as the terminal write."""
    await db.execute(
        update(LabRunLease)
        .where(LabRunLease.run_id == run_id)
        .values(fencing_epoch=LabRunLease.fencing_epoch + 1)
        .execution_options(synchronize_session=False)
    )
