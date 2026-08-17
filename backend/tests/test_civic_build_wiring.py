"""P3 ①⑤:公投落库点 _add_dynamic_location 的净化与校验接线。"""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.agent import pathfinder
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


# ── 几何校验(S6) ───────────────────────────────────────────────────────

THEATER_DATA = {
    "slug": "theater", "name": "剧院", "type": "public", "role": "culture",
    "bounds": [172, 40, 178, 50], "center": [175, 45], "entrance": [172, 45],
    "description": "小镇剧院:说书、演展、故事会的舞台",
    "boosted_actions": ["CHAT_RESIDENT", "OBSERVE"],
}


@pytest.fixture
def validate_on(monkeypatch):
    monkeypatch.setattr(settings, "civic_build_validate_enabled", True)
    pathfinder.reset_walkable_cache()
    yield
    pathfinder.reset_walkable_cache()


@pytest.mark.anyio
async def test_post_office_still_lands_with_validation_on(db_session, validate_on):
    """合法楼不许被 outdoor 街区误杀 —— 这条是整个 P3 的回归红线。"""
    assert await civic_service._add_dynamic_location(
        db_session, dict(POST_OFFICE_DATA)) is True
    assert await _stored(db_session) is not None


@pytest.mark.anyio
async def test_out_of_walkable_bounds_are_refused(db_session, validate_on):
    """剧院 bounds x2=178 越过 WALKABLE_X_RANGE 上限 173。"""
    assert await civic_service._add_dynamic_location(
        db_session, dict(THEATER_DATA)) is False
    assert await _stored(db_session, "theater") is None


@pytest.mark.anyio
async def test_unreachable_entrance_is_refused(db_session, validate_on):
    data = {"slug": "observatory", "name": "天文台", "type": "public",
            "bounds": [5, 88, 15, 96], "entrance": [10, 88]}
    assert await civic_service._add_dynamic_location(db_session, data) is False
    assert await _stored(db_session, "observatory") is None


@pytest.mark.anyio
async def test_building_on_building_is_refused(db_session, validate_on):
    """楼压楼(academy 15,18,42,34)才是真冲突。"""
    data = {"slug": "annex", "name": "侧楼", "type": "public",
            "bounds": [20, 20, 30, 30], "entrance": [25, 25]}
    assert await civic_service._add_dynamic_location(db_session, data) is False
    assert await _stored(db_session, "annex") is None


@pytest.mark.anyio
async def test_gate_off_keeps_landing_the_bad_geometry(db_session):
    """闸关 = 旧行为:剧院照样落库(这就是生产今天的状态)。"""
    assert await civic_service._add_dynamic_location(
        db_session, dict(THEATER_DATA)) is True
    assert await _stored(db_session, "theater") is not None


@pytest.mark.anyio
async def test_rebuild_of_an_existing_slug_is_an_upsert(db_session, validate_on):
    """同一条 effect 重跑是覆盖写,不该被 'slug already exists' 挡住。"""
    assert await civic_service._add_dynamic_location(
        db_session, dict(POST_OFFICE_DATA)) is True
    from app.agent import map_data
    map_data.LOCATIONS["post_office"] = {**POST_OFFICE_DATA,
                                         "bounds": (44, 100, 48, 106)}
    map_data._dynamic_slugs.add("post_office")
    try:
        assert await civic_service._add_dynamic_location(
            db_session, {**POST_OFFICE_DATA, "description": "改了一句"}) is True
        assert (await _stored(db_session)).data_json["description"] == "改了一句"
    finally:
        map_data.LOCATIONS.pop("post_office", None)
        map_data._dynamic_slugs.discard("post_office")
