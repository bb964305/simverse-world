"""Phase 2 (recovery plan), gap #9 remainder — the run enqueue is durable.

_start_run now writes a ``lab.run.enqueue`` outbox event in the SAME transaction
as the run + accepted-run link, so a crash between that commit and the Redis
LPUSH cannot lose the run: the outbox dispatcher replays the enqueue onto the
work queue (idempotently — the runner's queued-guard skips a run already taken).
"""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.lab import outbox_dispatcher as disp
from app.lab import queue as lab_queue
from app.models.user import User
from app.models.resident import Resident
from app.models.lab_event import OutboxEvent
from app.services import lab_task_service as svc


@pytest.fixture
def lab_env(db_engine, monkeypatch):
    from app.config import settings
    for k, v in {
        "lab_enabled": True, "lab_adapter": "mock", "lab_platform_fee_rate": 0.1,
        "lab_default_budget_usd": 0.5, "lab_sc_per_usd": 100, "lab_daily_tasks_per_user": 20,
    }.items():
        monkeypatch.setattr(settings, k, v, raising=False)
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("app.lab.runner.async_session", factory), \
         patch("app.services.lab_task_service.async_session", factory), \
         patch("app.services.lab_task_service.emit", new_callable=AsyncMock):
        yield factory


@pytest.mark.anyio
async def test_start_run_writes_durable_enqueue_event(lab_env):
    factory = lab_env
    async with factory() as s:
        s.add(User(id="issuer", name="I", email="i@t.com", soul_coin_balance=1000))
        s.add(Resident(slug="sage", name="Sage", creator_id="system", resident_type="npc",
                       meta_json={"lab": {"access": True}}))
        await s.commit()

    async with factory() as s:
        task = await svc.create_task(
            s, issuer_id="issuer", title="任务", brief="...",
            scopes=["web_search"], reward_sc=100, researcher_slug="sage",
        )
        run_id = task.accepted_run_id
    assert run_id is not None

    async with factory() as s:
        ev = (await s.execute(
            select(OutboxEvent).where(OutboxEvent.topic == "lab.run.enqueue", OutboxEvent.run_id == run_id)
        )).scalar_one()
        assert ev.payload_json == {"run_id": run_id}
        assert ev.published_at is None  # durable, awaiting the dispatcher


@pytest.mark.anyio
async def test_dispatcher_replays_enqueue_onto_the_queue(db_engine):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        db.add(OutboxEvent(event_id="e-enq", tenant_id="t1", run_id="rX",
                           topic="lab.run.enqueue", payload_json={"run_id": "rX"}))
        await db.commit()

    async with factory() as db:
        stats = await disp.dispatch_once(db, publishers=disp.default_publishers())
    assert stats["published"] == 1

    # The run is now on the work queue (crash-replayed) and the event is published.
    assert await lab_queue.dequeue_run(timeout=1) == "rX"
    async with factory() as db:
        ev = (await db.execute(select(OutboxEvent).where(OutboxEvent.event_id == "e-enq"))).scalar_one()
        assert ev.dispatch_status == "published" and ev.published_at is not None
