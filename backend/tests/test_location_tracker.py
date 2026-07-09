"""S5 LocationTracker: in-memory move detection + consumer upsert + first-visit event."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models.user import User  # noqa: F401
from app.models.location_visit import LocationVisit
from app.models.achievement import UserAchievement


@pytest.fixture
def lt_session(db_engine):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("app.services.location_tracker.async_session", factory):
        yield


def _drain_and_reset():
    from app.services import location_tracker as lt
    while not lt._queue.empty():
        lt._queue.get_nowait()
    lt._last_location.clear()


def test_location_at_tile_lookup():
    from app.services import location_tracker as lt
    # academy bounds are (15,18,42,34) and it's first in LOCATIONS.
    assert lt.location_at_tile(20, 20) == "academy"
    assert lt.location_at_tile(99999, 99999) is None


def test_on_move_enqueues_only_on_new_location():
    from app.services import location_tracker as lt
    _drain_and_reset()

    lt.on_move("u1", 20, 20)  # enter academy
    assert lt._queue.qsize() == 1
    assert lt._queue.get_nowait() == ("u1", "academy")

    lt.on_move("u1", 21, 21)  # still academy → no enqueue
    assert lt._queue.qsize() == 0

    lt.on_move("u1", 99999, 99999)  # open world → reset, no enqueue
    assert lt._queue.qsize() == 0

    lt.on_move("u1", 20, 20)  # re-enter academy → enqueue again
    assert lt._queue.qsize() == 1
    _drain_and_reset()


@pytest.mark.anyio
async def test_process_one_first_visit_then_increment(db_session, lt_session):
    from app.services import location_tracker as lt

    with patch.object(lt, "emit", new_callable=AsyncMock) as emit_mock:
        await lt.process_one("u1", "academy")
        await lt.process_one("u1", "academy")

    rows = (await db_session.execute(
        select(LocationVisit).where(LocationVisit.user_id == "u1")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].visit_count == 2

    events = [c.args[1] for c in emit_mock.call_args_list]
    assert events.count("location_first_visit") == 1  # only on the first


@pytest.mark.anyio
async def test_first_visits_unlock_explorer_achievement(db_session, lt_session):
    from app.services import location_tracker as lt
    import app.events.achievements as ach
    from app.agent.map_data import LOCATIONS

    factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    five = list(LOCATIONS.keys())[:5]

    with patch("app.events.achievements.async_session", factory), \
         patch.object(ach.manager, "send", new_callable=AsyncMock):
        for loc in five:
            await lt.process_one("u1", loc)

    ua = (await db_session.execute(
        select(UserAchievement).where(UserAchievement.code == "explorer_5")
    )).scalar_one()
    assert ua.unlocked_at is not None
