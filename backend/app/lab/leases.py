"""Run ownership lease + fencing epochs (PRD §Run Lease and Fencing).

Exactly one live ``owner_id`` may hold a run's lease. Ownership is time-bounded
by a TTL: a holder must ``heartbeat`` before ``expires_at`` or its lease lapses
and another worker may **take over**. Every takeover bumps ``fencing_epoch`` so
a stale holder — one that resumes after being reaped — is fenced: its heartbeat
and (via the Ledger's ``expected_epoch`` gate) its event writes are rejected.

The load-bearing invariant is takeover atomicity. Takeover is a single
conditional ``UPDATE ... WHERE run_id=? AND expires_at<=now`` — the row-count
check is what makes two racing takeovers unable to both flip the same expired
row, so they cannot both land the same epoch (mirrors the Broker's conditional
approval-consume). This module owns lease lifecycle only: it never opens its own
session (the caller owns the transaction boundary) and does no WS/Redis I/O.
"""
from __future__ import annotations

from datetime import datetime, timedelta, UTC

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.models.lab_lease import LabRunLease

LEASE_TTL_S = 30
HEARTBEAT_INTERVAL_S = 10


class LeaseError(Exception):
    """Base for lease-level failures (contention, lost race)."""


class StaleEpoch(LeaseError):
    """The caller's fencing epoch is no longer current — it has been fenced by a
    takeover (or the lease lapsed). Downstream writes must be refused."""


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def _as_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo on load; re-attach UTC so a loaded column can be
    compared against an aware ``now`` (SQL comparisons already agree because the
    dialect binds both as UTC wall-clock strings)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def acquire_lease(db, *, run_id: str, owner_id: str, now: datetime | None = None) -> LabRunLease:
    """Acquire (or renew, or take over) the lease for ``run_id``.

    * no row → create at epoch 0
    * live + same owner → renew, epoch unchanged
    * live + other owner → ``LeaseError("held")``
    * expired → atomic takeover: new owner, ``fencing_epoch += 1``, TTL renewed
    """
    now = _now(now)
    expires = now + timedelta(seconds=LEASE_TTL_S)

    lease = await db.get(LabRunLease, run_id)
    if lease is None:
        lease = LabRunLease(
            run_id=run_id, owner_id=owner_id, fencing_epoch=0,
            heartbeat_at=now, expires_at=expires,
        )
        db.add(lease)
        try:
            await db.commit()
        except IntegrityError:
            # A concurrent creator won the insert race — fall through and treat
            # the now-existing row like any other contended lease.
            await db.rollback()
            lease = await db.get(LabRunLease, run_id)
            if lease is None:  # pragma: no cover - defensive
                raise LeaseError("acquire_conflict")
        else:
            await db.refresh(lease)
            return lease

    # Existing row.
    if _as_utc(lease.expires_at) > now:
        if lease.owner_id == owner_id:
            # Same owner, still live → renew heartbeat/TTL, keep the epoch.
            await db.execute(
                update(LabRunLease)
                .where(LabRunLease.run_id == run_id, LabRunLease.owner_id == owner_id)
                .values(heartbeat_at=now, expires_at=expires)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            await db.refresh(lease)
            return lease
        raise LeaseError("held")

    # Expired → atomic takeover. The expiry predicate + rowcount==1 is the gate:
    # of two racing takeovers only one flips the expired row and bumps the epoch;
    # the loser sees rowcount 0 and re-reads the winner's fresh (live) lease.
    result = await db.execute(
        update(LabRunLease)
        .where(LabRunLease.run_id == run_id, LabRunLease.expires_at <= now)
        .values(
            owner_id=owner_id,
            fencing_epoch=LabRunLease.fencing_epoch + 1,
            heartbeat_at=now,
            expires_at=expires,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        fresh = await db.get(LabRunLease, run_id)
        if fresh is None:  # pragma: no cover - defensive
            raise LeaseError("acquire_conflict")
        if fresh.owner_id == owner_id and _as_utc(fresh.expires_at) > now:
            return fresh
        raise LeaseError("held")
    await db.commit()
    await db.refresh(lease)
    return lease


async def heartbeat(db, *, run_id: str, owner_id: str, epoch: int, now: datetime | None = None) -> LabRunLease:
    """Renew a live lease held by ``owner_id`` at ``epoch``.

    A conditional UPDATE gated on owner + epoch + not-yet-expired; rowcount 0
    means the caller has been fenced (taken over, or its lease lapsed) →
    ``StaleEpoch``.
    """
    now = _now(now)
    expires = now + timedelta(seconds=LEASE_TTL_S)
    result = await db.execute(
        update(LabRunLease)
        .where(
            LabRunLease.run_id == run_id,
            LabRunLease.owner_id == owner_id,
            LabRunLease.fencing_epoch == epoch,
            LabRunLease.expires_at > now,
        )
        .values(heartbeat_at=now, expires_at=expires)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise StaleEpoch(f"heartbeat rejected for run {run_id} at epoch {epoch}")
    await db.commit()
    lease = await db.get(LabRunLease, run_id)
    if lease is not None:
        await db.refresh(lease)  # Core UPDATE bypasses the identity map
    return lease


async def current_epoch(db, run_id: str) -> int:
    """The run's current fencing epoch; a missing lease is epoch 0."""
    epoch = (
        await db.execute(select(LabRunLease.fencing_epoch).where(LabRunLease.run_id == run_id))
    ).scalar_one_or_none()
    return epoch if epoch is not None else 0


async def assert_epoch(db, *, run_id: str, epoch: int) -> None:
    """Raise ``StaleEpoch`` unless ``epoch`` is the run's current epoch (a
    missing lease has epoch 0)."""
    live = await current_epoch(db, run_id)
    if live != epoch:
        raise StaleEpoch(f"stale epoch for run {run_id}: have {epoch}, current {live}")
