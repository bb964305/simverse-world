"""T4 — canonical event ledger + transactional outbox (PRD §Data and API
Evolution, V05 ledger + V08 crash windows).

Every ``append_event`` writes the canonical ``LabRunEvent`` + its ``OutboxEvent``
row (+ optional ``LabRunStep`` compatibility projection) in ONE transaction:
a crash between them is impossible, proven by the rollback (crash-window A)
test leaving neither row behind. ``seq`` is gap-free and the unique
(run_id, seq) constraint is the release-blocking backstop; a collision surfaces
as ``SequenceConflict`` with nothing half-written. The outbox is a durable,
monotonic (autoincrement id) cursor: an un-published row survives a "restart"
(crash-window B) and is replayable until stamped ``published_at``.
"""
import uuid
from datetime import datetime, UTC

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.lab import ledger
from app.lab.protocol import RunEventEnvelope
from app.models.lab_event import LabRunEvent, OutboxEvent
from app.models.lab_run import LabRun, LabRunStep
from app.models.lab_task import LabTask


@pytest.fixture
async def lab_env(db_engine):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add(LabTask(id="task1", issuer_user_id="issuer", title="t"))
        s.add(LabRun(id="run1", task_id="task1", researcher_slug="sage"))
        await s.commit()
    yield factory


def _env(**over):
    base = dict(
        event_id=str(uuid.uuid4()), tenant_id="issuer", run_id="run1",
        task_id="task1", seq=1, type="tool.requested", actor="agent-1",
        fencing_epoch=0, policy_version="lab-policy-v1",
        occurred_at=datetime.now(UTC),
    )
    base.update(over)
    return RunEventEnvelope(**base)


# ── 1. one append → one event + one outbox row, seq=1, full envelope in outbox ──
@pytest.mark.anyio
async def test_append_writes_event_and_outbox(lab_env):
    factory = lab_env
    env = _env(seq=1)
    async with factory() as s:
        event = await ledger.append_event(s, envelope=env, outbox_topic="lab.run")
        assert event is not None and event.seq == 1
    async with factory() as s:
        events = (await s.execute(select(LabRunEvent))).scalars().all()
        outbox = (await s.execute(select(OutboxEvent))).scalars().all()
        assert len(events) == 1 and len(outbox) == 1
        assert events[0].event_id == env.event_id
        ob = outbox[0]
        assert ob.event_id == env.event_id          # canonical + outbox share event_id
        assert ob.topic == "lab.run"
        assert ob.payload_json["run_id"] == "run1"   # full envelope, not just payload
        assert ob.payload_json["seq"] == 1
        assert ob.payload_json["type"] == "tool.requested"
        assert ob.payload_json["event_id"] == env.event_id


# ── 2. sequential appends → gap-free seq 1,2,3; read_events(after_seq) window ──
@pytest.mark.anyio
async def test_sequential_seq_is_gap_free(lab_env):
    factory = lab_env
    async with factory() as s:
        for _ in range(3):
            seq = await ledger.next_seq(s, "run1")
            await ledger.append_event(s, envelope=_env(seq=seq))
        allev = await ledger.read_events(s, run_id="run1")
        assert [e.seq for e in allev] == [1, 2, 3]
        after = await ledger.read_events(s, run_id="run1", after_seq=1)
        assert [e.seq for e in after] == [2, 3]


# ── 3. duplicate provider_event_id → second returns None, one canonical row ──
@pytest.mark.anyio
async def test_provider_event_id_dedup(lab_env):
    factory = lab_env
    async with factory() as s:
        first = await ledger.append_event(s, envelope=_env(seq=1), provider_event_id="p1")
        assert first is not None
    async with factory() as s:
        dup = await ledger.append_event(s, envelope=_env(seq=2), provider_event_id="p1")
        assert dup is None
    async with factory() as s:
        rows = (await s.execute(
            select(LabRunEvent).where(LabRunEvent.provider_event_id == "p1")
        )).scalars().all()
        assert len(rows) == 1
        total = (await s.execute(select(func.count()).select_from(LabRunEvent))).scalar_one()
        assert total == 1


# ── 4. crash window A: (run_id,seq) collision → SequenceConflict, no half rows ──
@pytest.mark.anyio
async def test_seq_conflict_rolls_back_both_rows(lab_env):
    factory = lab_env
    # A committed placeholder already occupies (run1, seq=1).
    async with factory() as s:
        s.add(LabRunEvent(
            event_id="placeholder", tenant_id="issuer", run_id="run1",
            task_id="task1", seq=1, type="tool.requested", actor="x",
            policy_version="v", occurred_at=datetime.now(UTC),
        ))
        await s.commit()
    async with factory() as s:
        env = _env(seq=1, event_id="collider")
        with pytest.raises(ledger.SequenceConflict):
            await ledger.append_event(s, envelope=env)
    # The collider's event AND its outbox row are both absent (atomic rollback).
    async with factory() as s:
        ev = (await s.execute(
            select(LabRunEvent).where(LabRunEvent.event_id == "collider")
        )).scalar_one_or_none()
        ob = (await s.execute(
            select(OutboxEvent).where(OutboxEvent.event_id == "collider")
        )).scalar_one_or_none()
        assert ev is None and ob is None
        total = (await s.execute(select(func.count()).select_from(LabRunEvent))).scalar_one()
        assert total == 1  # only the placeholder survives


# ── 5. crash window B: unpublished outbox row is replayable; mark idempotent ──
@pytest.mark.anyio
async def test_outbox_unpublished_replayable_then_marked(lab_env):
    factory = lab_env
    async with factory() as s:
        await ledger.append_event(s, envelope=_env(seq=1))
    # "restart": the row is still there, unpublished → replayable.
    async with factory() as s:
        rows = await ledger.read_outbox(s)
        assert len(rows) == 1 and rows[0].published_at is None
        oid = rows[0].id
    async with factory() as s:
        await ledger.mark_published(s, outbox_ids=[oid])
    async with factory() as s:
        rows = await ledger.read_outbox(s)
        assert [r for r in rows if r.published_at is None] == []
        assert rows[0].published_at is not None
    # Re-marking is idempotent (no error, stays published).
    async with factory() as s:
        await ledger.mark_published(s, outbox_ids=[oid])
        rows = await ledger.read_outbox(s)
        assert [r for r in rows if r.published_at is None] == []


# ── 6. compatibility projection: projecting types land a LabRunStep in-txn ──
@pytest.mark.anyio
async def test_step_projection_in_same_transaction(lab_env):
    factory = lab_env
    async with factory() as s:
        await ledger.append_event(s, envelope=_env(
            seq=1, type="tool.started",
            payload={"tool": "web.search", "summary": "searching"}))
        await ledger.append_event(s, envelope=_env(
            seq=2, type="tool.completed",
            payload={"tool": "web.search", "summary": "got results"}))
    async with factory() as s:
        steps = (await s.execute(
            select(LabRunStep).where(LabRunStep.run_id == "run1").order_by(LabRunStep.seq)
        )).scalars().all()
        assert [(st.phase, st.seq) for st in steps] == [("tool_call", 1), ("observation", 2)]
        assert steps[0].tool == "web.search"
    # A non-projecting type writes no step.
    assert ledger.project_step(_env(type="approval.requested")) is None
    async with factory() as s:
        await ledger.append_event(s, envelope=_env(seq=3, type="approval.requested"))
    async with factory() as s:
        steps = (await s.execute(
            select(LabRunStep).where(LabRunStep.run_id == "run1")
        )).scalars().all()
        assert len(steps) == 2  # unchanged — approval.requested did not project


# ── 7. outbox cursor is a monotonic, gap-free autoincrement stream ──
@pytest.mark.anyio
async def test_outbox_cursor_is_monotonic(lab_env):
    factory = lab_env
    async with factory() as s:
        for i in range(5):
            await ledger.append_event(s, envelope=_env(seq=i + 1))
    async with factory() as s:
        rows = await ledger.read_outbox(s)
        ids = [r.id for r in rows]
        assert ids == sorted(ids)
        assert ids == list(range(ids[0], ids[0] + 5))  # strictly +1, no gaps
        # Paged cursor read visits every id exactly once, in order.
        seen, cursor = [], 0
        while True:
            batch = await ledger.read_outbox(s, after_id=cursor, limit=2)
            if not batch:
                break
            seen.extend(r.id for r in batch)
            cursor = batch[-1].id
        assert seen == ids
