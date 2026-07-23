"""Realism P0-5b: reclaim stuck approved proposals + orphan lab reservations."""
import pytest
from datetime import datetime, timedelta, UTC

from sqlalchemy import select

from app.models.world_change_proposal import WorldChangeProposal
from app.models.lab_run import LabRun
from app.models.lab_budget import LabRunBudget
from app.models.resident_treasury import ResidentTreasury
from app.services.proposal_service import reclaim_stuck_proposals
from app.tasks.nightly_cron import sweep_orphan_lab_reservations


@pytest.mark.anyio
async def test_reclaim_stuck_approved_proposal_refunds(db_session):
    db_session.add(ResidentTreasury(resident_slug="alice", balance_sc=0))
    stuck = WorldChangeProposal(
        kind="add_lore", title="卡死提案", status="approved",
        approved_at=datetime.now(UTC) - timedelta(minutes=12),
        cost_sc=50, author_slug="alice",
    )
    fresh = WorldChangeProposal(
        kind="add_lore", title="刚批的提案", status="approved",
        approved_at=datetime.now(UTC), cost_sc=50, author_slug="alice",
    )
    db_session.add_all([stuck, fresh])
    await db_session.commit()

    n = await reclaim_stuck_proposals(db_session, stuck_minutes=10)
    assert n == 1

    await db_session.refresh(stuck)
    await db_session.refresh(fresh)
    assert stuck.status == "failed"       # reclaimed
    assert fresh.status == "approved"     # too fresh → untouched

    bal = (await db_session.execute(
        select(ResidentTreasury.balance_sc).where(ResidentTreasury.resident_slug == "alice")
    )).scalar_one()
    assert bal == 50                       # fuel refunded


@pytest.mark.anyio
async def test_sweep_orphan_lab_reservations_releases_terminal(db_session, monkeypatch):
    # sweep opens its own async_session(); point it at this test's engine.
    import app.tasks.nightly_cron as nc
    monkeypatch.setattr(nc, "async_session", lambda: _SessionCtx(db_session))

    run = LabRun(id="run-1", task_id="t1", researcher_slug="bob", status="succeeded")
    db_session.add(run)
    db_session.add(LabRunBudget(run_id="run-1", tenant_id="tn", reserved_model_tokens=100,
                                reserved_tool_calls=3))
    running = LabRun(id="run-2", task_id="t2", researcher_slug="bob", status="running")
    db_session.add(running)
    db_session.add(LabRunBudget(run_id="run-2", tenant_id="tn", reserved_model_tokens=50))
    await db_session.commit()

    released = await sweep_orphan_lab_reservations()
    assert released == 1

    b1 = (await db_session.execute(select(LabRunBudget).where(LabRunBudget.run_id == "run-1"))).scalar_one()
    b2 = (await db_session.execute(select(LabRunBudget).where(LabRunBudget.run_id == "run-2"))).scalar_one()
    assert b1.reserved_model_tokens == 0 and b1.reserved_tool_calls == 0   # terminal → released
    assert b2.reserved_model_tokens == 50                                   # running → untouched


class _SessionCtx:
    """Wrap the test's db_session as an async-context-manager (no close)."""
    def __init__(self, session):
        self._s = session

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *exc):
        return False
