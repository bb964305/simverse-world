"""Phase 3 (recovery plan) — world apply is transactionally atomic.

Status report gap #3: overlay mutations committed inside apply.py BEFORE the
revision, outbox record, and proposal terminal status committed in
proposal_service, so a crash could split world state from its audit record.

These tests inject a failure between the overlay write and the final commit and
assert nothing is visible (no overlay, no revision, proposal not applied), and
that two concurrent admins cannot both apply/revert the same proposal.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.agent import location_lore, map_data
from app.models.dynamic_location import DynamicLocation
from app.models.world_change_proposal import WorldChangeProposal
from app.models.world_revision import WorldRevision
from app.services import location_tracker
from app.services import proposal_service as psvc
from app.services import world_revision_service as wrsvc


@pytest.fixture
def rev_env(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    import app.database as database
    monkeypatch.setattr(database, "async_session", factory)
    monkeypatch.setattr("app.services.proposal_service.emit", AsyncMock())
    monkeypatch.setattr("app.ws.manager.manager.broadcast", AsyncMock())
    snap = {k: dict(v) for k, v in map_data.LOCATIONS.items()}
    snap_dyn = set(map_data._dynamic_slugs)
    snap_lore = dict(location_lore._dynamic_lore)
    yield factory
    map_data.LOCATIONS.clear()
    map_data.LOCATIONS.update(snap)
    map_data._dynamic_slugs = snap_dyn
    location_lore._dynamic_lore = snap_lore
    location_tracker.rebuild_lookup()


async def _pending_lore(factory) -> str:
    async with factory() as db:
        p = await psvc.create_proposal(
            db, kind="add_lore", title="加个传说", rationale="研究员的发现",
            patch={"location_id": "academy", "text": "学院深处藏着秘密"},
            author_slug="sage", cost_sc=0,
        )
        return p.id


@pytest.mark.anyio
async def test_apply_rolls_back_overlay_when_revision_fails(rev_env, monkeypatch):
    """A failure AFTER the overlay write but BEFORE the atomic commit must leave
    no visible overlay and no revision — world state and its audit stay together.
    RED before the fix: apply.py committed the overlay first, so it survived."""
    factory = rev_env
    pid = await _pending_lore(factory)

    # Inject: revision recording blows up right after the overlay is written but
    # before the single atomic commit.
    monkeypatch.setattr(
        wrsvc, "record_apply",
        AsyncMock(side_effect=RuntimeError("injected crash before commit")),
    )
    async with factory() as db:
        with pytest.raises(RuntimeError, match="injected crash"):
            await psvc.approve_proposal(db, pid, "admin1", "ok")

    async with factory() as db:
        # No revision recorded.
        revs = (await db.execute(
            select(WorldRevision).where(WorldRevision.proposal_id == pid)
        )).scalars().all()
        assert revs == []
        # Proposal never reached applied.
        p = await db.get(WorldChangeProposal, pid)
        assert p.status != "applied"
    # The lore overlay is NOT visible (rolled back with the transaction).
    assert location_lore.lore_for("academy") != "学院深处藏着秘密"


@pytest.mark.anyio
async def test_apply_commits_overlay_and_revision_together(rev_env):
    """The happy path still lands exactly one revision + the overlay, atomically."""
    factory = rev_env
    pid = await _pending_lore(factory)
    async with factory() as db:
        p = await psvc.approve_proposal(db, pid, "admin1", "ok")
        assert p.status == "applied"
    async with factory() as db:
        revs = (await db.execute(
            select(WorldRevision).where(WorldRevision.proposal_id == pid)
        )).scalars().all()
        assert len(revs) == 1 and revs[0].status == "applied"
    assert location_lore.lore_for("academy") == "学院深处藏着秘密"


@pytest.mark.anyio
async def test_two_admin_concurrent_approve_only_one_applies(rev_env):
    """Two admins approving the same pending proposal: exactly one applies it and
    exactly one revision is recorded (CAS on the pending->approved transition)."""
    factory = rev_env
    pid = await _pending_lore(factory)

    async def _approve(admin):
        async with factory() as db:
            try:
                await psvc.approve_proposal(db, pid, admin, "")
                return True
            except psvc.ProposalError:
                return False

    results = await asyncio.gather(_approve("admin1"), _approve("admin2"))
    assert sum(results) == 1  # exactly one winner

    async with factory() as db:
        revs = (await db.execute(
            select(WorldRevision).where(WorldRevision.proposal_id == pid)
        )).scalars().all()
        assert len(revs) == 1  # never double-applied
        p = await db.get(WorldChangeProposal, pid)
        assert p.status == "applied"
