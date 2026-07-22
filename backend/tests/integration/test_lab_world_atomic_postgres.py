"""AC18: real-PostgreSQL World Governor atomicity and concurrent CAS."""
from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.dynamic_mechanic import DynamicMechanic
from app.models.lab_event import OutboxEvent
from app.models.world_change_proposal import WorldChangeProposal
from app.models.world_revision import WorldRevision
from app.services import proposal_service, world_revision_service


pytestmark = [pytest.mark.lab_postgres, pytest.mark.anyio]
_TRUE = {"1", "true", "yes", "on"}


@pytest.fixture
async def world_factory(monkeypatch):
    if os.environ.get("LAB_POSTGRES_REQUIRED", "").lower() not in _TRUE:
        pytest.skip("AC18 real PostgreSQL evidence was not requested")
    database_url = os.environ.get("LAB_TEST_DATABASE_URL", "")
    release_run_id = os.environ.get("LAB_RELEASE_RUN_ID", "")
    if not database_url or not release_run_id:
        pytest.fail("AC18 requires LAB_TEST_DATABASE_URL and LAB_RELEASE_RUN_ID")
    if make_url(database_url).drivername != "postgresql+asyncpg":
        pytest.fail("LAB_TEST_DATABASE_URL must use postgresql+asyncpg")

    schema = f"lab_world_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(database_url)
    test_engine = None
    try:
        async with admin_engine.begin() as connection:
            database, disposable = (
                await connection.execute(
                    text(
                        "SELECT current_database(), "
                        "current_setting('simverse.release_disposable', true)"
                    )
                )
            ).one()
            if (
                database != f"simverse_lab_release_{release_run_id}"
                or disposable != "on"
            ):
                pytest.fail("AC18 database is not the asserted disposable release database")
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        test_engine = create_async_engine(
            database_url,
            connect_args={
                "server_settings": {"search_path": f'"{schema}", public'}
            },
        )
        async with test_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all, checkfirst=False)
        factory = async_sessionmaker(
            test_engine, class_=AsyncSession, expire_on_commit=False
        )
        monkeypatch.setattr("app.services.proposal_service.emit", AsyncMock())
        monkeypatch.setattr("app.lab.apply.reload_world", AsyncMock())
        monkeypatch.setattr("app.lab.apply.publish_world_reload", AsyncMock())
        monkeypatch.setattr("app.lab.apply.broadcast_world_changed", AsyncMock())
        yield factory
    finally:
        if test_engine is not None:
            await test_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


async def _pending_lore(factory, text_value: str) -> str:
    async with factory() as db:
        proposal = await proposal_service.create_proposal(
            db,
            kind="add_lore",
            title="PostgreSQL world atomicity",
            rationale="merge gate",
            patch={"location_id": "academy", "text": text_value},
            author_slug="sage",
            cost_sc=0,
        )
        return proposal.id


async def _counts(factory, proposal_id: str) -> tuple[int, int, int, str]:
    async with factory() as db:
        revision_count = await db.scalar(
            select(func.count()).select_from(WorldRevision).where(
                WorldRevision.proposal_id == proposal_id
            )
        )
        outbox_count = await db.scalar(
            select(func.count()).select_from(OutboxEvent).where(
                OutboxEvent.topic == "world_changed"
            )
        )
        overlay_count = await db.scalar(
            select(func.count()).select_from(DynamicMechanic).where(
                DynamicMechanic.code == "lore:academy"
            )
        )
        proposal = await db.get(WorldChangeProposal, proposal_id)
        return revision_count, outbox_count, overlay_count, proposal.status


async def test_two_postgres_admins_create_exactly_one_world_revision(world_factory):
    proposal_id = await _pending_lore(world_factory, "one durable revision")

    async def approve(reviewer: str) -> bool:
        async with world_factory() as db:
            try:
                await proposal_service.approve_proposal(
                    db, proposal_id, reviewer, ""
                )
                return True
            except proposal_service.ProposalError:
                return False

    outcomes = await asyncio.gather(approve("admin-one"), approve("admin-two"))
    assert sum(outcomes) == 1
    assert await _counts(world_factory, proposal_id) == (1, 1, 1, "applied")


async def test_postgres_outbox_fault_rolls_back_every_world_effect(
    world_factory, monkeypatch
):
    proposal_id = await _pending_lore(world_factory, "must never escape")
    monkeypatch.setattr(
        world_revision_service,
        "build_world_changed_envelope",
        AsyncMock(side_effect=RuntimeError("injected world outbox fault")),
    )

    async with world_factory() as db:
        with pytest.raises(RuntimeError, match="world outbox fault"):
            await proposal_service.approve_proposal(
                db, proposal_id, "admin", ""
            )

    assert await _counts(world_factory, proposal_id) == (0, 0, 0, "pending")
