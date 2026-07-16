"""P3 — world self-modification governance (spec §7).

proposal → approve → apply → overlay active → merged into LOCATIONS + tile index
→ revert; bounds-conflict detection rejects overlapping buildings; treasury fuel
is frozen on create and refunded on reject.
"""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.agent import map_data
from app.lab.apply import validate_add_location, ApplyError
from app.models.dynamic_location import DynamicLocation
from app.models.world_change_proposal import WorldChangeProposal
from app.services import coin_service
from app.services import proposal_service as psvc
from app.services import location_tracker


@pytest.fixture
def gov_env(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    import app.database as database
    monkeypatch.setattr(database, "async_session", factory)  # reload_world reads test DB
    monkeypatch.setattr("app.services.proposal_service.emit", AsyncMock())
    # Snapshot + restore the in-memory LOCATIONS the overlay mutates.
    snap = {k: dict(v) for k, v in map_data.LOCATIONS.items()}
    snap_dyn = set(map_data._dynamic_slugs)
    yield factory
    map_data.LOCATIONS.clear()
    map_data.LOCATIONS.update(snap)
    map_data._dynamic_slugs = snap_dyn
    location_tracker.rebuild_lookup()


# ── validation unit ───────────────────────────────────────────────────

def test_validate_add_location():
    good = {"slug": "observatory", "data": {"name": "天文台", "bounds": [5, 88, 15, 96], "entrance": [10, 88]}}
    assert validate_add_location(good) == []
    # overlaps academy (15,18,42,34)
    bad = {"slug": "x", "data": {"name": "X", "bounds": [20, 20, 30, 30]}}
    errs = validate_add_location(bad)
    assert any("overlap" in e for e in errs)
    # slug clash + out-of-range bounds
    assert any("already exists" in e for e in validate_add_location({"slug": "academy", "data": {"name": "A", "bounds": [5, 88, 15, 96]}}))


# ── apply + revert ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_add_location_apply_then_revert(gov_env):
    factory = gov_env
    patch = {"slug": "observatory", "data": {
        "name": "天文台", "type": "public", "role": "research",
        "bounds": [5, 88, 15, 96], "center": [10, 92], "entrance": [10, 88],
        "description": "新建的观星台",
    }}
    async with factory() as db:
        p = await psvc.create_proposal(db, kind="add_location", title="加建天文台",
                                       rationale="研究员发现了适合观星的空地", patch=patch,
                                       author_slug="sage", cost_sc=0)
        pid = p.id
        assert p.status == "pending"

    async with factory() as db:
        p = await psvc.approve_proposal(db, pid, "admin1", "looks good")
        assert p.status == "applied"

    # Overlay merged into LOCATIONS + tile index rebuilt.
    assert "observatory" in map_data.LOCATIONS
    assert map_data.get_location_id_at(10, 92) == "observatory"
    assert location_tracker.location_at_tile(10, 92) == "observatory"

    async with factory() as db:
        row = (await db.execute(select(DynamicLocation).where(DynamicLocation.slug == "observatory"))).scalar_one()
        assert row.active is True

    async with factory() as db:
        p = await psvc.revert_proposal(db, pid, "admin1")
        assert p.status == "reverted"

    # Soft-removed from the live world.
    assert map_data.get_location_id_at(10, 92) is None
    async with factory() as db:
        row = (await db.execute(select(DynamicLocation).where(DynamicLocation.slug == "observatory"))).scalar_one()
        assert row.active is False


@pytest.mark.anyio
async def test_conflicting_bounds_rejected(gov_env):
    factory = gov_env
    patch = {"slug": "ghost", "data": {"name": "幽灵楼", "bounds": [20, 20, 30, 30]}}  # overlaps academy
    async with factory() as db:
        p = await psvc.create_proposal(db, kind="add_location", title="冲突建筑",
                                       rationale="...", patch=patch, author_slug="sage", cost_sc=0)
        pid = p.id
    async with factory() as db:
        with pytest.raises(psvc.ProposalError):
            await psvc.approve_proposal(db, pid, "admin1", "")
    async with factory() as db:
        p = await db.get(WorldChangeProposal, pid)
        assert p.status == "failed"
    assert "ghost" not in map_data.LOCATIONS  # never merged


# ── treasury fuel ─────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_treasury_fuel_frozen_and_refunded_on_reject(gov_env):
    factory = gov_env
    async with factory() as db:
        await coin_service.treasury_credit(db, "sage", 50, "seed")
    async with factory() as db:
        p = await psvc.create_proposal(db, kind="add_lore", title="加个传说",
                                       rationale="...", patch={"location_id": "academy", "text": "传说……"},
                                       author_slug="sage", cost_sc=20)
        pid = p.id
    async with factory() as db:
        assert await coin_service.treasury_balance(db, "sage") == 30  # 50 - 20 frozen
    async with factory() as db:
        p = await psvc.reject_proposal(db, pid, "admin1", "not now")
        assert p.status == "rejected"
    async with factory() as db:
        assert await coin_service.treasury_balance(db, "sage") == 50  # refunded


@pytest.mark.anyio
async def test_insufficient_treasury_fuel_rejected(gov_env):
    factory = gov_env
    async with factory() as db:
        with pytest.raises(psvc.ProposalError):
            await psvc.create_proposal(db, kind="add_lore", title="没钱",
                                       rationale="...", patch={"location_id": "academy", "text": "x"},
                                       author_slug="broke", cost_sc=100)
