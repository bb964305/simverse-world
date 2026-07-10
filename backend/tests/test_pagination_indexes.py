"""P1-3: residents list pagination + perf index declarations + slow-query hook."""

import pytest
from sqlalchemy import select

from app.models.resident import Resident
from app.services.resident_service import list_residents


async def _seed(db, n):
    for i in range(n):
        db.add(Resident(slug=f"r{i}", name=f"R{i}", creator_id="system",
                        district="cafe", status="idle", heat=i, tile_x=i, tile_y=i))
    await db.commit()


@pytest.mark.anyio
async def test_list_default_returns_all_ordered_by_heat(db_session):
    await _seed(db_session, 5)
    rows = await list_residents(db_session)
    assert len(rows) == 5
    heats = [r.heat for r in rows]
    assert heats == sorted(heats, reverse=True)  # heat desc


@pytest.mark.anyio
async def test_list_limit_and_offset(db_session):
    await _seed(db_session, 5)
    page1 = await list_residents(db_session, limit=2, offset=0)
    page2 = await list_residents(db_session, limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 2
    # No overlap, and ordering is stable (heat desc, id tiebreak).
    ids1 = {r.id for r in page1}
    ids2 = {r.id for r in page2}
    assert ids1.isdisjoint(ids2)
    assert page1[0].heat >= page1[1].heat >= page2[0].heat


@pytest.mark.anyio
async def test_offset_past_end_is_empty(db_session):
    await _seed(db_session, 3)
    assert await list_residents(db_session, limit=10, offset=10) == []


def test_perf_indexes_declared():
    """The two P1-3 indexes are declared on the ORM metadata (migration 030)."""
    from app.models.conversation import Conversation
    conv_indexes = {ix.name for ix in Conversation.__table__.indexes}
    assert "ix_conversations_resident_rating" in conv_indexes
    # residents.status carries a single-column index.
    status_col = Resident.__table__.c.status
    assert status_col.index is True


def test_slow_query_logging_install():
    """Threshold 0 is a no-op; a positive threshold attaches a cursor listener."""
    from app.database import _install_slow_query_logging, engine
    dispatch = engine.sync_engine.dispatch
    before = len(dispatch.before_cursor_execute)
    _install_slow_query_logging(0)
    assert len(dispatch.before_cursor_execute) == before  # disabled → no change
    _install_slow_query_logging(200)
    assert len(dispatch.before_cursor_execute) == before + 1  # enabled → one listener
