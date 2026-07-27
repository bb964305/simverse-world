"""Durable command submission and the single Lab escrow transaction owner."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Iterable

from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.coin_hold import CoinHold
from app.models.lab_event import OutboxEvent
from app.models.lab_lease import LabRunLease
from app.models.lab_run import LabRun
from app.models.lab_task import LabTask
from app.models.lab_terminalization import (
    LabTerminalizationCommand,
    LabTerminalizationReceipt,
)
from app.models.resident import Resident
from app.models.user import User
from app.services import coin_service
from app.services.system_users import NON_USER_CREATOR_IDS

logger = logging.getLogger(__name__)

MAX_TRANSACTION_ATTEMPTS = 3
ACTIVE_RUN_STATES = ("queued", "running", "needs_approval")
COMMAND_SCHEMA = "simverse.lab.terminalization-command.v2"
TERMINAL_EVENT_TOPIC = "lab.task.terminalized"
SETTLEMENT_OPERATIONS = {
    "accept": (("review",), "completed"),
    "auto_release": (("review",), "completed"),
    "arbitrate_settle": (("rejected",), "completed"),
}
REFUND_OPERATIONS = {
    "cancel": (
        ("funded", "assigned", "running"),
        "cancelled",
    ),
    "fail": (("assigned", "running"), "failed"),
    "expire": (("funded", "assigned", "running"), "expired"),
    "arbitrate_refund": (("rejected",), "cancelled"),
}


class LabTerminalizationError(Exception):
    """A command is invalid, stale, unauthorized, or lost terminal ownership."""


def require_v2_consumer_ready() -> None:
    """Reject v2 production unless its dedicated consumer can be started."""
    if not (
        settings.lab_terminalizer_v2_enabled
        and settings.lab_terminalizer_worker_enabled
        and bool((settings.lab_terminalizer_database_url or "").strip())
    ):
        raise LabTerminalizationError("v2 terminalization consumer is not ready")


async def require_task_consumer_ready(
    db: AsyncSession, task: LabTask
) -> str:
    """Return the task cohort after enforcing the v2 consumer contract."""
    hold_version = await db.scalar(
        select(CoinHold.terminalization_version).where(CoinHold.id == task.hold_id)
    )
    if hold_version not in {"v1", "v2"}:
        raise LabTerminalizationError(
            "task hold has no recognized terminalization cohort"
        )
    if hold_version == "v2":
        require_v2_consumer_ready()
    return hold_version


def _database_commit_checkpoint() -> None:
    """Test seam for the crash window after the kernel returns, before commit."""


def _stable_uuid(kind: str, command_id: str) -> str:
    digest = hashlib.sha256(f"{kind}:{command_id}".encode()).hexdigest()
    return (
        f"{digest[:8]}-{digest[8:12]}-5{digest[13:16]}-"
        f"a{digest[17:20]}-{digest[20:32]}"
    )


def _canonical_digest(value: dict) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _sqlstate(exc: BaseException) -> str | None:
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        for name in ("sqlstate", "pgcode"):
            value = getattr(current, name, None)
            if isinstance(value, str):
                return value
        for name in ("orig", "__cause__", "__context__"):
            nested = getattr(current, name, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
    return None


def is_retryable_transaction_error(exc: BaseException) -> bool:
    """Only PostgreSQL serialization failure/deadlock may restart the transaction."""
    return isinstance(exc, DBAPIError) and _sqlstate(exc) in {"40001", "40P01"}


def _operation_contract(operation: str) -> tuple[tuple[str, ...], str, str]:
    if operation in SETTLEMENT_OPERATIONS:
        expected, target = SETTLEMENT_OPERATIONS[operation]
        return expected, target, "settle"
    if operation in REFUND_OPERATIONS:
        expected, target = REFUND_OPERATIONS[operation]
        return expected, target, "refund"
    raise LabTerminalizationError(f"unsupported terminalization operation {operation}")


async def expected_epoch_for_task(db: AsyncSession, task: LabTask) -> int:
    if not task.accepted_run_id:
        return 0
    value = await db.scalar(
        select(LabRunLease.fencing_epoch).where(
            LabRunLease.run_id == task.accepted_run_id
        )
    )
    return int(value or 0)


async def _validate_operation_authority(
    db: AsyncSession,
    *,
    task: LabTask,
    operation: str,
    actor: str,
) -> None:
    if operation in {"accept", "cancel"}:
        if actor != task.issuer_user_id:
            raise LabTerminalizationError("terminalization actor is not the task issuer")
    elif operation == "auto_release":
        if actor != "scheduler:auto-release":
            raise LabTerminalizationError("auto-release actor binding is invalid")
        deadline = task.review_deadline_at
        if deadline is None or _as_utc(deadline) > datetime.now(UTC):
            raise LabTerminalizationError("task has not reached its auto-release deadline")
    elif operation == "expire":
        if actor != "scheduler:expire":
            raise LabTerminalizationError("expiry actor binding is invalid")
        if _as_utc(task.deadline_at) > datetime.now(UTC):
            raise LabTerminalizationError("task has not reached its expiry deadline")
    elif operation == "fail":
        if not task.accepted_run_id or actor != f"runner:{task.accepted_run_id}":
            raise LabTerminalizationError("failure actor binding is invalid")
    elif operation in {"arbitrate_settle", "arbitrate_refund"}:
        is_admin = await db.scalar(select(User.is_admin).where(User.id == actor))
        if is_admin is not True:
            raise LabTerminalizationError("arbitration actor is not an admin")


async def settlement_splits(db: AsyncSession, task: LabTask) -> list[coin_service.Split]:
    researcher = None
    if task.researcher_slug:
        researcher = (
            await db.execute(
                select(Resident).where(Resident.slug == task.researcher_slug)
            )
        ).scalar_one_or_none()
        if researcher is None:
            raise LabTerminalizationError("settlement researcher does not exist")

    reward = task.reward_sc
    fee = task.platform_fee_sc
    creator_id = researcher.creator_id if researcher else None
    share_bps = task.terminal_creator_share_bps
    if share_bps is None:
        share_bps = int(round(settings.lab_creator_share * 10_000))
    if isinstance(share_bps, bool) or not 0 <= share_bps <= 10_000:
        raise LabTerminalizationError("settlement creator-share policy is invalid")
    creator_amount = reward * share_bps // 10_000
    treasury_amount = reward - creator_amount
    splits: list[coin_service.Split] = []
    if creator_id and creator_id not in NON_USER_CREATOR_IDS and creator_amount > 0:
        splits.append((creator_id, creator_amount, f"lab_reward:{task.id}"))
    else:
        treasury_amount = reward
    if task.researcher_slug and treasury_amount > 0:
        splits.append(
            (
                f"treasury:{task.researcher_slug}",
                treasury_amount,
                f"lab_treasury:{task.id}",
            )
        )
    if fee > 0:
        splits.append(("sink", fee, f"lab_fee:{task.id}"))
    return splits


async def refund_splits(
    db: AsyncSession, task: LabTask, hold: CoinHold, operation: str, reason: str
) -> list[coin_service.Split]:
    """Refund escrow minus metered model cost for consumed-run terminal paths."""
    chargeable = operation in {"fail", "cancel", "expire"}
    cost_sc = 0
    if chargeable and task.accepted_run_id:
        run = await db.get(LabRun, task.accepted_run_id)
        if run is not None and run.adapter != "mock":
            if (run.error or "").startswith("cost_unknown:"):
                raise LabTerminalizationError(
                    "model cost is unknown; refund settlement is blocked"
                )
            from app.lab.model_policy import cost_usd_cents_to_sc

            cost_sc = min(
                hold.amount,
                cost_usd_cents_to_sc(
                    run.cost_usd_cents or 0,
                    sc_per_usd=run.model_cost_sc_per_usd,
                ),
            )
    refund_sc = hold.amount - cost_sc
    splits: list[coin_service.Split] = []
    if refund_sc > 0:
        splits.append((hold.user_id, refund_sc, reason))
    if cost_sc > 0:
        splits.append(("sink", cost_sc, f"lab_model_cost:{task.id}"))
    return coin_service.validate_distribution(splits, hold.amount)


async def submit_command(
    db: AsyncSession,
    *,
    task: LabTask,
    operation: str,
    actor: str,
    expected_epoch: int | None = None,
) -> LabTerminalizationCommand:
    """Persist an exact-retry command; this function performs no terminal effect."""
    expected_statuses, target_status, terminal_action = _operation_contract(operation)
    if not task.hold_id:
        raise LabTerminalizationError("task has no escrow hold")
    if task.status not in expected_statuses:
        raise LabTerminalizationError(
            f"task status {task.status} is not eligible for {operation}"
        )
    if not actor:
        raise LabTerminalizationError("terminalization actor is required")
    await _validate_operation_authority(
        db, task=task, operation=operation, actor=actor
    )

    hold = await db.get(CoinHold, task.hold_id)
    if hold is None or hold.status != "held":
        raise LabTerminalizationError("task escrow hold is missing or not held")
    if hold.user_id != task.issuer_user_id or hold.reason != f"lab_task:{task.id}":
        raise LabTerminalizationError("task escrow ownership binding is invalid")
    if hold.terminalization_version == "v2" and hold.cutover_at is None:
        raise LabTerminalizationError("v2 task escrow has no cutover watermark")

    epoch = (
        await expected_epoch_for_task(db, task)
        if expected_epoch is None
        else expected_epoch
    )
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise LabTerminalizationError("expected epoch must be a nonnegative integer")

    idempotency_key = f"{operation}:{task.id}:{task.hold_id}:{epoch}"
    command_id = _stable_uuid("command", idempotency_key)
    if (
        hold.terminalization_version == "v2"
        and db.get_bind().dialect.name == "postgresql"
    ):
        await db.execute(text("SET LOCAL ROLE lab_command_submitter_v2"))
        submitted_id = await db.scalar(
            text(
                "SELECT public.submit_lab_terminalization_command("
                ":operation, :task_id, :actor, :expected_epoch)"
            ),
            {
                "operation": operation,
                "task_id": task.id,
                "actor": actor,
                "expected_epoch": epoch,
            },
        )
        if submitted_id != command_id:
            await db.rollback()
            raise LabTerminalizationError(
                "financial kernel returned a noncanonical command identity"
            )
        await db.execute(text("RESET ROLE"))
        command = await db.get(LabTerminalizationCommand, command_id)
        if command is None:
            await db.rollback()
            raise LabTerminalizationError(
                "financial kernel did not persist the submitted command"
            )
        await db.commit()
        return command

    existing = await db.get(LabTerminalizationCommand, command_id)
    if existing is not None:
        if existing.actor != actor:
            raise LabTerminalizationError("command retry actor binding changed")
        return existing

    payload: dict = {
        "schema": COMMAND_SCHEMA,
        "expected_task_statuses": list(expected_statuses),
        "target_status": target_status,
        "terminal_action": terminal_action,
        "reason": f"lab_{operation}:{task.id}",
        "completed_at": target_status == "completed",
        "event_id": _stable_uuid("event", command_id),
        "receipt_id": _stable_uuid("receipt", command_id),
    }
    if terminal_action == "settle":
        splits = await settlement_splits(db, task)
        payload["splits"] = [
            {"recipient_key": recipient, "amount": amount, "reason": reason}
            for recipient, amount, reason in coin_service.validate_distribution(
                splits, hold.amount
            )
        ]
    else:
        splits = await refund_splits(
            db, task, hold, operation, payload["reason"]
        )
        payload["splits"] = [
            {"recipient_key": recipient, "amount": amount, "reason": reason}
            for recipient, amount, reason in splits
        ]

    command = LabTerminalizationCommand(
        command_id=command_id,
        operation=operation,
        task_id=task.id,
        hold_id=task.hold_id,
        actor=actor,
        expected_epoch=epoch,
        idempotency_key=idempotency_key,
        status="pending",
        payload_json=payload,
    )
    db.add(command)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await db.get(LabTerminalizationCommand, command_id)
        if existing is None or existing.actor != actor:
            raise
        return existing
    await db.refresh(command)
    return command


def _payload_splits(command: LabTerminalizationCommand) -> list[coin_service.Split]:
    raw_splits = (command.payload_json or {}).get("splits")
    if not isinstance(raw_splits, list):
        raise LabTerminalizationError("terminalization command has no split list")
    splits: list[coin_service.Split] = []
    for raw in raw_splits:
        if not isinstance(raw, dict):
            raise LabTerminalizationError("terminalization command contains an invalid split")
        splits.append((raw.get("recipient_key"), raw.get("amount"), raw.get("reason")))
    return splits


async def _prepare_task_runs(
    db: AsyncSession,
    task: LabTask,
    command: LabTerminalizationCommand,
    *,
    expected_epoch: int,
    strict: bool,
) -> None:
    runs = (
        (
            await db.execute(
                select(LabRun)
                .where(LabRun.task_id == task.id)
                .order_by(LabRun.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    run_ids = [run.id for run in runs]
    leases: dict[str, LabRunLease] = {}
    if run_ids:
        leases = {
            lease.run_id: lease
            for lease in (
                (
                    await db.execute(
                        select(LabRunLease)
                        .where(LabRunLease.run_id.in_(run_ids))
                        .order_by(LabRunLease.run_id)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
        }

    accepted = next(
        (run for run in runs if run.id == task.accepted_run_id), None
    )
    accepted_lease = leases.get(task.accepted_run_id or "")
    if task.accepted_run_id:
        if accepted is None:
            raise LabTerminalizationError("accepted run binding is missing")
        if strict and accepted_lease is None:
            raise LabTerminalizationError("accepted v2 run has no fencing lease")
        live_epoch = accepted_lease.fencing_epoch if accepted_lease else 0
    else:
        live_epoch = 0
    if live_epoch != expected_epoch:
        raise LabTerminalizationError(
            f"command epoch {expected_epoch} does not match live epoch {live_epoch}"
        )

    if len(runs) > 1:
        raise LabTerminalizationError(
            "terminalization requires exactly one linked run"
        )
    if task.accepted_run_id is None and runs:
        raise LabTerminalizationError(
            "terminalization has an unbound linked run"
        )

    if strict:
        if command.operation in SETTLEMENT_OPERATIONS:
            if accepted is None or accepted.status != "succeeded":
                raise LabTerminalizationError(
                    "v2 settlement requires a succeeded accepted run"
                )
        elif command.operation == "arbitrate_refund":
            if accepted is None or accepted.status != "succeeded":
                raise LabTerminalizationError(
                    "v2 arbitration refund requires a succeeded accepted run"
                )
        elif command.operation == "fail":
            if accepted is None or accepted.status not in {"failed", "cancelled"}:
                raise LabTerminalizationError(
                    "v2 failure refund requires a terminal accepted run"
                )
        elif task.status != "funded" and accepted is None:
            raise LabTerminalizationError(
                "v2 refund cohort is missing its accepted run"
            )

    if command.operation not in REFUND_OPERATIONS:
        return

    now = datetime.now(UTC)
    for run in runs:
        lease = leases.get(run.id)
        if strict and lease is None:
            raise LabTerminalizationError(
                f"linked v2 run {run.id} has no fencing lease"
            )
        if lease is not None:
            lease.fencing_epoch += 1
        if run.status in ACTIVE_RUN_STATES:
            run.status = "cancelled"
            run.ended_at = now


def _validate_command_binding(
    command: LabTerminalizationCommand,
    task: LabTask,
    *,
    expected_epoch: int,
) -> tuple[tuple[str, ...], str, str]:
    expected_statuses, target_status, terminal_action = _operation_contract(
        command.operation
    )
    payload = command.payload_json or {}
    if payload.get("schema") != COMMAND_SCHEMA:
        raise LabTerminalizationError("terminalization command schema is invalid")
    if command.task_id != task.id or command.hold_id != task.hold_id:
        raise LabTerminalizationError("terminalization command task/hold binding changed")
    if command.expected_epoch != expected_epoch:
        raise LabTerminalizationError("terminalization command epoch binding changed")
    if payload.get("expected_task_statuses") != list(expected_statuses):
        raise LabTerminalizationError("terminalization expected states changed")
    if payload.get("target_status") != target_status:
        raise LabTerminalizationError("terminalization target state changed")
    if payload.get("terminal_action") != terminal_action:
        raise LabTerminalizationError("terminalization action changed")
    if payload.get("completed_at") is not (target_status == "completed"):
        raise LabTerminalizationError("terminalization timestamp policy changed")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason or len(reason) > 100:
        raise LabTerminalizationError("terminalization reason is invalid")
    if payload.get("event_id") != _stable_uuid("event", command.command_id):
        raise LabTerminalizationError("terminal event identity changed")
    if payload.get("receipt_id") != _stable_uuid("receipt", command.command_id):
        raise LabTerminalizationError("terminal receipt identity changed")
    if command.operation in {"accept", "cancel"} and command.actor != task.issuer_user_id:
        raise LabTerminalizationError("terminalization actor binding changed")
    if not command.actor:
        raise LabTerminalizationError("terminalization actor is empty")
    return expected_statuses, target_status, terminal_action


async def _completed_receipt(
    db: AsyncSession, command_id: str
) -> LabTerminalizationReceipt:
    receipt = await db.scalar(
        select(LabTerminalizationReceipt).where(
            LabTerminalizationReceipt.command_id == command_id
        )
    )
    if receipt is None:
        raise LabTerminalizationError("completed command has no receipt")
    return receipt


async def _finalize_orm_attempt(
    db: AsyncSession, command_id: str, expected_epoch: int
) -> LabTerminalizationReceipt:
    seed = await db.get(LabTerminalizationCommand, command_id)
    if seed is None:
        raise LabTerminalizationError("terminalization command not found")

    task = (
        await db.execute(
            select(LabTask).where(LabTask.id == seed.task_id).with_for_update()
        )
    ).scalar_one_or_none()
    if task is None:
        raise LabTerminalizationError("terminalization task not found")
    hold = (
        await db.execute(
            select(CoinHold).where(CoinHold.id == seed.hold_id).with_for_update()
        )
    ).scalar_one_or_none()
    if hold is None:
        raise LabTerminalizationError("terminalization hold not found")
    command = (
        await db.execute(
            select(LabTerminalizationCommand)
            .where(LabTerminalizationCommand.command_id == command_id)
            .with_for_update()
        )
    ).scalar_one()
    expected_statuses, target_status, terminal_action = _validate_command_binding(
        command, task, expected_epoch=expected_epoch
    )
    await _validate_operation_authority(
        db, task=task, operation=command.operation, actor=command.actor
    )
    if hold.id != command.hold_id or hold.user_id != task.issuer_user_id:
        raise LabTerminalizationError("terminalization hold ownership binding changed")
    if hold.reason != f"lab_task:{task.id}":
        raise LabTerminalizationError("terminalization hold reason binding changed")
    if command.status == "completed":
        return await _completed_receipt(db, command_id)
    if command.status not in {"pending", "processing"}:
        raise LabTerminalizationError(
            f"terminalization command is {command.status}, not pending"
        )
    if task.status not in expected_statuses:
        raise LabTerminalizationError(
            f"task status {task.status} is not eligible for {command.operation}"
        )

    if hold.status != "held":
        raise LabTerminalizationError("terminalization hold is not held")
    if hold.terminalization_version == "v2" and hold.cutover_at is None:
        raise LabTerminalizationError("v2 terminalization hold has no cutover watermark")

    splits = coin_service.validate_distribution(
        _payload_splits(command), hold.amount
    )
    if terminal_action == "settle":
        canonical_splits = coin_service.validate_distribution(
            await settlement_splits(db, task), hold.amount
        )
        if splits != canonical_splits:
            raise LabTerminalizationError(
                "terminalization command changed the canonical settlement distribution"
            )
    elif splits != await refund_splits(
        db,
        task,
        hold,
        command.operation,
        str((command.payload_json or {}).get("reason") or ""),
    ):
        raise LabTerminalizationError("refund distribution binding changed")
    await coin_service.lock_distribution_accounts(db, splits)
    await _prepare_task_runs(
        db,
        task,
        command,
        expected_epoch=expected_epoch,
        strict=hold.terminalization_version == "v2",
    )

    if terminal_action == "settle":
        hold = await coin_service.settle_pending(
            db,
            command.hold_id,
            splits,
            operation_key=command.command_id,
        )
        journal_count = len(splits)
    else:
        hold = await coin_service.refund_pending(
            db,
            command.hold_id,
            str((command.payload_json or {}).get("reason") or ""),
            operation_key=command.command_id,
            splits=splits,
        )
        journal_count = len(splits)

    now = datetime.now(UTC)
    moved = await db.execute(
        update(LabTask)
        .where(LabTask.id == task.id, LabTask.status.in_(expected_statuses))
        .values(
            status=target_status,
            completed_at=now if target_status == "completed" else None,
            updated_at=now,
        )
    )
    if (moved.rowcount or 0) != 1:
        raise LabTerminalizationError("task lost terminal ownership")

    event_id = _stable_uuid("event", command.command_id)
    receipt_id = _stable_uuid("receipt", command.command_id)
    effect = {
        "command_id": command.command_id,
        "operation": command.operation,
        "task_id": task.id,
        "hold_id": hold.id,
        "target_status": target_status,
        "terminal_action": terminal_action,
        "amount": hold.amount,
        "journal_count": journal_count,
        "event_id": event_id,
    }
    digest = _canonical_digest(effect)
    outbox_payload = {
        "type": "lab.task.terminalized",
        "schema_version": 2,
        "event_id": event_id,
        "receipt_id": receipt_id,
        **effect,
    }
    db.add(
        OutboxEvent(
            event_id=event_id,
            tenant_id=task.issuer_user_id,
            run_id=task.accepted_run_id,
            topic=TERMINAL_EVENT_TOPIC,
            payload_json=outbox_payload,
        )
    )
    receipt = LabTerminalizationReceipt(
        receipt_id=receipt_id,
        command_id=command.command_id,
        task_id=task.id,
        hold_id=hold.id,
        operation=command.operation,
        event_id=event_id,
        amount=hold.amount,
        journal_count=journal_count,
        result_digest=digest,
        payload_json=effect,
    )
    db.add(receipt)
    command.status = "completed"
    command.claimed_at = command.claimed_at or now
    command.completed_at = now
    command.last_error = None
    await db.flush()
    await db.commit()
    return receipt


async def _finalize_database_kernel(
    db: AsyncSession, command_id: str, expected_epoch: int
) -> str:
    receipt_id = await db.scalar(
        text(
            "SELECT public.finalize_lab_terminalization"
            "(:command_id, :expected_epoch)"
        ),
        {"command_id": command_id, "expected_epoch": expected_epoch},
    )
    _database_commit_checkpoint()
    await db.commit()
    if not isinstance(receipt_id, str) or not receipt_id:
        raise LabTerminalizationError("financial kernel returned no receipt id")
    return receipt_id


async def finalize(
    db: AsyncSession,
    command_id: str,
    expected_epoch: int,
    *,
    _allow_local_kernel: bool = False,
) -> LabTerminalizationReceipt | str:
    """Finalize exactly one command, with bounded transient-transaction retry."""
    if not settings.lab_terminalizer_v2_enabled and not _allow_local_kernel:
        raise LabTerminalizationError("v2 terminalizer rollout gate is closed")
    use_database_kernel = (
        settings.lab_terminalizer_v2_enabled
        and db.get_bind().dialect.name == "postgresql"
    )

    for attempt in range(1, MAX_TRANSACTION_ATTEMPTS + 1):
        try:
            if use_database_kernel:
                return await _finalize_database_kernel(
                    db, command_id, expected_epoch
                )
            return await _finalize_orm_attempt(db, command_id, expected_epoch)
        except Exception as exc:
            retryable = is_retryable_transaction_error(exc)
            await db.rollback()
            if not retryable or attempt == MAX_TRANSACTION_ATTEMPTS:
                raise
            logger.warning(
                "retrying terminalization command %s after SQLSTATE %s (%s/%s)",
                command_id,
                _sqlstate(exc),
                attempt,
                MAX_TRANSACTION_ATTEMPTS,
            )
    raise AssertionError("terminalization retry loop exhausted")


async def finalize_legacy(
    db: AsyncSession, command_id: str, expected_epoch: int
) -> LabTerminalizationReceipt:
    """Temporary v1 cohort owner used only while legacy admission remains open."""
    for attempt in range(1, MAX_TRANSACTION_ATTEMPTS + 1):
        try:
            return await _finalize_orm_attempt(db, command_id, expected_epoch)
        except Exception as exc:
            retryable = is_retryable_transaction_error(exc)
            await db.rollback()
            if not retryable or attempt == MAX_TRANSACTION_ATTEMPTS:
                raise
            logger.warning(
                "retrying legacy terminalization command %s after SQLSTATE %s (%s/%s)",
                command_id,
                _sqlstate(exc),
                attempt,
                MAX_TRANSACTION_ATTEMPTS,
            )
    raise AssertionError("legacy terminalization retry loop exhausted")


async def submit_for_caller(
    db: AsyncSession,
    *,
    task: LabTask,
    operation: str,
    actor: str,
) -> LabTerminalizationCommand:
    """Submit every cohort, but inline only the explicitly legacy v1 cohort."""
    hold_version = await require_task_consumer_ready(db, task)
    command = await submit_command(
        db, task=task, operation=operation, actor=actor
    )
    if command.status == "failed":
        raise LabTerminalizationError(
            "terminalization command failed and requires reconciliation"
        )
    if hold_version == "v1":
        receipt = await finalize_legacy(db, command.command_id, command.expected_epoch)
        from app.lab import terminalizer

        await terminalizer.publish_terminal_event(db, event_id=receipt.event_id)
        await db.refresh(task)
    return command


async def pending_commands(
    db: AsyncSession, task_id: str, *, operations: Iterable[str] | None = None
) -> list[LabTerminalizationCommand]:
    statement = select(LabTerminalizationCommand).where(
        LabTerminalizationCommand.task_id == task_id,
        LabTerminalizationCommand.status == "pending",
    )
    if operations is not None:
        statement = statement.where(
            LabTerminalizationCommand.operation.in_(tuple(operations))
        )
    return list((await db.execute(statement)).scalars().all())
