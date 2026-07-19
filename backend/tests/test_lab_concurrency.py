"""Phase 4 (recovery plan) — run-concurrency admission is atomically capped (gap #4).

lab_max_concurrent_runs (and a per-researcher cap) are enforced across all Runner
processes via atomic Redis counters — concurrent reservers can never exceed the
configured limit, a released slot is reusable, and a leaked counter is healed by
reconcile() re-syncing to the DB's true active-run count.
"""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.lab import concurrency
from app.models.lab_run import LabRun
from app.redis_client import get_redis


@pytest.fixture(autouse=True)
def caps(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "lab_max_concurrent_runs", 2, raising=False)
    monkeypatch.setattr(settings, "lab_max_concurrent_per_researcher", 1, raising=False)


@pytest.mark.anyio
async def test_global_cap_never_exceeded_and_slot_reusable(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "lab_max_concurrent_per_researcher", 0)  # isolate the global cap
    assert await concurrency.try_reserve(researcher_slug="a") is True
    assert await concurrency.try_reserve(researcher_slug="b") is True
    assert await concurrency.try_reserve(researcher_slug="c") is False  # cap=2 reached
    await concurrency.release(researcher_slug="a")
    assert await concurrency.try_reserve(researcher_slug="d") is True  # freed slot reused


@pytest.mark.anyio
async def test_per_researcher_cap(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "lab_max_concurrent_runs", 10)
    assert await concurrency.try_reserve(researcher_slug="sage") is True
    assert await concurrency.try_reserve(researcher_slug="sage") is False  # per-researcher cap=1
    # a refused per-researcher reserve must release the global it briefly took
    assert await concurrency.try_reserve(researcher_slug="other") is True
    await concurrency.release(researcher_slug="sage")
    assert await concurrency.try_reserve(researcher_slug="sage") is True


@pytest.mark.anyio
async def test_reconcile_resyncs_to_active_runs(db_engine):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        db.add(LabRun(id="r1", task_id="t1", researcher_slug="sage", status="running"))
        db.add(LabRun(id="r2", task_id="t2", researcher_slug="sage", status="needs_approval"))
        db.add(LabRun(id="r3", task_id="t3", researcher_slug="bob", status="running"))
        db.add(LabRun(id="r4", task_id="t4", researcher_slug="sage", status="queued"))     # not a slot
        db.add(LabRun(id="r5", task_id="t5", researcher_slug="sage", status="succeeded"))  # terminal
        await db.commit()
    r = get_redis()
    await r.set(concurrency.GLOBAL_KEY, 99)  # leak
    async with factory() as db:
        stats = await concurrency.reconcile(db)
    assert stats["global"] == 3               # r1,r2,r3 count; queued+terminal excluded
    assert int(await r.get(concurrency.GLOBAL_KEY)) == 3
    assert stats["researchers"]["sage"] == 2
    assert stats["researchers"]["bob"] == 1


@pytest.mark.anyio
async def test_process_run_refuses_when_cap_full(db_engine, monkeypatch):
    """_process_run returns 'full' when no slot is available (caller requeues),
    'skipped' for a non-queued run, and reserves+runs otherwise."""
    from app.lab import runner
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    import app.database as database
    monkeypatch.setattr(database, "async_session", factory)

    async with factory() as db:
        db.add(LabRun(id="rq", task_id="tq", researcher_slug="sage", status="queued"))
        db.add(LabRun(id="rc", task_id="tc", researcher_slug="sage", status="cancelled"))
        await db.commit()

    # Occupy the global cap (2) so the next reserve fails.
    monkeypatch.setattr(__import__("app.config", fromlist=["settings"]).settings,
                        "lab_max_concurrent_per_researcher", 0)
    assert await concurrency.try_reserve(researcher_slug="x") is True
    assert await concurrency.try_reserve(researcher_slug="y") is True

    # Cancelled run → skipped (never reserves).
    assert await runner._process_run("rc") == "skipped"
    # Queued run with the cap full → full (caller requeues; no execution).
    assert await runner._process_run("rq") == "full"

    # Free a slot and stub run_one: now it runs and releases.
    await concurrency.release(researcher_slug="x")
    with patch.object(runner, "run_one", new=AsyncMock()) as ran:
        assert await runner._process_run("rq") == "ran"
        ran.assert_awaited_once_with("rq")
    # The slot it took was released.
    assert await concurrency.try_reserve(researcher_slug="z") is True
