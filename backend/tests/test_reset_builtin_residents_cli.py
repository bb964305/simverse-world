"""The roster reset is read-only unless an operator confirms an exact count."""
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from app.models.resident import Resident
from seed import reset_builtin_residents as reset


pytestmark = pytest.mark.anyio


def _sessions(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


async def _legacy(db_session) -> Resident:
    resident = Resident(
        slug="isabella",
        name="Legacy",
        district="central_plaza",
        status="idle",
        resident_type="npc",
        creator_id="legacy",
    )
    db_session.add(resident)
    await db_session.commit()
    return resident


async def test_default_run_is_a_read_only_preview(db_session, monkeypatch):
    resident = await _legacy(db_session)
    monkeypatch.setattr(reset, "async_session", _sessions(db_session))

    await reset.main()

    assert await db_session.get(Resident, resident.id) is not None


async def test_apply_refuses_changed_target_count_before_writes(db_session, monkeypatch):
    resident = await _legacy(db_session)
    monkeypatch.setattr(reset, "async_session", _sessions(db_session))

    with pytest.raises(RuntimeError, match="expected 0 purge target.*found 1"):
        await reset.main(apply=True, expect_targets=0)

    assert await db_session.get(Resident, resident.id) is not None
    assert (await db_session.execute(select(Resident.id))).scalars().all() == [resident.id]


async def test_apply_requires_explicit_expected_count(db_session, monkeypatch):
    resident = await _legacy(db_session)
    monkeypatch.setattr(reset, "async_session", _sessions(db_session))

    with pytest.raises(RuntimeError, match="requires --expect-targets"):
        await reset.main(apply=True)

    assert await db_session.get(Resident, resident.id) is not None
