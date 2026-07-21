"""Protocol-v2 admission and Runner capability fail-fast regressions."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.coin_hold import CoinHold
from app.models.lab_event import OutboxEvent
from app.models.lab_run import LabRun
from app.models.lab_task import LabTask
from app.models.resident import Resident
from app.models.user import User
from app.services import coin_service
from app.services import lab_task_service as service


@pytest.fixture
def factory(db_engine, monkeypatch):
    for name, value in {
        "lab_enabled": True,
        "lab_agent_v2_enabled": True,
        "lab_terminalizer_v2_enabled": False,
        "lab_adapter": "mock",
        "lab_platform_fee_rate": 0.1,
        "lab_default_budget_usd": 0.5,
        "lab_sc_per_usd": 100,
        "lab_daily_tasks_per_user": 20,
    }.items():
        monkeypatch.setattr(settings, name, value, raising=False)

    session_factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )
    return session_factory


async def _seed(factory) -> None:
    async with factory() as db:
        db.add(User(
            id="issuer", name="Issuer", email="issuer@consumer.test",
            soul_coin_balance=1000,
        ))
        db.add(Resident(
            slug="sage", name="Sage", creator_id="system",
            resident_type="npc", meta_json={"lab": {"access": True}},
        ))
        await db.commit()


def _without_v2_handler(monkeypatch):
    from app.lab import runner

    monkeypatch.setattr(
        runner,
        "_PROTOCOL_HANDLERS",
        {
            version: handler
            for version, handler in runner._PROTOCOL_HANDLERS.items()
            if version != 2
        },
    )
    return runner


@pytest.mark.anyio
async def test_v2_task_creation_fails_before_domain_or_queue_side_effect(
    factory, monkeypatch
):
    runner = _without_v2_handler(monkeypatch)
    await _seed(factory)
    enqueue = AsyncMock()

    with patch("app.lab.queue.enqueue_run", enqueue), patch(
        "app.services.lab_task_service.emit", new_callable=AsyncMock
    ):
        async with factory() as db:
            with pytest.raises(
                service.LabTaskError,
                match="protocol_version 2 consumer is not ready",
            ):
                await service.create_task(
                    db,
                    issuer_id="issuer",
                    title="v2 requires a real consumer",
                    brief="fail before task funding",
                    scopes=["web_search"],
                    reward_sc=100,
                    researcher_slug="sage",
                )

    assert 2 not in runner._PROTOCOL_HANDLERS
    enqueue.assert_not_awaited()
    async with factory() as db:
        for model in (LabTask, CoinHold, LabRun, OutboxEvent):
            count = (await db.execute(select(func.count()).select_from(model))).scalar()
            assert count == 0
        assert await coin_service.get_balance(db, "issuer") == 1000


@pytest.mark.anyio
async def test_runner_service_rejects_v2_before_starting_any_child(
    monkeypatch,
):
    runner = _without_v2_handler(monkeypatch)
    from app.lab.main import RunnerService

    run_loop = AsyncMock()
    world_loop = AsyncMock()
    dispatcher_loop = AsyncMock()
    terminalizer_loop = AsyncMock()
    instance = RunnerService(
        session_factory=object(),
        protocol_version=2,
        runner_loop=run_loop,
        world_reload_loop=world_loop,
        dispatcher_loop=dispatcher_loop,
        terminalizer_loop=terminalizer_loop,
    )

    with pytest.raises(
        runner.ProtocolConsumerUnavailable,
        match="protocol_version 2 consumer is not ready",
    ):
        await instance.run(stop_event=asyncio.Event())

    assert instance.ready is False
    run_loop.assert_not_awaited()
    world_loop.assert_not_awaited()
    dispatcher_loop.assert_not_awaited()
    terminalizer_loop.assert_not_awaited()


@pytest.mark.anyio
async def test_registered_v2_handler_still_rejects_mock_before_any_side_effect(
    factory, monkeypatch
):
    runner = _without_v2_handler(monkeypatch)
    from app.lab import main as lab_main

    runner.register_protocol_handler(2, AsyncMock())
    run_loop = AsyncMock()
    world_loop = AsyncMock()
    instance = lab_main.RunnerService(
        protocol_version=2,
        runner_loop=run_loop,
        world_reload_loop=world_loop,
    )

    with pytest.raises(
        runner.ProtocolConsumerUnavailable,
        match="requires lab_adapter='simverse_ref'",
    ):
        lab_main.build_runner_service()
    with pytest.raises(
        runner.ProtocolConsumerUnavailable,
        match="requires lab_adapter='simverse_ref'",
    ):
        await instance.run(stop_event=asyncio.Event())
    run_loop.assert_not_awaited()
    world_loop.assert_not_awaited()

    await _seed(factory)
    enqueue = AsyncMock()
    with patch("app.lab.queue.enqueue_run", enqueue):
        async with factory() as db:
            with pytest.raises(
                service.LabTaskError,
                match="requires lab_adapter='simverse_ref'",
            ):
                await service.create_task(
                    db,
                    issuer_id="issuer",
                    title="mock is not a v2 runtime",
                    brief="reject even though a handler was registered",
                    scopes=["web_search"],
                    reward_sc=100,
                    researcher_slug="sage",
                )

    enqueue.assert_not_awaited()
    async with factory() as db:
        for model in (LabTask, CoinHold, LabRun, OutboxEvent):
            count = (await db.execute(select(func.count()).select_from(model))).scalar()
            assert count == 0
        assert await coin_service.get_balance(db, "issuer") == 1000


@pytest.mark.anyio
async def test_registered_v2_handler_is_the_only_v2_execution_path(
    factory, monkeypatch
):
    runner = _without_v2_handler(monkeypatch)
    import app.database as database

    monkeypatch.setattr(database, "async_session", factory)
    monkeypatch.setattr(runner, "async_session", factory)
    monkeypatch.setattr(settings, "lab_adapter", "simverse_ref", raising=False)
    v2_handler = AsyncMock()
    runner.register_protocol_handler(2, v2_handler)

    async with factory() as db:
        db.add(LabRun(
            id="run-v2", task_id="task-v2", researcher_slug="sage",
            adapter="simverse_ref", status="queued", protocol_version=2,
        ))
        await db.commit()

    with patch.object(runner, "run_one", new=AsyncMock()) as v1_handler:
        assert await runner._process_run("run-v2", protocol_version=2) == "ran"

    v2_handler.assert_awaited_once_with("run-v2")
    v1_handler.assert_not_awaited()
