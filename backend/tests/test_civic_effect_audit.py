"""P3 ⑥:公投胜出后「落没落地」必须可追溯,且失败原因可区分。"""
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.agent import pathfinder
from app.config import settings
from app.models.dynamic_location import DynamicLocation
from app.models.season import Poll
from app.services import civic_service

THEATER_DATA = {
    "slug": "theater", "name": "剧院", "type": "public",
    "bounds": [172, 40, 178, 50], "center": [175, 45], "entrance": [172, 45],
}
POST_OFFICE_DATA = {
    "slug": "post_office", "name": "邮局", "type": "public",
    "bounds": [44, 100, 48, 106], "center": [46, 103], "entrance": [46, 100],
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr("app.lab.apply.reload_world", AsyncMock(return_value=0))
    monkeypatch.setattr("app.lab.apply.publish_world_reload", AsyncMock())
    pathfinder.reset_walkable_cache()
    yield
    pathfinder.reset_walkable_cache()


def _poll(data: dict) -> Poll:
    return Poll(
        question=f"兴建{data['name']}", status="open",
        closes_at=datetime.now(UTC) - timedelta(hours=1),
        options_json=[
            {"label": "赞成兴建", "npc_votes": 3,
             "effect": {"type": "dynamic_location", "data": data}},
            {"label": "暂缓,维持现状", "npc_votes": 0, "effect": None},
        ])


@pytest.mark.anyio
async def test_audit_is_opt_in(db_session):
    poll = _poll(POST_OFFICE_DATA)
    db_session.add(poll)
    await db_session.commit()
    await civic_service.close_due_polls(db_session)
    await db_session.refresh(poll)
    assert poll.options_json[0]["won"] is True
    assert "_effect_applied" not in poll.options_json[0], "默认关 = 不多写一个键"


@pytest.mark.anyio
async def test_success_is_recorded(db_session, monkeypatch):
    monkeypatch.setattr(settings, "civic_effect_audit_enabled", True)
    poll = _poll(POST_OFFICE_DATA)
    db_session.add(poll)
    await db_session.commit()
    await civic_service.close_due_polls(db_session)
    await db_session.refresh(poll)
    assert poll.options_json[0]["_effect_applied"] is True
    assert poll.options_json[0]["_effect_error"] is None


@pytest.mark.anyio
async def test_geometry_rejection_is_distinguishable(db_session, monkeypatch):
    """「选址不合规」不许和「DB 炸了」说成同一件事。"""
    monkeypatch.setattr(settings, "civic_effect_audit_enabled", True)
    monkeypatch.setattr(settings, "civic_build_validate_enabled", True)
    poll = _poll(THEATER_DATA)
    db_session.add(poll)
    await db_session.commit()
    await civic_service.close_due_polls(db_session)
    await db_session.refresh(poll)
    assert poll.options_json[0]["won"] is True, "胜出仍然成立,只是没落地"
    assert poll.options_json[0]["_effect_applied"] is False
    assert poll.options_json[0]["_effect_error"] == "invalid_geometry"
    assert (await db_session.execute(
        select(DynamicLocation).where(DynamicLocation.slug == "theater")
    )).scalar_one_or_none() is None


@pytest.mark.anyio
async def test_unsupported_type_reports_its_own_code(db_session):
    audit: dict = {}
    ok = await civic_service._execute_outcome(
        db_session, {"type": "teleport"}, audit=audit)
    assert ok is False
    assert audit["error"] == "unsupported_type"
    assert audit["etype"] == "teleport"


@pytest.mark.anyio
async def test_missing_bounds_reports_schema_rejected(db_session):
    audit: dict = {}
    ok = await civic_service._execute_outcome(
        db_session, {"type": "dynamic_location", "data": {"slug": "x"}},
        audit=audit)
    assert ok is False
    assert audit["error"] == "schema_rejected"


@pytest.mark.anyio
async def test_audit_blob_never_leaks_to_the_public_option(db_session, monkeypatch):
    """options_json 出网是白名单投影,新键不该有任何一条出得去。"""
    monkeypatch.setattr(settings, "civic_effect_audit_enabled", True)
    from app.services.script_service import public_option
    opt = {"label": "赞成兴建", "npc_votes": 3, "effect": {"type": "x"},
           "_effect_applied": False, "_effect_error": "invalid_geometry"}
    public = public_option(opt)
    assert "_effect_applied" not in public and "_effect_error" not in public
