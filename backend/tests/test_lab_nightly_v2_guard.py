"""Legacy orphan recovery must not terminalize protocol-v2 runs."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.lab_run import LabRun
from app.models.lab_task import LabTask
from app.services import lab_task_service
from app.tasks import nightly_cron


@pytest.mark.anyio
async def test_orphan_sweep_leaves_protocol_v2_for_durable_recovery(
    db_engine, monkeypatch
):
    session_factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )
    stale = datetime.now(UTC) - timedelta(
        seconds=settings.lab_run_heartbeat_ttl_s + 10
    )
    async with session_factory() as db:
        db.add_all(
            [
                LabTask(
                    id="legacy-orphan-task",
                    issuer_user_id="tenant",
                    researcher_slug="sage",
                    title="legacy orphan",
                    status="running",
                    accepted_run_id="legacy-orphan-run",
                ),
                LabRun(
                    id="legacy-orphan-run",
                    task_id="legacy-orphan-task",
                    researcher_slug="sage",
                    protocol_version=1,
                    status="running",
                    heartbeat_at=stale,
                ),
                LabTask(
                    id="v2-orphan-task",
                    issuer_user_id="tenant",
                    researcher_slug="sage",
                    title="v2 recoverable run",
                    status="running",
                    accepted_run_id="v2-orphan-run",
                ),
                LabRun(
                    id="v2-orphan-run",
                    task_id="v2-orphan-task",
                    researcher_slug="sage",
                    adapter="simverse_ref",
                    protocol_version=2,
                    status="running",
                    heartbeat_at=stale,
                ),
            ]
        )
        await db.commit()

    failed_tasks: list[str] = []

    async def record_failure(db, task, reason=""):
        failed_tasks.append(task.id)

    monkeypatch.setattr(nightly_cron, "async_session", session_factory)
    monkeypatch.setattr(lab_task_service, "fail_task", record_failure)

    assert await nightly_cron.sweep_orphan_lab_runs() == 1

    async with session_factory() as db:
        legacy = await db.get(LabRun, "legacy-orphan-run")
        recoverable = await db.get(LabRun, "v2-orphan-run")
        assert legacy.status == "failed"
        assert recoverable.status == "running"
    assert failed_tasks == ["legacy-orphan-task"]


@pytest.mark.anyio
async def test_codex_orphan_is_fenced_and_metered_before_refund(
    db_engine, monkeypatch
):
    from app.lab.sandbox.codex import CodexAdapter

    session_factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )
    stale = datetime.now(UTC) - timedelta(
        seconds=settings.lab_run_heartbeat_ttl_s + 10
    )
    async with session_factory() as db:
        db.add_all([
            LabTask(
                id="codex-orphan-task",
                issuer_user_id="tenant",
                researcher_slug="sage",
                title="metered orphan",
                status="running",
                accepted_run_id="codex-orphan-run",
            ),
            LabRun(
                id="codex-orphan-run",
                task_id="codex-orphan-task",
                researcher_slug="sage",
                adapter="codex",
                protocol_version=1,
                status="running",
                heartbeat_at=stale,
            ),
        ])
        await db.commit()

    collect = AsyncMock(return_value=6)
    failed_costs: list[int] = []

    async def record_failure(db, task, reason=""):
        run = await db.get(LabRun, task.accepted_run_id)
        failed_costs.append(run.cost_usd_cents)

    monkeypatch.setattr(nightly_cron, "async_session", session_factory)
    monkeypatch.setattr(lab_task_service, "fail_task", record_failure)
    monkeypatch.setattr(CodexAdapter, "cancel_and_collect_usage", collect)

    assert await nightly_cron.sweep_orphan_lab_runs() == 1
    collect.assert_awaited_once_with("codex-orphan-run")
    assert failed_costs == [6]


@pytest.mark.anyio
async def test_codex_orphan_with_unknown_usage_blocks_refund(
    db_engine, monkeypatch
):
    from app.lab.sandbox.codex import CodexAdapter

    session_factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )
    stale = datetime.now(UTC) - timedelta(
        seconds=settings.lab_run_heartbeat_ttl_s + 10
    )
    async with session_factory() as db:
        db.add_all([
            LabTask(
                id="unknown-orphan-task",
                issuer_user_id="tenant",
                researcher_slug="sage",
                title="unknown orphan",
                status="running",
                accepted_run_id="unknown-orphan-run",
            ),
            LabRun(
                id="unknown-orphan-run",
                task_id="unknown-orphan-task",
                researcher_slug="sage",
                adapter="codex",
                protocol_version=1,
                status="running",
                heartbeat_at=stale,
            ),
        ])
        await db.commit()

    fail_task = AsyncMock()
    monkeypatch.setattr(nightly_cron, "async_session", session_factory)
    monkeypatch.setattr(lab_task_service, "fail_task", fail_task)
    monkeypatch.setattr(
        CodexAdapter,
        "cancel_and_collect_usage",
        AsyncMock(side_effect=RuntimeError("gateway unavailable")),
    )

    assert await nightly_cron.sweep_orphan_lab_runs() == 1
    fail_task.assert_not_awaited()
    async with session_factory() as db:
        run = await db.get(LabRun, "unknown-orphan-run")
        task = await db.get(LabTask, "unknown-orphan-task")
        assert run.status == "failed"
        assert run.error.startswith("cost_unknown: orphaned")
        assert task.status == "running"
