"""P3 ⑤:PLACES_LIMIT 下给新建动态楼留名额。

_read_places 的 docstring 自陈「被条数上限挤掉的总是新加的动态地点」——
9 静态 public + 上限 12,再建 2 栋就开始挤。
"""
from datetime import datetime, timedelta, UTC

import pytest

from app.config import settings
from app.models.dynamic_location import DynamicLocation
from app.services import town_facts_service as tfs
from app.services import world_event_service


@pytest.fixture(autouse=True)
def _clean_caches():
    tfs._reset_for_tests()
    world_event_service.invalidate_active_cache()
    yield
    tfs._reset_for_tests()
    world_event_service.invalidate_active_cache()


@pytest.fixture
def facts_on(monkeypatch):
    monkeypatch.setattr(settings, "civic_facts_enabled", True)


def _dyn(slug: str, name: str, *, days_ago: int) -> DynamicLocation:
    return DynamicLocation(
        slug=slug, active=True,
        data_json={"name": name, "type": "public", "bounds": [0, 0, 1, 1]},
        created_at=datetime.now(UTC) - timedelta(days=days_ago))


async def _places(db):
    return (await tfs.get_town_facts_cached(db))["places"]


@pytest.mark.anyio
async def test_reserve_zero_is_byte_identical(db_session, facts_on):
    """默认 0 = 旧行为:静态占满,新楼被挤掉。"""
    db_session.add_all([_dyn(f"zz-{i:03d}", f"新楼{i:03d}", days_ago=0)
                        for i in range(6)])
    await db_session.commit()
    places = await _places(db_session)
    assert len(places) == tfs.PLACES_LIMIT
    assert "市政厅" in places
    assert places[:9] == ["学院", "酒馆", "咖啡馆", "工坊", "图书馆",
                          "杂货铺", "市政厅", "实验楼", "集市大厅"]


@pytest.mark.anyio
async def test_reserve_keeps_the_newest_buildings(db_session, facts_on, monkeypatch):
    monkeypatch.setattr(settings, "civic_facts_places_dynamic_reserve", 2)
    db_session.add_all(
        [_dyn(f"aa-{i:03d}", f"老楼{i:03d}", days_ago=30 + i) for i in range(6)]
        + [_dyn("zz-new1", "新楼甲", days_ago=1),
           _dyn("zz-new2", "新楼乙", days_ago=0)])
    await db_session.commit()
    places = await _places(db_session)
    assert len(places) == tfs.PLACES_LIMIT
    assert "新楼甲" in places and "新楼乙" in places
    assert "市政厅" in places, "保留位不许把静态公共设施整段顶掉"
    assert places[:9] == ["学院", "酒馆", "咖啡馆", "工坊", "图书馆",
                          "杂货铺", "市政厅", "实验楼", "集市大厅"], \
        "渲染顺序仍是静态在前(prompt 前缀稳定),只有名额分配先给动态"


@pytest.mark.anyio
async def test_unused_reserve_is_returned_to_static(db_session, facts_on, monkeypatch):
    """没填满的坑退还 —— len(places) 与 reserve=0 时恒等。"""
    monkeypatch.setattr(settings, "civic_facts_places_dynamic_reserve", 5)
    db_session.add(_dyn("zz-new1", "新楼甲", days_ago=0))
    await db_session.commit()
    places = await _places(db_session)
    assert len(places) == 10 == 9 + 1
    assert "市政厅" in places and "新楼甲" in places


@pytest.mark.anyio
async def test_reserve_does_not_double_count_a_merged_building(
        db_session, facts_on, monkeypatch):
    monkeypatch.setattr(settings, "civic_facts_places_dynamic_reserve", 2)
    from app.agent.map_data import LOCATIONS
    monkeypatch.setitem(LOCATIONS, "theater", {
        "name": "剧院", "type": "public", "bounds": (172, 40, 178, 50)})
    db_session.add(_dyn("theater", "剧院", days_ago=0))
    await db_session.commit()
    places = await _places(db_session)
    assert places.count("剧院") == 1


@pytest.mark.anyio
async def test_reserve_still_respects_the_char_cap(db_session, facts_on, monkeypatch):
    monkeypatch.setattr(settings, "civic_facts_places_dynamic_reserve", 2)
    db_session.add(_dyn("zz-long", "楼" * 200, days_ago=0))
    await db_session.commit()
    for name in await _places(db_session):
        assert len(name) <= tfs.PLACE_MAX_CHARS
