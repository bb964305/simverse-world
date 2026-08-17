"""P1-S8: 能力 → 地点的反查(capability_locations / nearest_capability_location)。

nearest_capability_location 的实现体与 nearest_indoor_location(map_data.py)同构:
entrance or center、曼哈顿距离、严格 <(并列取插入序先者)。**多一层可达性过滤**:闸开
时候选 entrance 必须落在 pathfinder.get_reachable_tiles() 内 —— 不可达目标会让
satiety 危急者永久空转(find_path 恒 None → status=idle,每 tick 重跑同一条路且吃掉
一格日行动配额)。闸关时逐字节旧口径(纯曼哈顿)。

对 dining 的等价对拍必须传 exclude_types=() —— 旧 nearest_dining_location 不排除
private/apartment(与 nearest_indoor_location 不对称,这是既有事实,不得顺手修)。

坐标约定:本文件所有「应当被选中」的临时地点都放在 walkable 域
(WALKABLE_X_RANGE=range(14,174) × WALKABLE_Y_RANGE=range(12,124))内且实测与 town hub
连通;只有 t_island 故意放在域外 —— 它 forced-walkable 但不连通,与生产 theater 同型。
"""
from pathlib import Path

import pytest

from app.agent.location_caps import CAP_DINING, CAP_MARKET, CAP_RESEARCH
from app.agent.map_data import (
    LOCATIONS,
    capability_locations,
    nearest_capability_location,
    nearest_dining_location,
)
from app.config import settings


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setattr(settings, "location_capabilities_enabled", True)


@pytest.fixture
def temp_location():
    added: list[str] = []

    def _add(slug: str, extra: dict) -> str:
        assert slug not in LOCATIONS, slug
        data = {"name": "临时", "type": "public",
                "bounds": (2, 2, 6, 6), "center": (4, 4), "entrance": (4, 4)}
        data.update(extra)
        LOCATIONS[slug] = data
        added.append(slug)
        return slug

    yield _add
    for slug in added:
        LOCATIONS.pop(slug, None)


def test_dining_lookup_is_the_two_authored_diners_in_insertion_order():
    """LOCATIONS 字面量里 tavern 在 cafe 之前。"""
    assert capability_locations(CAP_DINING, exclude_types=()) == ["tavern", "cafe"]
    assert capability_locations(CAP_DINING) == ["tavern", "cafe"]


def test_research_and_market_lookups():
    assert capability_locations(CAP_RESEARCH) == ["experiment_building"]
    assert capability_locations(CAP_MARKET) == ["market_hall"]


def test_unknown_capability_yields_an_empty_list():
    assert capability_locations("nope") == []


def test_private_and_apartment_are_excluded_by_default(temp_location):
    """避免把居民往别人家门口送(festival_draw_target 用全量候选就有这毛病)。"""
    temp_location("t_home_kitchen", {"type": "private", "capacity": 1,
                                    "capabilities": {CAP_DINING: {}}})
    assert "t_home_kitchen" not in capability_locations(CAP_DINING)
    assert "t_home_kitchen" in capability_locations(CAP_DINING, exclude_types=())


@pytest.mark.parametrize("tile", [
    (75, 56), (46, 103), (16, 20), (172, 45), (116, 79), (0, 0), (179, 127),
])
def test_nearest_dining_matches_the_legacy_helper(tile):
    assert nearest_capability_location(
        tile, CAP_DINING, exclude_types=()) == nearest_dining_location(tile)


def test_nearest_prefers_entrance(temp_location):
    """entrance 优先于 center:t_decoy 的入口(距离 10)专门用来打掉「读 center」的
    实现 —— 那样 t_far_center 会以 center 距离 20 输给它。"""
    temp_location("t_far_center", {"bounds": (16, 20, 56, 22),
                                  "center": (36, 21), "entrance": (16, 20),
                                  "capabilities": {CAP_MARKET: {}}})
    temp_location("t_decoy", {"bounds": (15, 30, 17, 32),
                             "center": (16, 31), "entrance": (16, 31),
                             "capabilities": {CAP_MARKET: {}}})
    assert nearest_capability_location((16, 21), CAP_MARKET) == "t_far_center"


def test_nearest_falls_back_to_center_without_entrance(temp_location):
    temp_location("t_no_entrance", {"bounds": (16, 20, 18, 22),
                                   "center": (17, 21),
                                   "capabilities": {CAP_RESEARCH: {}}})
    LOCATIONS["t_no_entrance"].pop("entrance", None)
    assert nearest_capability_location((17, 21), CAP_RESEARCH) == "t_no_entrance"


def test_unreachable_declaration_loses_to_a_farther_reachable_one(
        temp_location, monkeypatch):
    """孤岛目标会让 satiety 危急者永久空转(find_path 恒 None → status=idle,每 tick
    重跑同一条不可达路线且吃掉一格日行动配额)。必须让位给更远但可达的 cafe。"""
    from app.agent import pathfinder
    temp_location("t_island", {"bounds": (2, 2, 6, 6), "center": (4, 4),
                              "entrance": (4, 4),
                              "capabilities": {CAP_DINING: {}}})
    pathfinder.reset_walkable_cache()
    try:
        # 前提证据:forced-walkable 会自证成功,只有连通分量能戳穿它。
        assert (4, 4) in pathfinder.get_walkable_tiles()
        assert (4, 4) not in pathfinder.get_reachable_tiles()
        got = nearest_capability_location((4, 5), CAP_DINING, exclude_types=())
        assert got in ("cafe", "tavern") and got != "t_island"
    finally:
        LOCATIONS.pop("t_island", None)
        pathfinder.reset_walkable_cache()


def test_reachability_filter_is_off_when_the_flag_is_off(
        temp_location, monkeypatch):
    """闸关 = 逐字节旧口径(纯曼哈顿,不查可达性)。"""
    monkeypatch.setattr(settings, "location_capabilities_enabled", False)
    temp_location("t_island", {"bounds": (2, 2, 6, 6), "center": (4, 4),
                              "entrance": (4, 4),
                              "capabilities": {CAP_DINING: {}}})
    assert nearest_capability_location(
        (4, 5), CAP_DINING, exclude_types=()) == "t_island"


def test_nearest_dining_delegates_and_kills_the_priority_asymmetry(
        temp_location):
    """闸开后「去哪吃」与「能不能吃」同口径:显式 category 键不再把一条声明了
    dining 的地点踢出候选集。"""
    temp_location("t_lodge", {"category": "lodging",
                             "bounds": (58, 58, 62, 62), "center": (60, 60),
                             "entrance": (60, 60),
                             "capabilities": {CAP_DINING: {}}})
    assert nearest_dining_location((60, 60)) == "t_lodge"


def test_nearest_returns_none_when_nothing_declares_it():
    assert nearest_capability_location((75, 56), "nope") is None


def test_ties_go_to_the_earlier_insertion(temp_location):
    temp_location("t_a", {"bounds": (50, 61, 52, 63), "center": (51, 62),
                         "entrance": (51, 62),
                         "capabilities": {CAP_MARKET: {}}})
    temp_location("t_b", {"bounds": (50, 61, 52, 63), "center": (51, 62),
                         "entrance": (51, 62),
                         "capabilities": {CAP_MARKET: {}}})
    assert nearest_capability_location((51, 62), CAP_MARKET) == "t_a"


def test_decide_has_a_reserved_seat_comment_for_p2():
    """P1 只留座位不落分支:_maybe_capability_errand 需要真实消费者才可行为验证,
    提前落地就是无法测行为的死码。"""
    src = (Path(__file__).resolve().parents[1]
           / "app" / "agent" / "phases" / "decide" / "basic.py")
    text = src.read_text(encoding="utf-8")
    assert "_maybe_capability_errand" in text
    assert "skip_decide_when_planned" in text
