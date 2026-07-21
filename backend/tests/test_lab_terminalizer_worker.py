from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.coin_hold import CoinHold
from app.models.lab_event import OutboxEvent
from app.models.lab_lease import LabRunLease
from app.models.lab_run import LabRun
from app.models.lab_task import LabTask
from app.models.lab_terminalization import LabTerminalizationCommand
from app.models.memory import Memory
from app.models.notification import Notification
from app.models.resident import Resident
from app.models.user import User
from app.services import coin_service
from app.services import lab_terminalization_service


@pytest.fixture
def factory(db_engine):
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


async def _seed_command(
    factory: async_sessionmaker[AsyncSession],
    *,
    prefix: str,
    terminalization_version: str,
    operation: str = "accept",
    task_status: str = "review",
    run_status: str = "succeeded",
) -> str:
    issuer_id = f"{prefix}-issuer"
    creator_id = f"{prefix}-creator"
    resident_slug = f"{prefix}-researcher"
    task_id = f"{prefix}-task"
    run_id = f"{prefix}-run"
    now = datetime.now(UTC)

    async with factory() as db:
        db.add_all(
            [
                User(
                    id=issuer_id,
                    name=issuer_id,
                    email=f"{issuer_id}@test.invalid",
                    soul_coin_balance=110,
                ),
                User(
                    id=creator_id,
                    name=creator_id,
                    email=f"{creator_id}@test.invalid",
                    soul_coin_balance=0,
                ),
                Resident(
                    slug=resident_slug,
                    name=resident_slug,
                    creator_id=creator_id,
                    resident_type="npc",
                ),
            ]
        )
        await db.commit()

    async with factory() as db:
        hold_id = await coin_service.hold(
            db,
            issuer_id,
            110,
            f"lab_task:{task_id}",
            terminalization_version=terminalization_version,
        )
        assert hold_id is not None
        db.add_all(
            [
                LabTask(
                    id=task_id,
                    issuer_user_id=issuer_id,
                    researcher_slug=resident_slug,
                    title=f"{prefix} title",
                    brief_md="terminalizer worker regression",
                    scopes_json=["web_search"],
                    reward_sc=100,
                    platform_fee_sc=10,
                    status=task_status,
                    hold_id=hold_id,
                    accepted_run_id=run_id,
                ),
                LabRun(
                    id=run_id,
                    task_id=task_id,
                    researcher_slug=resident_slug,
                    status=run_status,
                ),
                LabRunLease(
                    run_id=run_id,
                    owner_id=f"{prefix}-owner",
                    fencing_epoch=0,
                    heartbeat_at=now,
                    expires_at=now + timedelta(minutes=5),
                ),
            ]
        )
        await db.commit()

    actor = issuer_id if operation != "fail" else f"runner:{run_id}"
    async with factory() as db:
        task = await db.get(LabTask, task_id)
        assert task is not None
        command = await lab_terminalization_service.submit_command(
            db,
            task=task,
            operation=operation,
            actor=actor,
        )
        return command.command_id


async def _command_state(
    factory: async_sessionmaker[AsyncSession], command_id: str
) -> LabTerminalizationCommand:
    async with factory() as db:
        command = await db.get(LabTerminalizationCommand, command_id)
        assert command is not None
        return command


@pytest.mark.anyio
async def test_v1_pending_command_recovers_even_while_v2_gate_is_closed(factory, monkeypatch):
    from app.lab import terminalizer

    command_id = await _seed_command(
        factory,
        prefix="legacy-recovery",
        terminalization_version="v1",
    )
    monkeypatch.setattr(settings, "lab_terminalizer_v2_enabled", False, raising=False)

    stats = await terminalizer.process_pending_commands(factory)
    command = await _command_state(factory, command_id)

    assert stats["completed"] == 1
    assert stats["deferred"] == 0
    assert command.status == "completed"
    assert command.attempts == 0


@pytest.mark.anyio
async def test_failure_record_does_not_overwrite_completed_command(factory, monkeypatch):
    from app.lab import terminalizer

    command_id = await _seed_command(
        factory,
        prefix="completed-race",
        terminalization_version="v1",
    )
    monkeypatch.setattr(settings, "lab_terminalizer_v2_enabled", False, raising=False)
    assert (await terminalizer.process_pending_commands(factory))["completed"] == 1

    before = await _command_state(factory, command_id)
    completed_at = before.completed_at
    outcome = await terminalizer._record_failure(
        factory,
        command_id=command_id,
        task_id="completed-race-task",
        run_id="completed-race-run",
        exc=RuntimeError("stale concurrent failure"),
    )
    after = await _command_state(factory, command_id)

    assert outcome == "ignored"
    assert after.status == "completed"
    assert after.attempts == 0
    assert after.last_error is None
    assert after.completed_at == completed_at


@pytest.mark.anyio
async def test_v1_inline_terminalization_delivers_event_with_worker_off(
    factory, monkeypatch
):
    from app.lab import terminalizer

    command_id = await _seed_command(
        factory,
        prefix="legacy-inline",
        terminalization_version="v1",
    )
    monkeypatch.setattr(settings, "lab_terminalizer_v2_enabled", False, raising=False)
    monkeypatch.setattr(settings, "lab_terminalizer_worker_enabled", False, raising=False)
    send = AsyncMock()
    monkeypatch.setattr(terminalizer, "_send_notification", send)

    async with factory() as db:
        task = await db.get(LabTask, "legacy-inline-task")
        assert task is not None
        command = await lab_terminalization_service.submit_for_caller(
            db,
            task=task,
            operation="accept",
            actor="legacy-inline-issuer",
        )
        assert command.command_id == command_id

    async with factory() as db:
        command = await db.get(LabTerminalizationCommand, command_id)
        event = (
            await db.execute(
                select(OutboxEvent).where(
                    OutboxEvent.topic
                    == lab_terminalization_service.TERMINAL_EVENT_TOPIC
                )
            )
        ).scalar_one()
        notifications = (await db.execute(select(Notification))).scalars().all()
        memories = (await db.execute(select(Memory))).scalars().all()
        assert command is not None and command.status == "completed"
        assert event.published_at is not None
        assert len(notifications) == len(memories) == 1
        assert notifications[0].payload_json["event_id"] == event.event_id
        assert memories[0].metadata_json["event_id"] == event.event_id
        assert await terminalizer.publish_terminal_event(
            db, event_id=event.event_id
        ) is False

    assert send.await_count == 1


@pytest.mark.anyio
async def test_v1_terminalization_freezes_multiple_run_anomaly(factory, monkeypatch):
    command_id = await _seed_command(
        factory,
        prefix="legacy-multiple-runs",
        terminalization_version="v1",
    )
    monkeypatch.setattr(settings, "lab_terminalizer_v2_enabled", False, raising=False)

    async with factory() as db:
        db.add(
            LabRun(
                id="legacy-multiple-runs-extra",
                task_id="legacy-multiple-runs-task",
                researcher_slug="legacy-multiple-runs-researcher",
                status="failed",
            )
        )
        await db.commit()

    async with factory() as db:
        task = await db.get(LabTask, "legacy-multiple-runs-task")
        assert task is not None
        with pytest.raises(
            lab_terminalization_service.LabTerminalizationError,
            match="exactly one linked run",
        ):
            await lab_terminalization_service.submit_for_caller(
                db,
                task=task,
                operation="accept",
                actor="legacy-multiple-runs-issuer",
            )

    async with factory() as db:
        task = await db.get(LabTask, "legacy-multiple-runs-task")
        hold = await db.get(CoinHold, task.hold_id)
        command = await db.get(LabTerminalizationCommand, command_id)
        assert task.status == "review"
        assert hold is not None and hold.status == "held"
        assert command is not None and command.status == "pending"


@pytest.mark.anyio
async def test_v2_command_consumes_only_with_dedicated_session_factory(factory, monkeypatch):
    from app.lab import terminalizer

    command_id = await _seed_command(
        factory,
        prefix="v2-routing",
        terminalization_version="v2",
    )

    monkeypatch.setattr(settings, "lab_terminalizer_v2_enabled", False, raising=False)
    closed_gate = await terminalizer.process_pending_commands(
        factory,
        terminalizer_session_factory=None,
    )
    pending = await _command_state(factory, command_id)
    assert closed_gate["completed"] == 0
    assert closed_gate["deferred"] == 1
    assert pending.status == "pending"

    monkeypatch.setattr(settings, "lab_terminalizer_v2_enabled", True, raising=False)
    with pytest.raises(RuntimeError, match="dedicated terminalizer session factory"):
        await terminalizer.process_pending_commands(
            factory,
            terminalizer_session_factory=None,
        )

    with_dsn = await terminalizer.process_pending_commands(
        factory,
        terminalizer_session_factory=factory,
    )
    completed = await _command_state(factory, command_id)
    assert with_dsn["completed"] == 1
    assert with_dsn["deferred"] == 0
    assert completed.status == "completed"


@pytest.mark.anyio
async def test_closed_v2_gate_prioritizes_v1_recovery_beyond_batch_limit(
    factory, monkeypatch
):
    from app.lab import terminalizer

    v2_commands = [
        await _seed_command(
            factory,
            prefix=f"deferred-v2-{index}",
            terminalization_version="v2",
        )
        for index in range(2)
    ]
    v1_command = await _seed_command(
        factory,
        prefix="recover-v1-after-v2-backlog",
        terminalization_version="v1",
    )
    monkeypatch.setattr(settings, "lab_terminalizer_v2_enabled", False, raising=False)

    stats = await terminalizer.process_pending_commands(factory, limit=2)

    assert stats["completed"] == 1
    assert stats["deferred"] == 1
    assert (await _command_state(factory, v1_command)).status == "completed"
    assert [
        (await _command_state(factory, command_id)).status
        for command_id in v2_commands
    ] == ["pending", "pending"]


@pytest.mark.anyio
async def test_v2_submit_for_caller_requires_ready_consumer(factory, monkeypatch):
    command_id = await _seed_command(
        factory,
        prefix="v2-producer-gate",
        terminalization_version="v2",
    )

    monkeypatch.setattr(settings, "lab_terminalizer_v2_enabled", False, raising=False)
    monkeypatch.setattr(settings, "lab_terminalizer_worker_enabled", False, raising=False)
    monkeypatch.setattr(settings, "lab_terminalizer_database_url", "", raising=False)
    async with factory() as db:
        task = await db.get(LabTask, "v2-producer-gate-task")
        assert task is not None
        with pytest.raises(
            lab_terminalization_service.LabTerminalizationError,
            match="consumer is not ready",
        ):
            await lab_terminalization_service.submit_for_caller(
                db,
                task=task,
                operation="accept",
                actor="v2-producer-gate-issuer",
            )

    pending = await _command_state(factory, command_id)
    assert pending.status == "pending"

    monkeypatch.setattr(settings, "lab_terminalizer_v2_enabled", True, raising=False)
    monkeypatch.setattr(settings, "lab_terminalizer_worker_enabled", True, raising=False)
    monkeypatch.setattr(
        settings,
        "lab_terminalizer_database_url",
        "sqlite+aiosqlite:///dedicated-terminalizer.db",
        raising=False,
    )
    async with factory() as db:
        task = await db.get(LabTask, "v2-producer-gate-task")
        assert task is not None
        command = await lab_terminalization_service.submit_for_caller(
            db,
            task=task,
            operation="accept",
            actor="v2-producer-gate-issuer",
        )
    assert command.command_id == command_id


@pytest.mark.anyio
async def test_v2_submit_for_caller_never_reports_failed_command_as_accepted(
    factory, monkeypatch
):
    command_id = await _seed_command(
        factory,
        prefix="v2-failed-command",
        terminalization_version="v2",
    )
    monkeypatch.setattr(settings, "lab_terminalizer_v2_enabled", True, raising=False)
    monkeypatch.setattr(settings, "lab_terminalizer_worker_enabled", True, raising=False)
    monkeypatch.setattr(
        settings,
        "lab_terminalizer_database_url",
        "sqlite+aiosqlite:///dedicated-terminalizer.db",
        raising=False,
    )
    async with factory() as db:
        command = await db.get(LabTerminalizationCommand, command_id)
        assert command is not None
        command.status = "failed"
        command.last_error = "injected_nonretryable_failure"
        await db.commit()

    async with factory() as db:
        task = await db.get(LabTask, "v2-failed-command-task")
        assert task is not None
        with pytest.raises(
            lab_terminalization_service.LabTerminalizationError,
            match="requires reconciliation",
        ):
            await lab_terminalization_service.submit_for_caller(
                db,
                task=task,
                operation="accept",
                actor="v2-failed-command-issuer",
            )


@pytest.mark.anyio
async def test_nonretryable_invalid_command_fails_without_poisoning_following_work(
    factory, monkeypatch
):
    from app.lab import telemetry, terminalizer

    bad_command_id = await _seed_command(
        factory,
        prefix="invalid-first",
        terminalization_version="v1",
    )
    good_command_id = await _seed_command(
        factory,
        prefix="valid-second",
        terminalization_version="v1",
    )

    async with factory() as db:
        command = await db.get(LabTerminalizationCommand, bad_command_id)
        assert command is not None
        payload = dict(command.payload_json or {})
        payload["target_status"] = "tampered"
        command.payload_json = payload
        await db.commit()

    seen_alerts: list[tuple[object, dict]] = []
    monkeypatch.setattr(
        telemetry,
        "emit_alert",
        lambda alert, **fields: seen_alerts.append((alert, fields)) or {},
    )

    stats = await terminalizer.process_pending_commands(factory)
    bad = await _command_state(factory, bad_command_id)
    good = await _command_state(factory, good_command_id)

    assert stats["failed"] == 1
    assert stats["completed"] == 1
    assert bad.status == "failed"
    assert bad.attempts == 1
    assert bad.last_error == "terminalization_target_state_changed"
    assert "invalid-first title" not in (bad.last_error or "")
    assert good.status == "completed"
    assert seen_alerts == [
        (
            telemetry.LabAlert.TERMINALIZATION_FAILED,
            {
                "command_id": bad.command_id,
                "run_id": "invalid-first-run",
                "task_id": "invalid-first-task",
                "reason": "terminalization_target_state_changed",
                "count": 1,
            },
        )
    ]


@pytest.mark.anyio
async def test_retryable_error_retries_over_three_passes_then_fails(factory, monkeypatch):
    from app.lab import telemetry, terminalizer

    command_id = await _seed_command(
        factory,
        prefix="retry-me",
        terminalization_version="v1",
    )

    class DeadlockError(Exception):
        sqlstate = "40P01"

    async def always_deadlock(*_args, **_kwargs):
        raise DBAPIError(None, None, DeadlockError(), False)

    monkeypatch.setattr(
        lab_terminalization_service,
        "finalize_legacy",
        always_deadlock,
    )
    seen_alerts: list[tuple[object, dict]] = []
    monkeypatch.setattr(
        telemetry,
        "emit_alert",
        lambda alert, **fields: seen_alerts.append((alert, fields)) or {},
    )

    first = await terminalizer.process_pending_commands(factory)
    second = await terminalizer.process_pending_commands(factory)
    third = await terminalizer.process_pending_commands(factory)
    command = await _command_state(factory, command_id)

    assert first["retried"] == 1
    assert second["retried"] == 1
    assert third["failed"] == 1
    assert command.status == "failed"
    assert command.attempts == 3
    assert command.last_error == "deadlock_error"
    assert seen_alerts == [
        (
            telemetry.LabAlert.TERMINALIZATION_FAILED,
            {
                "command_id": command_id,
                "run_id": "retry-me-run",
                "task_id": "retry-me-task",
                "reason": "deadlock_error",
                "count": 3,
            },
        )
    ]


@pytest.mark.anyio
async def test_terminal_event_publish_is_durable_and_idempotent(factory, monkeypatch):
    from app.lab import terminalizer

    command_id = await _seed_command(
        factory,
        prefix="publisher",
        terminalization_version="v1",
    )
    monkeypatch.setattr(settings, "lab_terminalizer_v2_enabled", False, raising=False)
    monkeypatch.setattr(
        "app.ws.manager.manager.is_online",
        AsyncMock(return_value=True),
    )
    send = AsyncMock()
    monkeypatch.setattr("app.ws.manager.manager.send", send)

    processed = await terminalizer.process_pending_commands(factory)
    first_publish = await terminalizer.publish_terminal_events(factory)
    second_publish = await terminalizer.publish_terminal_events(factory)

    async with factory() as db:
        command = await db.get(LabTerminalizationCommand, command_id)
        notifications = (
            await db.execute(
                select(Notification).where(Notification.user_id == "publisher-issuer")
            )
        ).scalars().all()
        memories = (
            await db.execute(
                select(Memory).where(Memory.source == "lab_task")
            )
        ).scalars().all()
        outbox = (
            await db.execute(
            select(OutboxEvent).where(
                OutboxEvent.topic
                == lab_terminalization_service.TERMINAL_EVENT_TOPIC
            )
            )
        ).scalars().all()

    assert processed["completed"] == 1
    assert first_publish["published"] == 1
    assert second_publish["published"] == 0
    assert command is not None and command.status == "completed"
    assert len(notifications) == 1
    assert notifications[0].payload_json["event_id"] == outbox[0].event_id
    assert len(memories) == 1
    assert memories[0].metadata_json["event_id"] == outbox[0].event_id
    assert outbox[0].dispatch_status == "published"
    assert outbox[0].published_at is not None
    assert send.await_count == 1


@pytest.mark.anyio
async def test_build_runner_service_uses_separate_terminalizer_dsn(tmp_path, monkeypatch):
    from app.lab import main as lab_main

    app_db = Path(tmp_path) / "app.db"
    term_db = Path(tmp_path) / "terminalizer.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{app_db}", raising=False)
    monkeypatch.setattr(settings, "lab_terminalizer_worker_enabled", False, raising=False)
    monkeypatch.setattr(settings, "lab_terminalizer_v2_enabled", False, raising=False)
    monkeypatch.setattr(settings, "lab_terminalizer_database_url", "", raising=False)
    monkeypatch.setattr(settings, "lab_outbox_v2_enabled", False, raising=False)

    disabled = lab_main.build_runner_service()
    assert disabled.terminalizer_loop is not None
    assert disabled.terminalizer_session_factory is None
    assert disabled.dispatcher_loop is None
    await disabled.aclose()

    monkeypatch.setattr(settings, "lab_terminalizer_v2_enabled", True, raising=False)
    with pytest.raises(RuntimeError, match="worker_enabled"):
        lab_main.build_runner_service()

    monkeypatch.setattr(settings, "lab_terminalizer_worker_enabled", True, raising=False)
    with pytest.raises(RuntimeError, match="database_url"):
        lab_main.build_runner_service()

    monkeypatch.setattr(
        settings,
        "lab_terminalizer_database_url",
        f"sqlite+aiosqlite:///{term_db}",
        raising=False,
    )

    enabled = lab_main.build_runner_service()
    assert enabled.terminalizer_loop is not None
    assert enabled.terminalizer_session_factory is not None
    assert enabled.terminalizer_session_factory is not enabled.session_factory
    assert enabled.dispatcher_loop is None
    await enabled.aclose()

    monkeypatch.setattr(settings, "lab_outbox_v2_enabled", True, raising=False)
    outbox_enabled = lab_main.build_runner_service()
    assert outbox_enabled.dispatcher_loop is not None
    await outbox_enabled.aclose()


@pytest.mark.anyio
async def test_runner_service_starts_terminalizer_loop_with_dedicated_factory():
    from app.lab.main import RunnerService

    stop = asyncio.Event()
    started = asyncio.Event()
    captured: dict[str, object] = {}

    async def standby():
        await stop.wait()

    async def dispatch_loop(session_factory, *, publishers, owned_topics, stop_event):
        await stop_event.wait()

    async def terminalizer_loop(
        session_factory, *, terminalizer_session_factory, stop_event
    ):
        captured["session_factory"] = session_factory
        captured["terminalizer_session_factory"] = terminalizer_session_factory
        started.set()
        await stop_event.wait()

    app_factory = object()
    dedicated_factory = object()
    service = RunnerService(
        session_factory=app_factory,
        terminalizer_session_factory=dedicated_factory,
        runner_loop=standby,
        world_reload_loop=standby,
        dispatcher_loop=dispatch_loop,
        terminalizer_loop=terminalizer_loop,
    )

    running = asyncio.create_task(service.run(stop_event=stop))
    await asyncio.wait_for(started.wait(), timeout=1)
    await service.wait_ready(timeout=1)

    assert captured == {
        "session_factory": app_factory,
        "terminalizer_session_factory": dedicated_factory,
    }
    stop.set()
    await asyncio.wait_for(running, timeout=1)
