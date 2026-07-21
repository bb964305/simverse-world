"""Phase 10 (recovery plan) — content-free SLO snapshot.

collect_snapshot derives queue depth, active/orphan run counts, oldest
unpublished outbox age, and dead-letter count from ground truth — all structural,
no content.
"""
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.lab import slo
from app.lab.queue import queue_keys
from app.models.lab_run import LabRun
from app.models.lab_event import OutboxEvent
from app.redis_client import get_redis


@pytest.mark.anyio
async def test_collect_snapshot_reports_structural_slos(db_engine, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "lab_run_heartbeat_ttl_s", 300, raising=False)
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    async with factory() as db:
        # 2 active runs; one is an orphan candidate (stale heartbeat).
        db.add(LabRun(id="r1", task_id="t1", researcher_slug="s", status="running",
                      heartbeat_at=now - timedelta(seconds=30)))
        db.add(LabRun(id="r2", task_id="t2", researcher_slug="s", status="needs_approval",
                      heartbeat_at=now - timedelta(seconds=999)))  # orphan candidate
        db.add(LabRun(id="r3", task_id="t3", researcher_slug="s", status="succeeded"))  # not active
        # one unpublished outbox row (10 min old) + one dead-lettered
        db.add(OutboxEvent(event_id="o1", tenant_id="t", topic="lab_run_event",
                           created_at=now - timedelta(minutes=10)))
        dead = OutboxEvent(event_id="o2", tenant_id="t", topic="mystery")
        dead.dispatch_status = "dead"
        db.add(dead)
        await db.commit()

    await get_redis().lpush(queue_keys(1)[0], "run-a")
    await get_redis().lpush(queue_keys(2)[0], "run-b")

    async with factory() as db:
        snap = await slo.collect_snapshot(db, now=now)

    assert snap["queue_depth"] == 2
    assert snap["queue_depth_v1"] == 1
    assert snap["queue_depth_v2"] == 1
    assert snap["active_runs"] == 2                    # r1, r2 (r3 terminal excluded)
    assert snap["orphan_candidates"] == 1              # r2 past TTL
    assert snap["dead_letter"] == 1                    # o2
    assert snap["oldest_unpublished_age_s"] == pytest.approx(600.0, abs=1)  # o1 is 10 min old
