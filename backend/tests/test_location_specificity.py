"""P3 ④d:楼压在 outdoor 街区上时,坐标反查必须认出楼。

实测 get_location_id_at(46,100) 返 south_quarter、(172,45) 返 east_gardens ——
首命中 = dict 插入序,6 条 outdoor 排在静态字面量索引 28-33,动态楼追加在 34+。
"""
import pytest

from app.agent import map_data
from app.config import settings
from app.services import location_tracker

POST_OFFICE = {"name": "邮局", "type": "public", "bounds": (44, 100, 48, 106),
               "center": (46, 103), "entrance": (46, 100)}
THEATER = {"name": "剧院", "type": "public", "bounds": (172, 40, 178, 50),
           "center": (175, 45), "entrance": (172, 45)}


@pytest.fixture
def merged_buildings():
    """把生产那两栋动态楼按真实 data_json 并进内存(追加在尾部,与
    load_dynamic_locations:386 同形)。"""
    snap = {k: dict(v) for k, v in map_data.LOCATIONS.items()}
    snap_dyn = set(map_data._dynamic_slugs)
    map_data.LOCATIONS["post_office"] = dict(POST_OFFICE)
    map_data.LOCATIONS["theater"] = dict(THEATER)
    map_data._dynamic_slugs |= {"post_office", "theater"}
    map_data.rebuild_bounds_order()
    location_tracker.rebuild_lookup()
    yield
    map_data.LOCATIONS.clear()
    map_data.LOCATIONS.update(snap)
    map_data._dynamic_slugs = snap_dyn
    map_data.rebuild_bounds_order()
    location_tracker.rebuild_lookup()


def test_gate_off_reproduces_the_shadowing(merged_buildings):
    assert map_data.get_location_id_at(46, 100) == "south_quarter"
    assert map_data.get_location_id_at(172, 45) == "east_gardens"


def test_gate_on_resolves_the_building(merged_buildings, monkeypatch):
    monkeypatch.setattr(settings, "location_specific_first_enabled", True)
    assert map_data.get_location_id_at(46, 100) == "post_office"
    assert map_data.get_location_id_at(46, 103) == "post_office"
    assert map_data.get_location_id_at(172, 45) == "theater"
    assert map_data.get_location_id_at(175, 45) == "theater"


def test_gate_on_does_not_disturb_non_overlapping_tiles(merged_buildings, monkeypatch):
    monkeypatch.setattr(settings, "location_specific_first_enabled", True)
    assert map_data.get_location_id_at(20, 20) == "academy"
    assert map_data.get_location_id_at(75, 56) == "central_plaza"
    assert map_data.get_location_id_at(0, 0) is None


def test_tracker_index_stays_in_sync_with_the_finder(merged_buildings, monkeypatch):
    """location_tracker 的 setdefault 表必须与 get_location_id_at 同序 ——
    两处不同序会让玩家首访与 NPC 认出不同的楼。"""
    monkeypatch.setattr(settings, "location_specific_first_enabled", True)
    location_tracker.rebuild_lookup()
    for tile in ((46, 100), (46, 103), (172, 45), (175, 45), (20, 20), (75, 56)):
        assert location_tracker.location_at_tile(*tile) == \
            map_data.get_location_id_at(*tile), f"{tile} 两套索引对不上"


def test_lore_for_the_two_voted_buildings_becomes_reachable(merged_buildings, monkeypatch):
    """location_lore.py:21-22 那两段专门为公投新楼写的文案今天是死文案。"""
    from app.agent.location_lore import lore_for
    monkeypatch.setattr(settings, "location_specific_first_enabled", True)
    for tile in ((46, 103), (172, 45)):
        loc_id = map_data.get_location_id_at(*tile)
        assert lore_for(loc_id), f"{loc_id} 的 lore 仍然取不到"


def test_specificity_order_puts_buildings_before_outdoor(merged_buildings):
    order = [k for k, _ in map_data._specificity_items()]
    assert order.index("post_office") < order.index("south_quarter")
    assert order.index("theater") < order.index("east_gardens")


def test_swapping_one_building_for_another_keeps_the_index_honest(
        merged_buildings, monkeypatch):
    """删一条同时加一条:长度不变、键集变了。长度守卫会让新楼整栋从坐标反查里
    消失(get_location_id_at 返 None),而这正是 load_dynamic_locations 每次
    reload 的形状(先 pop 全部动态 slug 再 merge 本轮)。"""
    monkeypatch.setattr(settings, "location_specific_first_enabled", True)
    before = len(map_data.LOCATIONS)
    map_data.LOCATIONS.pop("theater")
    map_data.LOCATIONS["gallery"] = {"name": "画廊", "type": "public",
                                     "bounds": (172, 40, 178, 50),
                                     "center": (175, 45), "entrance": (172, 45)}
    map_data._dynamic_slugs.discard("theater")
    map_data._dynamic_slugs.add("gallery")
    assert len(map_data.LOCATIONS) == before, "这条测试的前提就是条数不变"
    # 刻意不调 rebuild_bounds_order():惰性守卫必须自己发现键集变了
    assert map_data.get_location_id_at(175, 45) == "gallery"
    location_tracker.rebuild_lookup()
    assert location_tracker.location_at_tile(175, 45) == "gallery"


def test_specificity_items_is_cached_between_calls(merged_buildings):
    """每次调用重建 34 元组列表实测 20000 次 25.2ms;caravan 全图扫描是大户。"""
    first = map_data._specificity_items()
    assert map_data._specificity_items() is first
    map_data.LOCATIONS["annex"] = {"name": "侧厅", "type": "public",
                                   "bounds": (60, 60, 62, 62)}
    assert map_data._specificity_items() is not first, "键集变了必须重建"
