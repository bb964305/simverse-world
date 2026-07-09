"""A2 world event ops: holiday scheduling (idempotent) + collective memory on start."""

from datetime import date, datetime, UTC
from unittest.mock import patch

import pytest
from sqlalchemy import select, func

from app.models.resident import Resident
from app.models.world_event import WorldEvent
from app.models.memory import Memory


@pytest.mark.anyio
async def test_ensure_holiday_events_idempotent(db_session):
    from app.tasks.event_templates import ensure_scheduled_events
    # No news on this run (force probability to 0) — isolate the holiday path.
    with patch("app.tasks.event_templates.random.random", return_value=1.0):
        n1 = await ensure_scheduled_events(db_session, date(2026, 9, 15))  # 丰收节
        n2 = await ensure_scheduled_events(db_session, date(2026, 9, 15))  # idempotent

    assert n1 == 1 and n2 == 0
    events = (await db_session.execute(select(WorldEvent).where(WorldEvent.title == "丰收节"))).scalars().all()
    assert len(events) == 1 and events[0].type == "festival"


@pytest.mark.anyio
async def test_news_scheduled_when_rolled(db_session):
    from app.tasks.event_templates import ensure_scheduled_events
    with patch("app.tasks.event_templates.random.random", return_value=0.0), \
         patch("app.tasks.event_templates.random.choice", side_effect=lambda x: x[0]):
        n = await ensure_scheduled_events(db_session, date(2026, 3, 3))  # not a holiday
    assert n == 1
    news = (await db_session.execute(select(WorldEvent).where(WorldEvent.type == "news"))).scalars().all()
    assert len(news) == 1


@pytest.mark.anyio
async def test_collective_memory_on_start(db_session):
    from app.services.world_event_service import write_collective_memories
    db_session.add(Resident(slug="a", name="A", creator_id="system", district="central_plaza", status="idle", tile_x=1, tile_y=1))
    db_session.add(Resident(slug="b", name="B", creator_id="system", district="central_plaza", status="idle", tile_x=2, tile_y=2))
    db_session.add(Resident(slug="s", name="S", creator_id="system", district="central_plaza", status="sleeping", tile_x=3, tile_y=3))
    await db_session.commit()

    n = await write_collective_memories(db_session, {"title": "丰收节", "description": "田里的作物成熟了"})
    assert n == 2  # sleeping resident excluded

    mems = (await db_session.execute(
        select(Memory).where(Memory.source == "world_event")
    )).scalars().all()
    assert len(mems) == 2 and all(m.importance == 0.5 and "作物成熟" in m.content for m in mems)
