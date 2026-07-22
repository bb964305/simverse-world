"""Durable protocol-v2 control plane owned by the Lab Runner.

API processes append control intent.  Only a polling Runner claims that intent,
fences effects, reconstructs Runtime/Executor targets from durable locators, and
records provider receipts.  Redis/outbox delivery is therefore a wakeup, never
the source of control truth.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models.lab_control import (
    LabControlTarget,
    LabGlobalControl,
    LabGlobalKill,
    LabQueueClaim,
    LabRunControlRequest,
    LabToolExecution,
)
from app.models.lab_event import OutboxEvent
from app.models.lab_grant import LabCapabilityGrant
from app.models.lab_lease import LabRunLease
from app.models.lab_run import LabRun
from app.models.lab_runtime import LabRuntimeSession

logger = logging.getLogger(__name__)

Controller = Callable[[dict], Awaitable[dict]]

CONTROL_CLAIM_S = 30
CONTROL_DEADLINE_S = 30
QUEUE_CLAIM_S = 60
_ACTIVE_RUN_STATES = ("queued", "running", "needs_approval")
_ACTIVE_RUNTIME_STATES = ("creating", "ready", "fenced")
_ACTIVE_EXECUTOR_STATES = ("active", "fenced")


class ControlPlaneError(RuntimeError):
    """Base for fail-closed durable-control errors."""


class GlobalEffectFenced(ControlPlaneError):
    """A global epoch or admission fence rejected an effect."""


class AdmissionClosed(GlobalEffectFenced):
    """Global kill has closed Lab admission/effect execution."""


class StaleEffect(GlobalEffectFenced):
    """An effect carries an older or otherwise mismatched global epoch."""

    def __init__(self, *, effect: str, expected: int, actual: int):
        self.effect = effect
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"{effect} effect carries global epoch {expected}; current epoch is {actual}"
        )


async def claim_queue_run(
    db,
    *,
    run_id: str,
    protocol_version: int,
    owner_id: str,
    now: datetime | None = None,
    lease_s: int = QUEUE_CLAIM_S,
) -> str | None:
    """Bind a Redis processing item to one durable protocol-v2 owner."""
    if protocol_version != 2:
        raise ValueError("durable queue claims are protocol-v2 only")
    if not owner_id or type(lease_s) is not int or lease_s <= 0:
        raise ValueError("queue claim owner and positive lease are required")
    current_time = _now(now)
    run = await db.get(LabRun, run_id)
    if run is None or run.protocol_version != protocol_version:
        raise ControlPlaneError("queue claim run binding is invalid")

    row = await db.scalar(
        select(LabQueueClaim)
        .where(LabQueueClaim.run_id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        row is not None
        and row.status == "processing"
        and row.owner_id != owner_id
        and _aware(row.claim_expires_at) > current_time
    ):
        await db.rollback()
        return None

    token = str(uuid.uuid4())
    if row is None:
        row = LabQueueClaim(
            run_id=run_id,
            protocol_version=protocol_version,
            claim_token=token,
            owner_id=owner_id,
            status="processing",
            attempts=1,
            claimed_at=current_time,
            heartbeat_at=current_time,
            claim_expires_at=current_time + timedelta(seconds=lease_s),
        )
        db.add(row)
    else:
        row.protocol_version = protocol_version
        row.claim_token = token
        row.owner_id = owner_id
        row.status = "processing"
        row.attempts += 1
        row.claimed_at = current_time
        row.heartbeat_at = current_time
        row.claim_expires_at = current_time + timedelta(seconds=lease_s)
        row.completed_at = None
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return None
    return token


async def heartbeat_queue_claim(
    db,
    *,
    run_id: str,
    claim_token: str,
    owner_id: str,
    now: datetime | None = None,
    lease_s: int = QUEUE_CLAIM_S,
) -> bool:
    current_time = _now(now)
    result = await db.execute(
        update(LabQueueClaim)
        .where(
            LabQueueClaim.run_id == run_id,
            LabQueueClaim.claim_token == claim_token,
            LabQueueClaim.owner_id == owner_id,
            LabQueueClaim.status == "processing",
        )
        .values(
            heartbeat_at=current_time,
            claim_expires_at=current_time + timedelta(seconds=lease_s),
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return (result.rowcount or 0) == 1


async def settle_queue_claim(
    db,
    *,
    run_id: str,
    claim_token: str,
    owner_id: str,
    disposition: str,
    now: datetime | None = None,
) -> bool:
    if disposition not in {"completed", "released"}:
        raise ValueError("invalid queue claim disposition")
    current_time = _now(now)
    result = await db.execute(
        update(LabQueueClaim)
        .where(
            LabQueueClaim.run_id == run_id,
            LabQueueClaim.claim_token == claim_token,
            LabQueueClaim.owner_id == owner_id,
            LabQueueClaim.status == "processing",
        )
        .values(
            status=disposition,
            heartbeat_at=current_time,
            claim_expires_at=current_time,
            completed_at=current_time if disposition == "completed" else None,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return (result.rowcount or 0) == 1


async def reconcile_v2_processing(
    db, *, now: datetime | None = None
) -> dict[str, int]:
    """Converge Redis processing state from durable claim and lease truth."""
    from app.lab import queue as lab_queue

    current_time = _now(now)
    run_ids = list(dict.fromkeys(await lab_queue.list_processing(protocol_version=2)))
    stats = {"examined": len(run_ids), "retained": 0, "requeued": 0, "removed": 0}
    for run_id in run_ids:
        run = await db.get(LabRun, run_id)
        claim = await db.get(LabQueueClaim, run_id, populate_existing=True)
        lease = await db.get(LabRunLease, run_id, populate_existing=True)
        if run is None or run.status in {"succeeded", "failed", "cancelled"}:
            await lab_queue.ack_run(run_id, protocol_version=2)
            if claim is not None and claim.status == "processing":
                claim.status = "completed"
                claim.completed_at = current_time
                claim.claim_expires_at = current_time
                await db.commit()
            stats["removed"] += 1
            continue

        live_claim = (
            claim is not None
            and claim.status == "processing"
            and _aware(claim.claim_expires_at) > current_time
        )
        live_execution_lease = (
            lease is not None and _aware(lease.expires_at) > current_time
        )
        if live_claim or live_execution_lease:
            stats["retained"] += 1
            continue

        if claim is not None and claim.status == "processing":
            claim.status = "expired"
            claim.claim_expires_at = current_time
            await db.commit()
        await lab_queue.requeue_run(run_id, protocol_version=2)
        stats["requeued"] += 1
    return stats


def _now(value: datetime | None = None) -> datetime:
    return value if value is not None else datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _stable_client_run_id(run_id: str, epoch: int) -> str:
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:40]
    return f"control-{digest}-{epoch}"


def _require_locator(value: Mapping, *, name: str) -> dict:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{name} must be a non-empty object")
    return dict(value)


async def register_runtime_target(
    db,
    *,
    run_id: str,
    session_id: str,
    locator: Mapping,
    epoch: int,
) -> LabRuntimeSession:
    """Register or verify the one durable Runtime locator for a v2 run."""
    if not session_id or len(session_id) > 200:
        raise ValueError("runtime session_id must be 1..200 characters")
    if type(epoch) is not int or epoch < 0:
        raise ValueError("runtime epoch must be a non-negative integer")
    locator_json = _require_locator(locator, name="runtime locator")
    existing = await db.scalar(
        select(LabRuntimeSession)
        .where(LabRuntimeSession.run_id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if existing is not None:
        target_id = existing.provider_session_id or existing.id
        if (
            target_id != session_id
            or existing.locator_json != locator_json
            or existing.fencing_epoch != epoch
        ):
            raise ControlPlaneError("runtime target binding already exists and differs")
        return existing

    row = LabRuntimeSession(
        id=session_id if len(session_id) <= 36 else str(uuid.uuid4()),
        run_id=run_id,
        client_run_id=_stable_client_run_id(run_id, epoch),
        fencing_epoch=epoch,
        authority_epoch=epoch,
        protocol_version=2,
        provider_name=str(locator_json.get("provider") or "runtime")[:80],
        provider_session_id=session_id,
        locator_json=locator_json,
        durability_class="session_affine",
        status="ready",
    )
    db.add(row)
    await db.flush()
    return row


async def register_executor_target(
    db,
    *,
    run_id: str,
    action_id: str,
    job_locator: Mapping,
    epoch: int,
    session_id: str | None = None,
    submit_receipt: Mapping | None = None,
) -> LabToolExecution:
    """Persist an Executor job before the Runner can control or reap it."""
    if not action_id or len(action_id) > 100:
        raise ValueError("executor action_id must be 1..100 characters")
    if type(epoch) is not int or epoch < 0:
        raise ValueError("executor epoch must be a non-negative integer")
    locator_json = _require_locator(job_locator, name="executor job locator")
    existing = await db.scalar(
        select(LabToolExecution)
        .where(
            LabToolExecution.action_id == action_id,
            LabToolExecution.executor_epoch == epoch,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if existing is not None:
        if existing.run_id != run_id or existing.job_locator_json != locator_json:
            raise ControlPlaneError("executor target binding already exists and differs")
        return existing
    row = LabToolExecution(
        run_id=run_id,
        session_id=session_id,
        action_id=action_id,
        job_locator_json=locator_json,
        executor_epoch=epoch,
        submit_receipt_json=(dict(submit_receipt) if submit_receipt else None),
        status="active",
    )
    db.add(row)
    await db.flush()
    return row


async def record_executor_submit_receipt(
    db,
    *,
    action_id: str,
    epoch: int,
    submit_receipt: Mapping,
) -> LabToolExecution:
    """CAS the verified submit receipt without changing the persisted locator."""
    receipt_json = dict(submit_receipt)
    if not receipt_json:
        raise ValueError("executor submit receipt is required")
    row = await db.scalar(
        select(LabToolExecution)
        .where(
            LabToolExecution.action_id == action_id,
            LabToolExecution.executor_epoch == epoch,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise ControlPlaneError("executor target is missing before submit receipt")
    if row.submit_receipt_json is not None:
        if row.submit_receipt_json != receipt_json:
            raise ControlPlaneError("executor submit receipt changed across replay")
        return row
    row.submit_receipt_json = receipt_json
    await db.flush()
    return row


async def settle_executor_target(
    db,
    *,
    action_id: str,
    epoch: int,
    teardown_proof: Mapping,
    stopped_at: datetime | None = None,
) -> LabToolExecution:
    """Mark a terminal Executor job as no longer requiring control fanout."""
    if (
        not isinstance(teardown_proof, Mapping)
        or teardown_proof.get("removed") is not True
    ):
        raise ControlPlaneError("executor target teardown is not verified")
    row = await db.scalar(
        select(LabToolExecution)
        .where(
            LabToolExecution.action_id == action_id,
            LabToolExecution.executor_epoch == epoch,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise ControlPlaneError("executor target is missing at settlement")
    if row.status == "quarantined":
        raise ControlPlaneError("quarantined executor target cannot be settled")
    row.status = "confirmed_stopped"
    row.stopped_at = row.stopped_at or _now(stopped_at)
    await db.flush()
    return row


async def record_executor_result_receipt(
    db,
    *,
    action_id: str,
    epoch: int,
    result_receipt: Mapping,
) -> LabToolExecution:
    """CAS the verified terminal/reconciliation receipt for crash recovery."""
    receipt_json = dict(result_receipt)
    if not receipt_json:
        raise ValueError("executor result receipt is required")
    row = await db.scalar(
        select(LabToolExecution)
        .where(
            LabToolExecution.action_id == action_id,
            LabToolExecution.executor_epoch == epoch,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise ControlPlaneError("executor target is missing before result receipt")
    if row.result_receipt_json is not None:
        if row.result_receipt_json != receipt_json:
            raise ControlPlaneError("executor result receipt changed across replay")
        return row
    row.result_receipt_json = receipt_json
    await db.flush()
    return row


async def ensure_global_control(db) -> LabGlobalControl:
    """Return the locked singleton, creating its default-open row if absent."""
    state = await db.scalar(
        select(LabGlobalControl)
        .where(LabGlobalControl.id == "global")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if state is not None:
        return state

    try:
        async with db.begin_nested():
            db.add(
                LabGlobalControl(
                    id="global", admission_open=True, fencing_epoch=0
                )
            )
            await db.flush()
    except IntegrityError:
        pass
    state = await db.scalar(
        select(LabGlobalControl)
        .where(LabGlobalControl.id == "global")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if state is None:  # pragma: no cover - only possible after external DB damage
        raise ControlPlaneError("global control singleton could not be created")
    return state


async def assert_admission_allowed(
    db, *, expected_global_epoch: int | None = None
) -> int:
    """Lock and validate the global admission state for the caller's transaction."""
    state = await ensure_global_control(db)
    if settings.lab_global_admission_enabled and not state.admission_open:
        raise AdmissionClosed("Lab admission is closed by global control")
    if expected_global_epoch is not None and expected_global_epoch != state.fencing_epoch:
        raise StaleEffect(
            effect="admission",
            expected=expected_global_epoch,
            actual=state.fencing_epoch,
        )
    return state.fencing_epoch


async def assert_effect_epoch(
    db,
    *,
    run_id: str,
    expected_global_epoch: int,
    effect: str,
) -> int:
    """Serialize an effect against global kill and reject stale capability."""
    del run_id  # reserved for per-run audit/fence expansion; global row is authority
    state = await ensure_global_control(db)
    if expected_global_epoch != state.fencing_epoch:
        raise StaleEffect(
            effect=effect,
            expected=expected_global_epoch,
            actual=state.fencing_epoch,
        )
    if (
        settings.lab_global_admission_enabled
        and not state.admission_open
        and effect != "terminalization"
    ):
        raise AdmissionClosed(f"{effect} effect is disabled by global kill")
    return state.fencing_epoch


async def submit_run_control(
    db,
    *,
    run_id: str,
    requested_by: str,
    action: str = "cancel",
    idempotency_key: str | None = None,
    deadline_at: datetime | None = None,
    now: datetime | None = None,
) -> LabRunControlRequest:
    """Append one idempotent run-control request and wakeup outbox atomically."""
    if action not in {"cancel", "terminate", "kill"}:
        raise ValueError("unsupported control action")
    current_time = _now(now)
    active_key = f"run:{run_id}:control"
    stable_key = idempotency_key or f"{active_key}:{action}"
    existing = await db.scalar(
        select(LabRunControlRequest).where(
            or_(
                LabRunControlRequest.idempotency_key == stable_key,
                LabRunControlRequest.active_key == active_key,
            )
        )
    )
    if existing is not None:
        return existing

    run = await db.scalar(
        select(LabRun)
        .where(LabRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None:
        raise ControlPlaneError("run not found")
    if run.protocol_version != 2:
        raise ControlPlaneError("durable control requires protocol_version 2")

    epochs = [0]
    session_epoch = await db.scalar(
        select(func.max(LabRuntimeSession.authority_epoch)).where(
            LabRuntimeSession.run_id == run_id
        )
    )
    executor_epoch = await db.scalar(
        select(func.max(LabToolExecution.executor_epoch)).where(
            LabToolExecution.run_id == run_id
        )
    )
    lease_epoch = await db.scalar(
        select(LabRunLease.fencing_epoch).where(LabRunLease.run_id == run_id)
    )
    epochs.extend(value for value in (session_epoch, executor_epoch, lease_epoch) if value is not None)
    request = LabRunControlRequest(
        id=str(uuid.uuid4()),
        run_id=run_id,
        action=action,
        idempotency_key=stable_key,
        active_key=active_key,
        requested_by=requested_by,
        status="pending",
        fencing_epoch=max(epochs) + 1,
        deadline_at=deadline_at or current_time + timedelta(seconds=CONTROL_DEADLINE_S),
        created_at=current_time,
        updated_at=current_time,
    )
    wakeup = OutboxEvent(
        event_id=str(uuid.uuid4()),
        tenant_id=requested_by,
        run_id=run_id,
        topic="lab_control",
        payload_json={
            "request_id": request.id,
            "run_id": run_id,
            "action": action,
            "epoch": request.fencing_epoch,
        },
    )
    try:
        async with db.begin_nested():
            db.add_all([request, wakeup])
            await db.flush()
    except IntegrityError:
        existing = await db.scalar(
            select(LabRunControlRequest).where(
                or_(
                    LabRunControlRequest.idempotency_key == stable_key,
                    LabRunControlRequest.active_key == active_key,
                )
            )
        )
        if existing is None:
            raise
        request = existing
    await db.commit()
    return request


async def _runtime_inventory(db, *, run_ids: list[str]) -> list[tuple[str, str, dict]]:
    if not run_ids:
        return []
    sessions = (
        await db.execute(
            select(LabRuntimeSession).where(
                LabRuntimeSession.run_id.in_(run_ids),
                LabRuntimeSession.status.in_(_ACTIVE_RUNTIME_STATES),
            )
        )
    ).scalars().all()
    inventory = []
    for session in sessions:
        target_id = session.provider_session_id or session.id
        if session.locator_json and target_id:
            inventory.append((session.run_id, target_id, dict(session.locator_json)))
    return inventory


async def _executor_inventory(db, *, run_ids: list[str]) -> list[tuple[str, str, dict]]:
    if not run_ids:
        return []
    executions = (
        await db.execute(
            select(LabToolExecution).where(
                LabToolExecution.run_id.in_(run_ids),
                LabToolExecution.status.in_(_ACTIVE_EXECUTOR_STATES),
            )
        )
    ).scalars().all()
    return [
        (row.run_id, row.action_id, dict(row.job_locator_json))
        for row in executions
    ]


def _executor_job_epoch(locator: Mapping) -> int:
    epoch = locator.get("epoch")
    if type(epoch) is not int or epoch < 0:
        raise ControlPlaneError("executor locator has no canonical job epoch")
    return epoch


async def _runs_requiring_runtime_target(db, *, run_ids: list[str]) -> set[str]:
    if not run_ids:
        return set()
    return set(
        (
            await db.execute(
                select(LabRun.id).where(
                    LabRun.id.in_(run_ids),
                    LabRun.status.in_(("running", "needs_approval")),
                )
            )
        ).scalars().all()
    )


def _missing_runtime_target(
    *,
    request_id: str | None,
    kill_id: str | None,
    run_id: str,
    action: str,
    epoch: int,
    deadline_at: datetime,
) -> LabControlTarget:
    now = _now()
    return LabControlTarget(
        request_id=request_id,
        kill_id=kill_id,
        run_id=run_id,
        target_kind="runtime",
        target_id=str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"simverse:missing-runtime:{run_id}")
        ),
        locator_json={"missing": "runtime"},
        action=action,
        epoch=epoch,
        status="quarantined",
        deadline_at=deadline_at,
        last_error="runtime target inventory missing",
        quarantined_at=now,
    )


async def _materialize_request_targets(
    db, *, request: LabRunControlRequest
) -> list[LabControlTarget]:
    existing = (
        await db.execute(
            select(LabControlTarget).where(
                LabControlTarget.request_id == request.id
            )
        )
    ).scalars().all()
    if existing:
        return list(existing)
    rows: list[LabControlTarget] = []
    runtime_inventory = await _runtime_inventory(db, run_ids=[request.run_id])
    for run_id, target_id, locator in runtime_inventory:
        rows.append(
            LabControlTarget(
                request_id=request.id,
                run_id=run_id,
                target_kind="runtime",
                target_id=target_id,
                locator_json=locator,
                action=request.action,
                epoch=request.fencing_epoch,
                deadline_at=request.deadline_at,
            )
        )
    required_runtime = await _runs_requiring_runtime_target(
        db, run_ids=[request.run_id]
    )
    if request.run_id in required_runtime and not runtime_inventory:
        rows.append(
            _missing_runtime_target(
                request_id=request.id,
                kill_id=None,
                run_id=request.run_id,
                action=request.action,
                epoch=request.fencing_epoch,
                deadline_at=request.deadline_at,
            )
        )
    for run_id, target_id, locator in await _executor_inventory(
        db, run_ids=[request.run_id]
    ):
        rows.append(
            LabControlTarget(
                request_id=request.id,
                run_id=run_id,
                target_kind="executor",
                target_id=target_id,
                locator_json=locator,
                action=request.action,
                # The parent request carries the new authority fence. Executor
                # control remains bound to the immutable epoch of the exact job.
                epoch=_executor_job_epoch(locator),
                deadline_at=request.deadline_at,
            )
        )
    db.add_all(rows)
    await db.flush()
    return rows


async def _fence_run(db, *, request: LabRunControlRequest, now: datetime) -> None:
    await db.execute(
        update(LabCapabilityGrant)
        .where(
            LabCapabilityGrant.run_id == request.run_id,
            LabCapabilityGrant.revoked_at.is_(None),
        )
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    lease = await db.scalar(
        select(LabRunLease)
        .where(LabRunLease.run_id == request.run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if lease is not None:
        lease.fencing_epoch = max(lease.fencing_epoch + 1, request.fencing_epoch)
        lease.expires_at = now
        lease.heartbeat_at = now
    sessions = (
        await db.execute(
            select(LabRuntimeSession).where(
                LabRuntimeSession.run_id == request.run_id,
                LabRuntimeSession.status.in_(("creating", "ready")),
            )
        )
    ).scalars().all()
    for session in sessions:
        session.authority_epoch = max(session.authority_epoch, request.fencing_epoch)
        session.status = "fenced"
    await db.execute(
        update(LabToolExecution)
        .where(
            LabToolExecution.run_id == request.run_id,
            LabToolExecution.status == "active",
        )
        .values(status="fenced", updated_at=now)
        .execution_options(synchronize_session=False)
    )
    request.fenced_at = request.fenced_at or now


async def _claim_request(
    db, *, request_id: str, owner_id: str, now: datetime
) -> LabRunControlRequest | None:
    result = await db.execute(
        update(LabRunControlRequest)
        .where(
            LabRunControlRequest.id == request_id,
            LabRunControlRequest.status.in_(("pending", "processing")),
            or_(
                LabRunControlRequest.status == "pending",
                LabRunControlRequest.claim_expires_at.is_(None),
                LabRunControlRequest.claim_expires_at <= now,
                LabRunControlRequest.claim_owner == owner_id,
            ),
        )
        .values(
            status="processing",
            claim_owner=owner_id,
            claim_expires_at=now + timedelta(seconds=CONTROL_CLAIM_S),
            heartbeat_at=now,
            attempts=LabRunControlRequest.attempts + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if (result.rowcount or 0) != 1:
        return None
    return await db.get(LabRunControlRequest, request_id, populate_existing=True)


async def _claim_target(
    db, *, target_id: str, owner_id: str, now: datetime
) -> LabControlTarget | None:
    result = await db.execute(
        update(LabControlTarget)
        .where(
            LabControlTarget.id == target_id,
            LabControlTarget.status.in_(("pending", "processing")),
            or_(
                LabControlTarget.status == "pending",
                LabControlTarget.claim_expires_at.is_(None),
                LabControlTarget.claim_expires_at <= now,
                LabControlTarget.claim_owner == owner_id,
            ),
        )
        .values(
            status="processing",
            claim_owner=owner_id,
            claim_expires_at=now + timedelta(seconds=CONTROL_CLAIM_S),
            attempts=LabControlTarget.attempts + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if (result.rowcount or 0) != 1:
        return None
    return await db.get(LabControlTarget, target_id, populate_existing=True)


def _command(*, parent_id: str, target: LabControlTarget) -> dict:
    return {
        "request_id": parent_id,
        "run_id": target.run_id,
        "target_kind": target.target_kind,
        "target_id": target.target_id,
        "locator": dict(target.locator_json),
        "action": target.action,
        "epoch": target.epoch,
        "deadline_at": _aware(target.deadline_at).isoformat(),
    }


async def runtime_http_controller(command: Mapping) -> dict:
    """Rebuild one Runtime control client exclusively from durable target state."""
    from app.lab.protocol import ControlCommand
    from app.lab.sandbox.base import HttpAgentAdapter

    if command.get("target_kind") != "runtime":
        raise ControlPlaneError("runtime controller received a non-runtime target")
    locator = _require_locator(command.get("locator"), name="runtime locator")
    base_url = locator.get("base_url")
    session_id = locator.get("session_id")
    if (
        not isinstance(base_url, str)
        or not base_url
        or not isinstance(session_id, str)
        or not session_id
        or session_id != command.get("target_id")
    ):
        raise ControlPlaneError("runtime locator is incomplete or cross-bound")
    command_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            ":".join(
                str(command[name])
                for name in (
                    "request_id",
                    "run_id",
                    "target_kind",
                    "target_id",
                    "action",
                    "epoch",
                )
            ),
        )
    )
    body = ControlCommand.model_validate(
        {
            "schema_version": 2,
            "command_id": command_id,
            "request_id": command["request_id"],
            "run_id": command["run_id"],
            "session_id": session_id,
            "target_kind": "runtime",
            "target_id": command["target_id"],
            "action": command["action"],
            "epoch": command["epoch"],
            "deadline_at": command["deadline_at"],
        }
    )
    adapter = HttpAgentAdapter(base_url=base_url)
    return await adapter.control_runtime_v2(body)


async def _settle_target(
    db,
    *,
    target_id: str,
    owner_id: str,
    response: Mapping | None,
    error: Exception | None,
    now: datetime,
) -> str:
    target = await db.scalar(
        select(LabControlTarget)
        .where(
            LabControlTarget.id == target_id,
            LabControlTarget.status == "processing",
            LabControlTarget.claim_owner == owner_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if target is None:
        await db.rollback()
        return "pending"

    payload = dict(response or {})
    confirmed = payload.get("status") == "confirmed_stopped" and bool(
        payload.get("receipt_id")
    )
    if confirmed:
        target.status = "confirmed_stopped"
        target.receipt_json = payload
        target.stopped_at = now
        target.last_error = None
        target.claim_expires_at = None
        if target.target_kind == "runtime":
            session = await db.scalar(
                select(LabRuntimeSession).where(
                    LabRuntimeSession.run_id == target.run_id,
                    or_(
                        LabRuntimeSession.provider_session_id == target.target_id,
                        LabRuntimeSession.id == target.target_id,
                    ),
                )
            )
            if session is not None:
                session.status = "cancelled"
                session.ended_at = now
        else:
            execution = await db.scalar(
                select(LabToolExecution).where(
                    LabToolExecution.run_id == target.run_id,
                    LabToolExecution.action_id == target.target_id,
                    LabToolExecution.executor_epoch == target.epoch,
                )
            )
            if execution is not None:
                execution.status = "confirmed_stopped"
                execution.control_receipt_json = payload
                execution.stopped_at = now
        await db.commit()
        return "confirmed_stopped"

    reason = str(error) if error is not None else str(payload.get("error") or "unconfirmed stop")
    target.last_error = reason[:500]
    target.claim_owner = None
    target.claim_expires_at = None
    if now >= _aware(target.deadline_at):
        target.status = "quarantined"
        target.quarantined_at = now
        if target.target_kind == "runtime":
            session = await db.scalar(
                select(LabRuntimeSession).where(
                    LabRuntimeSession.run_id == target.run_id,
                    or_(
                        LabRuntimeSession.provider_session_id == target.target_id,
                        LabRuntimeSession.id == target.target_id,
                    ),
                )
            )
            if session is not None:
                session.status = "quarantined"
                session.last_error = target.last_error
        else:
            execution = await db.scalar(
                select(LabToolExecution).where(
                    LabToolExecution.run_id == target.run_id,
                    LabToolExecution.action_id == target.target_id,
                    LabToolExecution.executor_epoch == target.epoch,
                )
            )
            if execution is not None:
                execution.status = "quarantined"
                execution.quarantined_at = now
        await db.commit()
        return "quarantined"
    target.status = "pending"
    await db.commit()
    return "pending"


async def _process_targets(
    db,
    *,
    targets: list[LabControlTarget],
    parent_id: str,
    owner_id: str,
    controllers: Mapping[str, Controller],
    now: datetime,
) -> dict[str, int]:
    counts = {"confirmed_stopped": 0, "quarantined": 0, "pending": 0}
    for known in targets:
        if known.status in ("confirmed_stopped", "quarantined"):
            counts[known.status] += 1
            continue
        target = await _claim_target(
            db, target_id=known.id, owner_id=owner_id, now=now
        )
        if target is None:
            counts["pending"] += 1
            continue
        controller = controllers.get(target.target_kind)
        response = None
        error = None
        if controller is None:
            error = ControlPlaneError(
                f"no {target.target_kind} controller is registered"
            )
        else:
            try:
                response = await controller(_command(parent_id=parent_id, target=target))
            except Exception as exc:  # noqa: BLE001 - controller failure is durable state
                error = exc
        outcome = await _settle_target(
            db,
            target_id=target.id,
            owner_id=owner_id,
            response=response,
            error=error,
            now=now,
        )
        counts[outcome] += 1
    return counts


async def _complete_request(
    db,
    *,
    request_id: str,
    owner_id: str,
    now: datetime,
) -> tuple[str, int]:
    request = await db.scalar(
        select(LabRunControlRequest)
        .where(
            LabRunControlRequest.id == request_id,
            LabRunControlRequest.claim_owner == owner_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if request is None:
        await db.rollback()
        return "pending", 0
    targets = (
        await db.execute(
            select(LabControlTarget).where(
                LabControlTarget.request_id == request.id
            )
        )
    ).scalars().all()
    pending = [target for target in targets if target.status not in ("confirmed_stopped", "quarantined")]
    quarantined = [target for target in targets if target.status == "quarantined"]
    confirmed = [target for target in targets if target.status == "confirmed_stopped"]
    if pending:
        request.status = "pending"
        request.claim_owner = None
        request.claim_expires_at = None
        await db.commit()
        return "pending", len(confirmed)

    runtime_targets = [target for target in targets if target.target_kind == "runtime"]
    executor_targets = [target for target in targets if target.target_kind == "executor"]
    if runtime_targets and all(target.status == "confirmed_stopped" for target in runtime_targets):
        request.provider_stopped_at = max(target.stopped_at for target in runtime_targets if target.stopped_at)
    if executor_targets and all(target.status == "confirmed_stopped" for target in executor_targets):
        request.executor_stopped_at = max(target.stopped_at for target in executor_targets if target.stopped_at)
    request.active_key = None
    request.claim_expires_at = None
    request.completed_at = now
    if quarantined:
        request.status = "quarantined"
        request.last_error = f"{len(quarantined)} control target(s) quarantined"
        await db.commit()
        return "quarantined", len(confirmed)

    request.status = "completed"
    run = await db.get(LabRun, request.run_id)
    if run is not None and run.status in _ACTIVE_RUN_STATES:
        run.status = "cancelled"
        run.ended_at = now
    await db.commit()
    return "completed", len(confirmed)


async def process_pending_controls(
    db,
    *,
    owner_id: str,
    controllers: Mapping[str, Controller],
    now: datetime | None = None,
    limit: int = 100,
) -> dict[str, int]:
    """Poll and converge pending controls independently of Redis wakeups."""
    current_time = _now(now)
    ids = (
        await db.execute(
            select(LabRunControlRequest.id)
            .where(
                LabRunControlRequest.status.in_(("pending", "processing")),
                or_(
                    LabRunControlRequest.status == "pending",
                    LabRunControlRequest.claim_expires_at.is_(None),
                    LabRunControlRequest.claim_expires_at <= current_time,
                    LabRunControlRequest.claim_owner == owner_id,
                ),
            )
            .order_by(LabRunControlRequest.created_at, LabRunControlRequest.id)
            .limit(limit)
        )
    ).scalars().all()
    stats = {"claimed": 0, "completed": 0, "quarantined": 0, "targets_confirmed": 0}
    for request_id in ids:
        request = await _claim_request(
            db, request_id=request_id, owner_id=owner_id, now=current_time
        )
        if request is None:
            continue
        stats["claimed"] += 1
        await _materialize_request_targets(db, request=request)
        await _fence_run(db, request=request, now=current_time)
        await db.commit()
        targets = (
            await db.execute(
                select(LabControlTarget).where(
                    LabControlTarget.request_id == request.id
                )
            )
        ).scalars().all()
        await _process_targets(
            db,
            targets=list(targets),
            parent_id=request.id,
            owner_id=owner_id,
            controllers=controllers,
            now=current_time,
        )
        outcome, confirmed = await _complete_request(
            db, request_id=request.id, owner_id=owner_id, now=current_time
        )
        stats["targets_confirmed"] += confirmed
        if outcome in ("completed", "quarantined"):
            stats[outcome] += 1
    return stats


async def _materialize_global_kill_targets(
    db,
    *,
    kill: LabGlobalKill,
    run_ids: list[str],
) -> list[LabControlTarget]:
    rows: list[LabControlTarget] = []
    runtime_inventory = await _runtime_inventory(db, run_ids=run_ids)
    runtime_run_ids = {run_id for run_id, _target_id, _locator in runtime_inventory}
    for run_id, target_id, locator in runtime_inventory:
        rows.append(
            LabControlTarget(
                kill_id=kill.id,
                run_id=run_id,
                target_kind="runtime",
                target_id=target_id,
                locator_json=locator,
                action="kill",
                epoch=kill.fencing_epoch,
                deadline_at=kill.deadline_at,
            )
        )
    required_runtime = await _runs_requiring_runtime_target(db, run_ids=run_ids)
    for run_id in sorted(required_runtime - runtime_run_ids):
        rows.append(
            _missing_runtime_target(
                request_id=None,
                kill_id=kill.id,
                run_id=run_id,
                action="kill",
                epoch=kill.fencing_epoch,
                deadline_at=kill.deadline_at,
            )
        )
    for run_id, target_id, locator in await _executor_inventory(db, run_ids=run_ids):
        rows.append(
            LabControlTarget(
                kill_id=kill.id,
                run_id=run_id,
                target_kind="executor",
                target_id=target_id,
                locator_json=locator,
                action="kill",
                epoch=_executor_job_epoch(locator),
                deadline_at=kill.deadline_at,
            )
        )
    db.add_all(rows)
    await db.flush()
    return rows


async def activate_global_kill(
    db,
    *,
    requested_by: str,
    idempotency_key: str,
    deadline_at: datetime,
    now: datetime | None = None,
) -> LabGlobalKill:
    """Atomically close admission, advance epoch, and freeze target inventory."""
    existing = await db.scalar(
        select(LabGlobalKill).where(
            LabGlobalKill.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        return existing
    current_time = _now(now)
    state = await ensure_global_control(db)
    run_ids = list(
        (
            await db.execute(
                select(LabRun.id)
                .where(LabRun.status.in_(_ACTIVE_RUN_STATES))
                .order_by(LabRun.id)
            )
        ).scalars().all()
    )
    kill = LabGlobalKill(
        id=str(uuid.uuid4()),
        idempotency_key=idempotency_key,
        requested_by=requested_by,
        status="pending",
        fencing_epoch=state.fencing_epoch + 1,
        watermark_run_count=len(run_ids),
        deadline_at=deadline_at,
        created_at=current_time,
    )
    db.add(kill)
    await db.flush()
    state.admission_open = False
    state.fencing_epoch = kill.fencing_epoch
    state.active_kill_id = kill.id
    state.updated_at = current_time
    await _materialize_global_kill_targets(db, kill=kill, run_ids=run_ids)
    await db.execute(
        update(LabCapabilityGrant)
        .where(
            LabCapabilityGrant.run_id.in_(run_ids),
            LabCapabilityGrant.revoked_at.is_(None),
        )
        .values(revoked_at=current_time)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return kill


async def process_global_kill(
    db,
    *,
    kill_id: str,
    owner_id: str,
    controllers: Mapping[str, Controller],
    now: datetime | None = None,
) -> dict[str, int]:
    current_time = _now(now)
    kill = await db.scalar(
        select(LabGlobalKill)
        .where(LabGlobalKill.id == kill_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if kill is None:
        raise ControlPlaneError("global kill not found")
    if kill.status in ("completed", "quarantined"):
        targets = (
            await db.execute(
                select(LabControlTarget).where(LabControlTarget.kill_id == kill.id)
            )
        ).scalars().all()
        return {
            "confirmed_stopped": sum(target.status == "confirmed_stopped" for target in targets),
            "quarantined": sum(target.status == "quarantined" for target in targets),
            "pending": 0,
        }
    if (
        kill.status == "processing"
        and kill.claim_owner != owner_id
        and kill.claim_expires_at is not None
        and _aware(kill.claim_expires_at) > current_time
    ):
        return {"confirmed_stopped": 0, "quarantined": 0, "pending": 1}
    kill.status = "processing"
    kill.claim_owner = owner_id
    kill.claim_expires_at = current_time + timedelta(seconds=CONTROL_CLAIM_S)
    kill.attempts += 1
    await db.commit()
    targets = (
        await db.execute(
            select(LabControlTarget).where(LabControlTarget.kill_id == kill.id)
        )
    ).scalars().all()
    counts = await _process_targets(
        db,
        targets=list(targets),
        parent_id=kill.id,
        owner_id=owner_id,
        controllers=controllers,
        now=current_time,
    )
    kill = await db.get(LabGlobalKill, kill.id, populate_existing=True)
    if counts["pending"]:
        kill.status = "pending"
        kill.claim_owner = None
        kill.claim_expires_at = None
    else:
        kill.status = "quarantined" if counts["quarantined"] else "completed"
        kill.completed_at = current_time
        kill.claim_expires_at = None
    await db.commit()
    return counts


async def restore_global_admission(
    db, *, expected_kill_id: str, now: datetime | None = None
) -> int:
    """Reopen admission only after the exact active kill has converged."""
    state = await ensure_global_control(db)
    if state.active_kill_id != expected_kill_id:
        raise ControlPlaneError("active global kill changed")
    kill = await db.get(LabGlobalKill, expected_kill_id)
    if kill is None or kill.status != "completed":
        raise ControlPlaneError("global kill has not completed without quarantine")
    state.admission_open = True
    state.active_kill_id = None
    state.updated_at = _now(now)
    await db.commit()
    return state.fencing_epoch


async def run_control_loop(
    session_factory,
    *,
    owner_id: str,
    controllers: Mapping[str, Controller],
    stop_event,
    interval_s: float = 1.0,
) -> None:
    """Poll durable run/global controls; no wakeup is required for progress."""
    while not stop_event.is_set():
        async with session_factory() as db:
            await process_pending_controls(
                db, owner_id=owner_id, controllers=controllers
            )
            kill_ids = (
                await db.execute(
                    select(LabGlobalKill.id).where(
                        LabGlobalKill.status.in_(("pending", "processing"))
                    )
                )
            ).scalars().all()
            for kill_id in kill_ids:
                await process_global_kill(
                    db,
                    kill_id=kill_id,
                    owner_id=owner_id,
                    controllers=controllers,
                )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
