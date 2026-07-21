"""Dedicated terminalization worker and durable terminal-event publisher."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.lab import telemetry
from app.models.coin_hold import CoinHold
from app.models.lab_event import OutboxEvent
from app.models.lab_task import LabTask
from app.models.lab_terminalization import LabTerminalizationCommand
from app.models.memory import Memory
from app.models.notification import Notification
from app.models.resident import Resident
from app.services import lab_terminalization_service

logger = logging.getLogger(__name__)

MAX_COMMAND_ATTEMPTS = 3
EVENT_LEASE_S = 30
EVENT_MEMORY_MAX_CHARS = 80


@dataclass(slots=True)
class DedicatedSessionFactory:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]


def build_session_factory(database_url: str) -> DedicatedSessionFactory:
    engine = create_async_engine(database_url, echo=False)
    return DedicatedSessionFactory(
        engine=engine,
        session_factory=async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        ),
    )


def _reason_code(exc: BaseException) -> str:
    source = ""
    if isinstance(exc, lab_terminalization_service.LabTerminalizationError):
        source = str(exc)
    elif lab_terminalization_service.is_retryable_transaction_error(exc):
        source = getattr(getattr(exc, "orig", None), "__class__", exc.__class__).__name__
    else:
        source = str(exc) or exc.__class__.__name__
    source = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", source)
    source = re.sub(r"[^a-zA-Z0-9]+", "_", source).strip("_").lower()
    return source[:200] or exc.__class__.__name__.lower()


async def _pending_commands(
    db: AsyncSession, *, limit: int, prefer_v1: bool
) -> list[tuple[str, int]]:
    statement = select(
        LabTerminalizationCommand.command_id,
        LabTerminalizationCommand.expected_epoch,
    ).where(LabTerminalizationCommand.status.in_(("pending", "processing")))
    if prefer_v1:
        statement = statement.outerjoin(
            CoinHold,
            CoinHold.id == LabTerminalizationCommand.hold_id,
        ).order_by(
            case((CoinHold.terminalization_version == "v1", 0), else_=1),
            LabTerminalizationCommand.created_at,
            LabTerminalizationCommand.command_id,
        )
    else:
        statement = statement.order_by(
            LabTerminalizationCommand.created_at,
            LabTerminalizationCommand.command_id,
        )
    rows = (
        await db.execute(
            statement.limit(limit)
        )
    ).all()
    return [(str(command_id), int(expected_epoch)) for command_id, expected_epoch in rows]


async def _lock_command_for_failure(
    db: AsyncSession, *, command_id: str
) -> LabTerminalizationCommand | None:
    binding = (
        await db.execute(
            select(
                LabTerminalizationCommand.task_id,
                LabTerminalizationCommand.hold_id,
                LabTerminalizationCommand.status,
            )
            .where(LabTerminalizationCommand.command_id == command_id)
        )
    ).one_or_none()
    if binding is None or binding.status in {"completed", "failed"}:
        return None

    # Match both ORM and PostgreSQL kernel lock order so a failure recorder
    # cannot steal command ownership from a finalizer already holding task/hold.
    await db.execute(
        select(LabTask.id).where(LabTask.id == binding.task_id).with_for_update()
    )
    await db.execute(
        select(CoinHold.id).where(CoinHold.id == binding.hold_id).with_for_update()
    )
    return (
        await db.execute(
            select(LabTerminalizationCommand)
            .where(LabTerminalizationCommand.command_id == command_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def _command_context(
    db: AsyncSession,
    *,
    command_id: str,
) -> tuple[LabTerminalizationCommand, str | None, str | None]:
    command = await db.get(LabTerminalizationCommand, command_id)
    if command is None:
        raise lab_terminalization_service.LabTerminalizationError(
            "terminalization command not found"
        )
    hold_version = await db.scalar(
        select(CoinHold.terminalization_version).where(CoinHold.id == command.hold_id)
    )
    run_id = await db.scalar(
        select(LabTask.accepted_run_id).where(LabTask.id == command.task_id)
    )
    return command, hold_version, run_id


async def _record_failure(
    session_factory,
    *,
    command_id: str,
    task_id: str,
    run_id: str | None,
    exc: BaseException,
) -> str:
    reason = _reason_code(exc)
    retryable = lab_terminalization_service.is_retryable_transaction_error(exc)
    async with session_factory() as db:
        command = await _lock_command_for_failure(db, command_id=command_id)
        if command is None or command.status in {"completed", "failed"}:
            return "ignored"
        command.attempts = min(MAX_COMMAND_ATTEMPTS, int(command.attempts or 0) + 1)
        command.claimed_at = command.claimed_at or datetime.now(UTC)
        command.last_error = reason
        if retryable and command.attempts < MAX_COMMAND_ATTEMPTS:
            command.status = "pending"
            await db.commit()
            return "retried"
        command.status = "failed"
        command.completed_at = datetime.now(UTC)
        await db.commit()

    telemetry.emit_alert(
        telemetry.LabAlert.TERMINALIZATION_FAILED,
        command_id=command_id,
        run_id=run_id,
        task_id=task_id,
        reason=reason,
        count=MAX_COMMAND_ATTEMPTS if retryable else 1,
    )
    return "failed"


async def process_pending_commands(
    session_factory,
    *,
    terminalizer_session_factory=None,
    limit: int = 100,
) -> dict:
    """Recover pending commands cohort-aware without touching service internals."""

    if settings.lab_terminalizer_v2_enabled and terminalizer_session_factory is None:
        raise RuntimeError(
            "v2 terminalizer requires a dedicated terminalizer session factory"
        )

    stats = {"completed": 0, "retried": 0, "failed": 0, "deferred": 0}
    async with session_factory() as db:
        pending = await _pending_commands(
            db,
            limit=limit,
            prefer_v1=not settings.lab_terminalizer_v2_enabled,
        )

    for command_id, expected_epoch in pending:
        async with session_factory() as db:
            command, hold_version, run_id = await _command_context(db, command_id=command_id)
            task_id = command.task_id

        try:
            if hold_version == "v1":
                async with session_factory() as db:
                    await lab_terminalization_service.finalize_legacy(
                        db,
                        command_id,
                        expected_epoch,
                    )
                stats["completed"] += 1
                continue

            if hold_version == "v2":
                if not settings.lab_terminalizer_v2_enabled:
                    stats["deferred"] += 1
                    continue
                async with terminalizer_session_factory() as db:
                    await lab_terminalization_service.finalize(
                        db,
                        command_id,
                        expected_epoch,
                    )
                stats["completed"] += 1
                continue

            raise lab_terminalization_service.LabTerminalizationError(
                "task hold has no recognized terminalization cohort"
            )
        except Exception as exc:  # noqa: BLE001
            outcome = await _record_failure(
                session_factory,
                command_id=command_id,
                task_id=task_id,
                run_id=run_id,
                exc=exc,
            )
            if outcome == "retried":
                stats["retried"] += 1
            elif outcome == "failed":
                stats["failed"] += 1

    return stats


async def _eligible_terminal_event_ids(
    db: AsyncSession,
    *,
    limit: int,
    now: datetime,
) -> list[int]:
    rows = (
        await db.execute(
            select(OutboxEvent.id)
            .where(
                OutboxEvent.topic == lab_terminalization_service.TERMINAL_EVENT_TOPIC,
                OutboxEvent.published_at.is_(None),
                OutboxEvent.dispatch_status == "pending",
                or_(OutboxEvent.next_attempt_at.is_(None), OutboxEvent.next_attempt_at <= now),
                or_(OutboxEvent.locked_until.is_(None), OutboxEvent.locked_until <= now),
            )
            .order_by(OutboxEvent.id)
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def _claim_terminal_event(
    db: AsyncSession,
    *,
    outbox_id: int,
    now: datetime,
) -> bool:
    result = await db.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.id == outbox_id,
            OutboxEvent.published_at.is_(None),
            OutboxEvent.dispatch_status == "pending",
            or_(OutboxEvent.locked_until.is_(None), OutboxEvent.locked_until <= now),
        )
        .values(locked_until=now + timedelta(seconds=EVENT_LEASE_S))
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return (result.rowcount or 0) == 1


def _capped_event_memory(content: str) -> str:
    if len(content) <= EVENT_MEMORY_MAX_CHARS:
        return content
    return content[:EVENT_MEMORY_MAX_CHARS].rstrip() + "…"


async def _apply_terminal_side_effects(db: AsyncSession, row: OutboxEvent) -> Notification | None:
    payload = row.payload_json or {}
    if payload.get("type") != "lab.task.terminalized":
        return None
    if payload.get("target_status") != "completed":
        return None

    task = await db.get(LabTask, payload.get("task_id"))
    if task is None:
        raise RuntimeError("terminal_task_missing")

    notification = None
    existing_notifications = (
        await db.execute(
            select(Notification).where(
                Notification.user_id == task.issuer_user_id,
                Notification.kind == "lab",
            )
        )
    ).scalars().all()
    if not any((item.payload_json or {}).get("event_id") == row.event_id for item in existing_notifications):
        notification = Notification(
            user_id=task.issuer_user_id,
            kind="lab",
            title="委托完成",
            body=f"你的委托「{task.title}」已完成，可领取产物",
            payload_json={"task_id": task.id, "event_id": row.event_id},
        )
        db.add(notification)

    if task.researcher_slug:
        resident = (
            await db.execute(
                select(Resident).where(Resident.slug == task.researcher_slug)
            )
        ).scalar_one_or_none()
        if resident is not None:
            existing_memories = (
                await db.execute(
                    select(Memory).where(
                        Memory.resident_id == resident.id,
                        Memory.source == "lab_task",
                    )
                )
            ).scalars().all()
            if not any((item.metadata_json or {}).get("event_id") == row.event_id for item in existing_memories):
                db.add(
                    Memory(
                        resident_id=resident.id,
                        type="event",
                        content=_capped_event_memory(
                            f"在实验楼完成了一项真实委托「{task.title}」，赚到了报酬"
                        ),
                        importance=0.75,
                        source="lab_task",
                        metadata_json={"task_id": task.id, "event_id": row.event_id},
                    )
                )

    await db.flush()
    return notification


async def _send_notification(notification: Notification | None) -> None:
    if notification is None:
        return
    try:
        from app.ws.manager import manager

        if await manager.is_online(notification.user_id):
            await manager.send(
                notification.user_id,
                {
                    "type": "notification",
                    "id": notification.id,
                    "kind": notification.kind,
                    "title": notification.title,
                    "body": notification.body,
                    "payload": notification.payload_json or {},
                    "read": notification.read_at is not None,
                    "created_at": (
                        notification.created_at.isoformat()
                        if notification.created_at is not None
                        else None
                    ),
                },
            )
    except Exception:
        logger.warning(
            "terminal notification WS push failed for user %s",
            notification.user_id,
            exc_info=True,
        )


async def _publish_terminal_event_id(
    db: AsyncSession,
    *,
    outbox_id: int,
    now: datetime,
) -> tuple[str, Notification | None]:
    if not await _claim_terminal_event(db, outbox_id=outbox_id, now=now):
        return "unclaimed", None
    row = await db.get(OutboxEvent, outbox_id)
    if row is None:
        return "unclaimed", None

    if (row.payload_json or {}).get("type") != "lab.task.terminalized":
        row.locked_until = None
        await db.commit()
        return "skipped", None

    try:
        notification = await _apply_terminal_side_effects(db, row)
        row.published_at = now
        row.dispatch_status = "published"
        row.locked_until = None
        row.last_error = None
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        row.attempts = int(row.attempts or 0) + 1
        row.last_error = _reason_code(exc)
        row.locked_until = None
        row.next_attempt_at = now + timedelta(
            seconds=min(60, 2 ** min(row.attempts, 5))
        )
        await db.commit()
        return "retried", None
    return "published", notification


async def publish_terminal_event(db: AsyncSession, *, event_id: str) -> bool:
    """Publish one committed terminal event without bypassing the worker claim."""
    outbox_id = await db.scalar(
        select(OutboxEvent.id).where(
            OutboxEvent.event_id == event_id,
            OutboxEvent.topic == lab_terminalization_service.TERMINAL_EVENT_TOPIC,
        )
    )
    if outbox_id is None:
        return False
    outcome, notification = await _publish_terminal_event_id(
        db,
        outbox_id=outbox_id,
        now=datetime.now(UTC),
    )
    if outcome == "published":
        await _send_notification(notification)
        return True
    return False


async def publish_terminal_events(session_factory, *, limit: int = 100) -> dict:
    stats = {"published": 0, "retried": 0, "skipped": 0}
    now = datetime.now(UTC)
    async with session_factory() as db:
        event_ids = await _eligible_terminal_event_ids(db, limit=limit, now=now)

    for outbox_id in event_ids:
        async with session_factory() as db:
            outcome, notification = await _publish_terminal_event_id(
                db,
                outbox_id=outbox_id,
                now=now,
            )
        if outcome == "unclaimed":
            continue
        if outcome == "published":
            await _send_notification(notification)
        stats[outcome] += 1

    return stats


async def run_terminalizer_pass(
    session_factory,
    *,
    terminalizer_session_factory=None,
    limit: int = 100,
) -> dict:
    command_stats = await process_pending_commands(
        session_factory,
        terminalizer_session_factory=terminalizer_session_factory,
        limit=limit,
    )
    publish_stats = await publish_terminal_events(session_factory, limit=limit)
    return {**command_stats, **{f"events_{k}": v for k, v in publish_stats.items()}}


async def run_terminalizer_loop(
    session_factory,
    *,
    terminalizer_session_factory=None,
    stop_event,
    interval_s: float = 1.0,
) -> None:
    while not stop_event.is_set():
        try:
            await run_terminalizer_pass(
                session_factory,
                terminalizer_session_factory=terminalizer_session_factory,
            )
        except Exception:  # noqa: BLE001
            logger.warning("terminalizer pass failed; retrying", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
