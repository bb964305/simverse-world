"""P3 ①⑤:公投落库点 _add_dynamic_location 的净化与校验接线。"""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.dynamic_location import DynamicLocation
from app.services import civic_service

POST_OFFICE_DATA = {
    "slug": "post_office", "name": "邮局", "type": "public", "role": "logistics",
    "bounds": [44, 100, 48, 106], "center": [46, 103], "entrance": [46, 100],
    "description": "小镇邮局:寄信、收件、时间胶囊的中转站",
    "boosted_actions": ["WORK"],
}


@pytest.fixture(autouse=True)
def no_world_reload(monkeypatch):
    """_add_dynamic_location 尾部会 reload_world + publish(懒 import),测试里
    不该去碰全局 engine 与 Redis。"""
    monkeypatch.setattr("app.lab.apply.reload_world", AsyncMock(return_value=0))
    monkeypatch.setattr("app.lab.apply.publish_world_reload", AsyncMock())


async def _stored(db, slug="post_office"):
    return (await db.execute(
        select(DynamicLocation).where(DynamicLocation.slug == slug)
    )).scalar_one_or_none()


@pytest.mark.anyio
async def test_gate_off_persists_the_payload_verbatim(db_session):
    data = {**POST_OFFICE_DATA, "wallet": 999, "boosted_actions": ["DANCE"]}
    assert await civic_service._add_dynamic_location(db_session, data) is True
    row = await _stored(db_session)
    assert row.data_json["wallet"] == 999
    assert row.data_json["boosted_actions"] == ["DANCE"]
    assert "slug" not in row.data_json


@pytest.mark.anyio
async def test_gate_on_strips_unknown_keys_and_bogus_actions(db_session, monkeypatch):
    monkeypatch.setattr(settings, "civic_build_schema_enabled", True)
    data = {**POST_OFFICE_DATA, "wallet": 999,
            "boosted_actions": ["WORK", "DANCE"]}
    assert await civic_service._add_dynamic_location(db_session, data) is True
    row = await _stored(db_session)
    assert "wallet" not in row.data_json
    assert row.data_json["boosted_actions"] == ["WORK"]
    assert "slug" not in row.data_json


@pytest.mark.anyio
async def test_gate_on_backfills_missing_type(db_session, monkeypatch):
    """缺 type 的一行会让计划 prompt 的 loc['type'] 硬下标打爆全镇 planner。"""
    monkeypatch.setattr(settings, "civic_build_schema_enabled", True)
    data = {k: v for k, v in POST_OFFICE_DATA.items() if k != "type"}
    assert await civic_service._add_dynamic_location(db_session, data) is True
    assert (await _stored(db_session)).data_json["type"] == "public"


@pytest.mark.anyio
async def test_gate_on_still_rejects_a_payload_without_bounds(db_session, monkeypatch):
    monkeypatch.setattr(settings, "civic_build_schema_enabled", True)
    assert await civic_service._add_dynamic_location(
        db_session, {"slug": "x", "name": "X"}) is False
    assert await _stored(db_session, "x") is None
