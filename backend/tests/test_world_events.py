"""S1 world event bus: cron flip, active cache, and prompt injection."""

import asyncio
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.user import User  # noqa: F401
from app.models.resident import Resident
from app.models.world_event import WorldEvent


def _make_event(**kw):
    now = datetime.now(UTC)
    defaults = dict(
        type="festival",
        title="元宵灯会",
        description="全城挂起花灯",
        payload_json={},
        starts_at=now - timedelta(hours=1),
        ends_at=now + timedelta(hours=1),
        is_active=False,
    )
    defaults.update(kw)
    return WorldEvent(**defaults)


# ── cron flip logic ──────────────────────────────────────────────────

@pytest.mark.anyio
async def test_flip_activates_started_event(db_session):
    from app.services.world_event_service import flip_active_events

    db_session.add(_make_event())  # started, not yet active
    await db_session.commit()

    changes = await flip_active_events(db_session)
    assert len(changes) == 1
    event, phase = changes[0]
    assert phase == "start"
    assert event["title"] == "元宵灯会"

    result = await db_session.execute(WorldEvent.__table__.select())
    row = result.first()
    assert row.is_active is True


@pytest.mark.anyio
async def test_flip_ends_expired_event(db_session):
    from app.services.world_event_service import flip_active_events

    now = datetime.now(UTC)
    db_session.add(_make_event(
        starts_at=now - timedelta(hours=2), ends_at=now - timedelta(minutes=1), is_active=True,
    ))
    await db_session.commit()

    changes = await flip_active_events(db_session)
    assert len(changes) == 1
    assert changes[0][1] == "end"


@pytest.mark.anyio
async def test_flip_no_change_for_ongoing_active(db_session):
    from app.services.world_event_service import flip_active_events

    db_session.add(_make_event(is_active=True))  # ongoing + already active
    await db_session.commit()

    changes = await flip_active_events(db_session)
    assert changes == []


# ── active-event cache ───────────────────────────────────────────────

@pytest.mark.anyio
async def test_active_cache_reads_and_invalidates(db_session):
    from app.services import world_event_service as svc

    svc.invalidate_active_cache()
    db_session.add(_make_event(title="A", is_active=True))
    await db_session.commit()

    first = await svc.get_active_events_cached(db_session)
    assert [e["title"] for e in first] == ["A"]

    # Add a second active event; cache still serves the old snapshot…
    db_session.add(_make_event(title="B", is_active=True))
    await db_session.commit()
    cached = await svc.get_active_events_cached(db_session)
    assert [e["title"] for e in cached] == ["A"]

    # …until invalidated.
    svc.invalidate_active_cache()
    fresh = await svc.get_active_events_cached(db_session)
    assert sorted(e["title"] for e in fresh) == ["A", "B"]


# ── prompt injection ─────────────────────────────────────────────────

def test_decision_prompt_includes_world_event():
    from app.agent.prompts import build_decision_prompt

    resident = Resident(slug="r1", name="小明", district="central_plaza", status="idle",
                        tile_x=0, tile_y=0, meta_json={})
    _system, user = build_decision_prompt(
        resident=resident, schedule_phase="day", world_time="10:00",
        nearby_residents=[], memories=[], today_actions=[], available_actions=[],
        max_daily_actions=10, world_events=[{"title": "元宵灯会"}],
    )
    assert "当前世界事件：元宵灯会" in user


def test_decision_prompt_without_events_has_no_line():
    from app.agent.prompts import build_decision_prompt

    resident = Resident(slug="r1", name="小明", district="central_plaza", status="idle",
                        tile_x=0, tile_y=0, meta_json={})
    _system, user = build_decision_prompt(
        resident=resident, schedule_phase="day", world_time="10:00",
        nearby_residents=[], memories=[], today_actions=[], available_actions=[],
        max_daily_actions=10,
    )
    assert "当前世界事件" not in user


def test_player_prompt_includes_world_event():
    from app.llm.prompt import assemble_system_prompt

    resident = Resident(slug="r1", name="小明", district="central_plaza", status="idle",
                        tile_x=0, tile_y=0, soul_md="", persona_md="", ability_md="")
    prompt = assemble_system_prompt(
        resident, world_events=[{"title": "元宵灯会", "description": "全城挂起花灯"}],
    )
    assert "当前世界事件" in prompt
    assert "元宵灯会" in prompt


# ── cron loop broadcasts ─────────────────────────────────────────────

@pytest.mark.anyio
async def test_event_cron_loop_broadcasts_transitions():
    from app.tasks.event_cron import event_cron_loop

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=MagicMock())
    cm.__aexit__ = AsyncMock(return_value=False)

    with patch("app.tasks.event_cron.async_session", return_value=cm), \
         patch("app.tasks.event_cron.flip_active_events",
               AsyncMock(return_value=[({"id": "e1", "title": "元宵灯会"}, "start")])), \
         patch("app.tasks.event_cron.manager.broadcast", new_callable=AsyncMock) as bcast, \
         patch("app.tasks.event_cron.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError())):
        with pytest.raises(asyncio.CancelledError):
            await event_cron_loop()

    bcast.assert_awaited_once()
    msg = bcast.call_args.args[0]
    assert msg["type"] == "world_event"
    assert msg["phase"] == "start"
