"""Approved-v10 P4b World Governor transaction, fence, and CAS regressions.

The setup reuses ``test_world_revision._seed_observatory`` so the new matrix
exercises the same overlay representation as the established apply/revert
tests, while keeping all new cases in this file.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import Base
from app.agent import location_lore, map_data
from app.lab import compiler
from app.models.dynamic_location import DynamicLocation
from app.models.lab_event import OutboxEvent
from app.models.world_change_proposal import WorldChangeProposal
from app.models.world_revision import WorldRevision
from app.services import location_tracker
from app.services import proposal_service as proposals
from app.services import world_revision_service as revisions
from tests.test_world_revision import _seed_observatory


@pytest.fixture
def world_fault_env(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.services.proposal_service.emit", AsyncMock())
    monkeypatch.setattr("app.ws.manager.manager.broadcast", AsyncMock())
    snapshot = {key: dict(value) for key, value in map_data.LOCATIONS.items()}
    dynamic_slugs = set(map_data._dynamic_slugs)
    lore = dict(location_lore._dynamic_lore)
    yield factory, db_engine
    map_data.LOCATIONS.clear()
    map_data.LOCATIONS.update(snapshot)
    map_data._dynamic_slugs = dynamic_slugs
    location_lore._dynamic_lore = lore
    location_tracker.rebuild_lookup()


async def _create_lore(factory, *, text: str = "a durable new legend") -> str:
    async with factory() as db:
        proposal = await proposals.create_proposal(
            db,
            kind="add_lore",
            title="new legend",
            rationale="test",
            patch={"location_id": "academy", "text": text},
            author_slug="sage",
            cost_sc=0,
        )
        return proposal.id


async def _world_counts(factory, proposal_id: str) -> tuple[int, int, WorldChangeProposal]:
    async with factory() as db:
        revision_count = (
            await db.execute(
                select(func.count()).select_from(WorldRevision).where(
                    WorldRevision.proposal_id == proposal_id
                )
            )
        ).scalar_one()
        outbox_count = (
            await db.execute(
                select(func.count()).select_from(OutboxEvent).where(
                    OutboxEvent.topic == "world_changed"
                )
            )
        ).scalar_one()
        proposal = await db.get(WorldChangeProposal, proposal_id)
        return revision_count, outbox_count, proposal


@pytest.mark.anyio
async def test_apply_outbox_fault_rolls_back_overlay_revision_and_terminal_status(
    world_fault_env, monkeypatch
):
    factory, _ = world_fault_env
    proposal_id = await _create_lore(factory, text="must not escape")
    monkeypatch.setattr(
        revisions,
        "build_world_changed_envelope",
        AsyncMock(side_effect=RuntimeError("injected outbox fault")),
    )

    async with factory() as db:
        with pytest.raises(RuntimeError, match="outbox fault"):
            await proposals.approve_proposal(db, proposal_id, "admin", "")

    revision_count, outbox_count, proposal = await _world_counts(factory, proposal_id)
    assert revision_count == 0
    assert outbox_count == 0
    assert proposal.status != "applied"
    assert location_lore.lore_for("academy") != "must not escape"


@pytest.mark.anyio
async def test_apply_final_commit_fault_leaves_no_partial_world_effect(world_fault_env):
    factory, _ = world_fault_env
    proposal_id = await _create_lore(factory, text="commit-or-nothing")

    def _fail_final_commit(sync_session):
        if any(
            isinstance(row, WorldChangeProposal) and row.status == "applied"
            for row in sync_session.dirty
        ):
            raise RuntimeError("injected final world commit fault")

    event.listen(AsyncSession.sync_session_class, "before_commit", _fail_final_commit)
    try:
        async with factory() as db:
            with pytest.raises(RuntimeError, match="final world commit"):
                await proposals.approve_proposal(db, proposal_id, "admin", "")
    finally:
        event.remove(AsyncSession.sync_session_class, "before_commit", _fail_final_commit)

    revision_count, outbox_count, proposal = await _world_counts(factory, proposal_id)
    assert revision_count == 0
    assert outbox_count == 0
    assert proposal.status != "applied"
    assert location_lore.lore_for("academy") != "commit-or-nothing"


@pytest.mark.anyio
async def test_reload_fault_occurs_after_one_atomic_commit_and_retry_cannot_duplicate(
    world_fault_env, monkeypatch
):
    from app.lab import apply as apply_engine

    factory, _ = world_fault_env
    proposal_id = await _create_lore(factory, text="committed before reload")
    monkeypatch.setattr(
        apply_engine,
        "reload_world",
        AsyncMock(side_effect=RuntimeError("injected post-commit reload fault")),
    )

    async with factory() as db:
        with pytest.raises(RuntimeError, match="post-commit reload"):
            await proposals.approve_proposal(db, proposal_id, "admin", "")

    revision_count, outbox_count, proposal = await _world_counts(factory, proposal_id)
    assert proposal.status == "applied"
    assert revision_count == 1
    assert outbox_count == 1

    async with factory() as db:
        with pytest.raises(proposals.ProposalError, match="not pending"):
            await proposals.approve_proposal(db, proposal_id, "admin", "retry")
    after_revision_count, after_outbox_count, _ = await _world_counts(factory, proposal_id)
    assert (after_revision_count, after_outbox_count) == (1, 1)


@pytest.mark.anyio
async def test_revert_outbox_fault_restores_neither_overlay_nor_audit(world_fault_env, monkeypatch):
    factory, _ = world_fault_env
    await _seed_observatory(factory, description="before")
    async with factory() as db:
        proposal = await proposals.create_proposal(
            db,
            kind="edit_location",
            title="edit",
            rationale="test",
            patch={"slug": "observatory", "data": {"description": "after"}},
            author_slug="sage",
            cost_sc=0,
        )
        proposal_id = proposal.id
    async with factory() as db:
        await proposals.approve_proposal(db, proposal_id, "admin", "")

    monkeypatch.setattr(
        revisions,
        "build_world_changed_envelope",
        AsyncMock(side_effect=RuntimeError("injected revert outbox fault")),
    )
    async with factory() as db:
        with pytest.raises(RuntimeError, match="revert outbox fault"):
            await proposals.revert_proposal(db, proposal_id, "admin")

    async with factory() as db:
        proposal = await db.get(WorldChangeProposal, proposal_id)
        revision = (
            await db.execute(
                select(WorldRevision).where(WorldRevision.proposal_id == proposal_id)
            )
        ).scalar_one()
        location = (
            await db.execute(
                select(DynamicLocation).where(DynamicLocation.slug == "observatory")
            )
        ).scalar_one()
        outbox_count = (
            await db.execute(
                select(func.count()).select_from(OutboxEvent).where(
                    OutboxEvent.topic == "world_changed"
                )
            )
        ).scalar_one()

    assert proposal.status == "applied"
    assert revision.status == "applied"
    assert location.data_json["description"] == "after"
    assert outbox_count == 1, "only the original apply event may remain"


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["add_location", "add_mechanic", "tile", "path"])
async def test_disabled_world_kinds_are_rejected_by_compiler_and_service(
    world_fault_env, kind
):
    factory, _ = world_fault_env
    async with factory() as db:
        with pytest.raises(compiler.CompileError):
            await compiler.compile_draft(
                db,
                draft={"kind": kind, "patch": {}, "title": "disabled", "rationale": ""},
                origin_ref="run-disabled",
                author_slug="sage",
            )
        with pytest.raises(proposals.ProposalError, match=r"not (?:enabled|open)"):
            await proposals.create_proposal(
                db,
                kind=kind,
                title="disabled",
                rationale="",
                patch={},
                origin_ref="run-disabled",
                author_slug="sage",
            )
        await db.rollback()

    async with factory() as db:
        count = (
            await db.execute(select(func.count()).select_from(WorldChangeProposal))
        ).scalar_one()
    assert count == 0


@pytest.mark.anyio
async def test_global_fence_rejects_a_proposal_captured_at_the_old_epoch(
    world_fault_env,
):
    from app.lab import control_plane

    factory, engine = world_fault_env
    import app.models.lab_control  # noqa: F401

    # The shared sqlite fixture was created before the delayed v2 model import.
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with factory() as db:
        await control_plane.ensure_global_control(db)
        proposal = await proposals.create_proposal(
            db,
            kind="add_lore",
            title="stale lore",
            rationale="test",
            patch={"location_id": "academy", "text": "must stay fenced"},
            origin_ref="run-stale-world",
            author_slug="sage",
            cost_sc=0,
        )
        proposal_id = proposal.id
        old_epoch = proposal.global_fencing_epoch

    async with factory() as db:
        kill = await control_plane.activate_global_kill(
            db,
            requested_by="admin",
            idempotency_key="kill-world-fence",
            deadline_at=datetime.now(UTC) + timedelta(seconds=30),
            now=datetime.now(UTC),
        )
    assert kill.fencing_epoch > old_epoch

    async with factory() as db:
        with pytest.raises(proposals.ProposalError, match="stale.*epoch"):
            await proposals.approve_proposal(db, proposal_id, "admin", "")

    revision_count, outbox_count, proposal = await _world_counts(factory, proposal_id)
    assert proposal.status != "applied"
    assert revision_count == 0
    assert outbox_count == 0
    assert location_lore.lore_for("academy") != "must stay fenced"
