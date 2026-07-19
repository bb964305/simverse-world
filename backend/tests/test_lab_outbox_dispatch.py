"""Phase 2 (recovery plan), gap #11 — the durable outbox finally has a dispatcher.

The outbox is written transactionally with the state it describes, but nothing
drained it in production. These tests pin the claimant/retry/topic-router:
success marks published exactly once, an unknown topic is quarantined (never
published), and a failing sink retries with backoff then dead-letters.
"""
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.lab import outbox_dispatcher as disp
from app.models.lab_event import OutboxEvent


@pytest.fixture
def factory(db_engine):
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


async def _add(factory, *, event_id, topic, payload=None) -> int:
    async with factory() as db:
        row = OutboxEvent(event_id=event_id, tenant_id="t1", topic=topic,
                          payload_json=payload or {"hello": "world"})
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


@pytest.mark.anyio
async def test_dispatch_publishes_and_marks_once(factory):
    oid = await _add(factory, event_id="e1", topic="lab_run_event")
    seen = []

    async def pub(payload):
        seen.append(payload)

    async with factory() as db:
        stats = await disp.dispatch_once(db, publishers={"lab_run_event": pub})
    assert stats["published"] == 1
    assert seen == [{"hello": "world"}]

    async with factory() as db:
        row = await db.get(OutboxEvent, oid)
        assert row.dispatch_status == "published" and row.published_at is not None

    # Idempotent: a second pass does not re-publish an already-published row.
    async with factory() as db:
        stats2 = await disp.dispatch_once(db, publishers={"lab_run_event": pub})
    assert stats2["published"] == 0
    assert len(seen) == 1


@pytest.mark.anyio
async def test_unknown_topic_quarantined_never_published(factory):
    oid = await _add(factory, event_id="e2", topic="mystery_topic")

    async def pub(payload):  # should never be called for the unknown topic
        raise AssertionError("publisher called for unknown topic")

    async with factory() as db:
        stats = await disp.dispatch_once(db, publishers={"lab_run_event": pub})
    assert stats["quarantined"] == 1 and stats["published"] == 0

    async with factory() as db:
        row = await db.get(OutboxEvent, oid)
        assert row.dispatch_status == "dead"
        assert row.published_at is None  # never marked delivered
        assert row.last_error == "unknown_topic"


@pytest.mark.anyio
async def test_failure_retries_with_backoff_then_dead_letters(factory):
    oid = await _add(factory, event_id="e3", topic="lab_run_event")

    async def boom(payload):
        raise RuntimeError("sink down")

    t = datetime(2026, 1, 1, tzinfo=UTC)
    # First failure: retried, backoff scheduled, not yet dead.
    async with factory() as db:
        s1 = await disp.dispatch_once(db, publishers={"lab_run_event": boom}, max_attempts=3, now=t)
    assert s1["retried"] == 1 and s1["dead"] == 0
    async with factory() as db:
        row = await db.get(OutboxEvent, oid)
        assert row.attempts == 1 and row.published_at is None
        assert row.next_attempt_at is not None and row.locked_until is None

    # A pass BEFORE the backoff elapses must not re-attempt it.
    async with factory() as db:
        s_early = await disp.dispatch_once(db, publishers={"lab_run_event": boom}, max_attempts=3, now=t)
    assert s_early["claimed"] == 0

    # Drive the remaining attempts past the cap → dead-letter, never published.
    later = t + timedelta(hours=1)
    async with factory() as db:
        await disp.dispatch_once(db, publishers={"lab_run_event": boom}, max_attempts=3, now=later)
    async with factory() as db:
        await disp.dispatch_once(db, publishers={"lab_run_event": boom}, max_attempts=3, now=later + timedelta(hours=1))
    async with factory() as db:
        row = await db.get(OutboxEvent, oid)
        assert row.attempts >= 3
        assert row.dispatch_status == "dead"
        assert row.published_at is None
