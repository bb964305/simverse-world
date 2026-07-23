"""Realism P0-4: fired scripts route through flip_active_events (broadcast +
collective memory), instead of the direct is_active=True that skipped both."""
import pytest
from datetime import datetime, UTC, timedelta

from sqlalchemy import select

from app.config import settings
from app.models.resident import Resident
from app.models.season import Season, SeasonScript
from app.models.world_event import WorldEvent
from app.models.memory import Memory
from app.services import script_service as ss
from app.services.world_event_service import flip_active_events, write_collective_memories


async def _due_act(db):
    now = datetime.now(UTC)
    s = Season(title="谜案季", theme="疑云", status="active",
               starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=24),
               payload_json={})
    db.add(s)
    await db.commit()
    act = SeasonScript(
        season_id=s.id, act=1, trigger_at=now - timedelta(minutes=1), status="pending",
        event_payload_json={"title": "钟楼疑云", "description": "钟楼午夜传出怪声。"},
    )
    db.add(act)
    await db.commit()
    return s


@pytest.mark.anyio
async def test_fired_script_starts_inactive_then_flip_broadcasts(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_enabled", True)
    res = Resident(slug="wit", name="旁观者", creator_id="system", district="cafe",
                   status="idle", tile_x=1, tile_y=1)
    db_session.add(res)
    await _due_act(db_session)

    fired = await ss.fire_due_scripts(db_session)
    assert len(fired) == 1

    we = (await db_session.execute(select(WorldEvent).where(WorldEvent.type == "script"))).scalar_one()
    assert we.is_active is False           # not active immediately anymore
    assert we.starts_at is not None

    # Next event_cron pass: flip emits a "start" transition for the script event.
    changes = await flip_active_events(db_session)
    starts = [ev for ev, phase in changes if phase == "start" and ev["type"] == "script"]
    assert len(starts) == 1

    # And the start drives collective memory to non-sleeping residents.
    n = await write_collective_memories(db_session, starts[0])
    assert n >= 1
    mem = (await db_session.execute(
        select(Memory).where(Memory.source == "world_event", Memory.resident_id == res.id)
    )).scalars().first()
    assert mem is not None


@pytest.mark.anyio
async def test_fired_script_active_immediately_when_realism_off(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_enabled", False)
    await _due_act(db_session)
    await ss.fire_due_scripts(db_session)
    we = (await db_session.execute(select(WorldEvent).where(WorldEvent.type == "script"))).scalar_one()
    assert we.is_active is True            # legacy behavior preserved
