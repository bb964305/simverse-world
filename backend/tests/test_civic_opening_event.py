"""P3 ④a:新楼落成庆典 —— 复用 festival 那条已在产的人流拉力。"""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.world_event import WorldEvent
from app.services import civic_service
from app.services.crowd_service import _EVENT_TYPES_WITH_CROWD

DATA = {"slug": "post_office", "name": "邮局", "type": "public",
        "bounds": [44, 100, 48, 106], "center": [46, 103],
        "entrance": [46, 100], "opening_event_days": 3}


@pytest.fixture(autouse=True)
def no_world_reload(monkeypatch):
    monkeypatch.setattr("app.lab.apply.reload_world", AsyncMock(return_value=0))
    monkeypatch.setattr("app.lab.apply.publish_world_reload", AsyncMock())


async def _events(db):
    return (await db.execute(select(WorldEvent))).scalars().all()


@pytest.mark.anyio
async def test_gate_off_creates_no_event(db_session):
    assert await civic_service._add_dynamic_location(db_session, dict(DATA)) is True
    assert await _events(db_session) == []


@pytest.mark.anyio
async def test_gate_on_stages_a_festival(db_session, monkeypatch):
    monkeypatch.setattr(settings, "civic_build_opening_event_enabled", True)
    assert await civic_service._add_dynamic_location(db_session, dict(DATA)) is True
    events = await _events(db_session)
    assert len(events) == 1
    ev = events[0]
    assert ev.type in _EVENT_TYPES_WITH_CROWD, \
        "只有 festival/script 能被 active_event_location 看见并拿到 x3 偏置"
    assert ev.type == "festival"
    assert ev.payload_json == {"location_id": "post_office", "opening": True}
    assert ev.is_active is False, "由 refresh_active_events 翻,与既有 narrative 分支同形"
    assert (ev.ends_at - ev.starts_at).days == 3


@pytest.mark.anyio
async def test_zero_or_missing_days_stages_nothing(db_session, monkeypatch):
    monkeypatch.setattr(settings, "civic_build_opening_event_enabled", True)
    assert await civic_service._add_dynamic_location(
        db_session, {**DATA, "opening_event_days": 0}) is True
    assert await _events(db_session) == []
    assert await civic_service._add_dynamic_location(
        db_session, {k: v for k, v in DATA.items() if k != "opening_event_days"}
    ) is True
    assert await _events(db_session) == []


@pytest.mark.anyio
async def test_days_are_capped_and_bogus_values_ignored(db_session, monkeypatch):
    monkeypatch.setattr(settings, "civic_build_opening_event_enabled", True)
    assert await civic_service._add_dynamic_location(
        db_session, {**DATA, "opening_event_days": 999}) is True
    events = await _events(db_session)
    assert (events[0].ends_at - events[0].starts_at).days == \
        civic_service._OPENING_EVENT_MAX_DAYS
    assert await civic_service._add_dynamic_location(
        db_session, {**DATA, "slug": "theater2", "opening_event_days": True}) is True
    assert len(await _events(db_session)) == 1, "bool 是 int 子类,不许当天数用"


@pytest.mark.anyio
async def test_event_is_committed_with_the_building(db_session, monkeypatch):
    """同一次 commit —— 不新增提交点,楼在事件就在。"""
    monkeypatch.setattr(settings, "civic_build_opening_event_enabled", True)
    await civic_service._add_dynamic_location(db_session, dict(DATA))
    db_session.expire_all()
    assert len(await _events(db_session)) == 1
