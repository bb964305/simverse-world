"""agent-worker entrypoint (app/agent/main.py) lifecycle tests."""
import asyncio
from types import SimpleNamespace

import pytest

import app.agent.main as worker


async def test_worker_survives_normal_task_return(monkeypatch):
    """A loop returning normally (e.g. embedding backfill short-circuits on
    EMBEDDING_ENABLED=false) must not take down the whole worker — vm212
    went into a container restart loop this way (2026-07-13)."""
    started = asyncio.Event()

    async def forever():
        started.set()
        await asyncio.Event().wait()

    async def returns_immediately():
        return None

    async def noop():
        return None

    monkeypatch.setattr(worker, "agent_loop", SimpleNamespace(run=forever))
    monkeypatch.setattr(worker, "heat_cron_loop", forever)
    monkeypatch.setattr(worker, "event_cron_loop", forever)
    monkeypatch.setattr(worker, "nightly_cron_loop", forever)
    monkeypatch.setattr(worker, "embedding_backfill_loop", returns_immediately)
    monkeypatch.setattr(worker, "init_sentry", lambda *_: None)
    monkeypatch.setattr(worker, "close_redis", noop)

    task = asyncio.create_task(worker.main())
    await asyncio.wait_for(started.wait(), 2)
    # Give the backfill task's immediate return time to be observed.
    await asyncio.sleep(0.1)
    assert not task.done(), "worker shut down after a task returned normally"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_worker_crashing_task_is_fatal(monkeypatch):
    """A task raising must still bring the worker down (crash-loud policy)."""
    started = asyncio.Event()

    async def forever():
        started.set()
        await asyncio.Event().wait()

    async def crashes():
        raise RuntimeError("boom")

    async def noop():
        return None

    monkeypatch.setattr(worker, "agent_loop", SimpleNamespace(run=forever))
    monkeypatch.setattr(worker, "heat_cron_loop", forever)
    monkeypatch.setattr(worker, "event_cron_loop", forever)
    monkeypatch.setattr(worker, "nightly_cron_loop", crashes)
    monkeypatch.setattr(worker, "embedding_backfill_loop", forever)
    monkeypatch.setattr(worker, "init_sentry", lambda *_: None)
    monkeypatch.setattr(worker, "close_redis", noop)

    with pytest.raises(RuntimeError, match="boom"):
        await asyncio.wait_for(worker.main(), 5)
