"""P3:商队路面语义必须与 NPC 地点具体性解耦。

_caravan_tile_allowed 用 get_location_id_at 判开放路面 —— 集市大道
x∈[100,104] 穿过 south_quarter(42,100,135,109),今天安全全靠遮蔽。
"""
import pytest

from app.agent import map_data
from app.config import settings
from app.services import caravan_route

CORRIDOR_TILE = (102, 104)   # 大道 ∩ south_quarter


@pytest.fixture
def kiosk_in_the_corridor():
    snap = {k: dict(v) for k, v in map_data.LOCATIONS.items()}
    snap_dyn = set(map_data._dynamic_slugs)
    map_data.LOCATIONS["kiosk"] = {"name": "报刊亭", "type": "public",
                                   "bounds": (100, 102, 104, 106),
                                   "center": (102, 104),
                                   "entrance": (102, 102)}
    map_data._dynamic_slugs.add("kiosk")
    map_data.rebuild_bounds_order()
    caravan_route.build_caravan_route.cache_clear()
    yield
    map_data.LOCATIONS.clear()
    map_data.LOCATIONS.update(snap)
    map_data._dynamic_slugs = snap_dyn
    map_data.rebuild_bounds_order()
    caravan_route.build_caravan_route.cache_clear()


def test_outdoor_container_ignores_buildings(kiosk_in_the_corridor):
    """新查表永远只认 outdoor 容器,不受具体性优先影响。"""
    assert map_data.outdoor_container_at(*CORRIDOR_TILE) == "south_quarter"
    assert map_data.outdoor_container_at(20, 20) is None   # academy 不是地面
    assert map_data.outdoor_container_at(0, 0) is None


def test_corridor_survives_a_building_with_the_gate_on(
        kiosk_in_the_corridor, monkeypatch):
    monkeypatch.setattr(settings, "location_specific_first_enabled", True)
    assert map_data.get_location_id_at(*CORRIDOR_TILE) == "kiosk"
    assert caravan_route._caravan_tile_allowed(CORRIDOR_TILE) is True
    caravan_route.build_caravan_route()   # 不抛 = 路网没断链


def test_a_building_outside_any_outdoor_block_is_still_refused():
    assert caravan_route._caravan_tile_allowed((20, 20)) is False


def test_route_is_identical_across_the_gate(kiosk_in_the_corridor, monkeypatch):
    caravan_route.build_caravan_route.cache_clear()
    off = caravan_route.build_caravan_route()
    off_path, off_park = off.full_path, off.market_hall_parking
    monkeypatch.setattr(settings, "location_specific_first_enabled", True)
    caravan_route.build_caravan_route.cache_clear()
    on = caravan_route.build_caravan_route()
    assert on.full_path == off_path
    assert on.market_hall_parking == off_park
