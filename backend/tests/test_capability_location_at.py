"""P1-S5: capability_location_at —— 按「最具体」而非「插入序首命中」做能力反查。

生产实测:post_office(44,100,48,106) 完全落在 south_quarter(42,100,135,109) 内部,
theater(172,40,178,50) 落在 east_gardens(140,35,179,58) 内部。get_location_id_at
首命中即返 → 这两栋楼在「按坐标」这条链上等于不存在。本函数把能力门从遮蔽里摘
出来,同时不动 get_location_id_at 的契约(location_tracker 与它同序)。
"""
import pytest

from app.agent.location_caps import CAP_DINING, CAP_MARKET, CAP_RESEARCH
from app.agent.map_data import (
    LOCATIONS,
    capability_location_at,
    get_location_id_at,
)
from app.config import settings

# 生产 dynamic_locations 两行的 data_json(2026-08-01, active=t)。
POST_OFFICE = {"name": "邮局", "type": "public", "role": "logistics",
               "bounds": (44, 100, 48, 106), "center": (46, 103),
               "entrance": (46, 100),
               "description": "小镇邮局:寄信、收件、时间胶囊的中转站",
               "boosted_actions": ["WORK"]}
THEATER = {"name": "剧院", "type": "public", "role": "culture",
           "bounds": (172, 40, 178, 50), "center": (175, 45),
           "entrance": (172, 45),
           "description": "小镇剧院:说书、演展、故事会的舞台",
           "boosted_actions": ["CHAT_RESIDENT", "OBSERVE"]}


@pytest.fixture
def overlay():
    """模拟 load_dynamic_locations 的合入:追加到 LOCATIONS 尾部,再还原。"""
    added: list[str] = []

    def _merge(slug: str, data: dict, capabilities=None):
        assert slug not in LOCATIONS, slug
        row = dict(data)
        if capabilities is not None:
            row["capabilities"] = capabilities
        LOCATIONS[slug] = row
        added.append(slug)
        return slug

    yield _merge
    for slug in added:
        LOCATIONS.pop(slug, None)


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setattr(settings, "location_capabilities_enabled", True)


def test_masking_is_real_and_get_location_id_at_keeps_its_contract(overlay):
    overlay("post_office", POST_OFFICE)
    overlay("theater", THEATER)
    assert get_location_id_at(46, 100) == "south_quarter"
    assert get_location_id_at(46, 103) == "south_quarter"
    assert get_location_id_at(172, 45) == "east_gardens"
    assert get_location_id_at(175, 45) == "east_gardens"


def test_capability_lookup_sees_through_the_outdoor_mask(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_DINING: {}})
    assert capability_location_at(46, 103, CAP_DINING) == "post_office"
    assert capability_location_at(46, 100, CAP_DINING) == "post_office"


def test_undeclared_masked_building_yields_nothing(overlay):
    """P1 的动态侧迁移是 no-op:老行没有 capabilities 键 → 不解锁任何东西。"""
    overlay("theater", THEATER)
    assert capability_location_at(172, 45, CAP_DINING) is None
    assert capability_location_at(172, 45, CAP_RESEARCH) is None


@pytest.mark.parametrize("slug,cap", [
    ("cafe", CAP_DINING),
    ("tavern", CAP_DINING),
    ("experiment_building", CAP_RESEARCH),
    ("market_hall", CAP_MARKET),
])
def test_declared_statics_resolve_to_themselves_and_match_first_match(slug, cap):
    """四条声明的 bounds 与全表零重叠 → 「最小面积」==「首命中」== 它自己。"""
    loc = LOCATIONS[slug]
    for tile in (loc["center"], loc.get("entrance") or loc["center"]):
        x, y = tile
        assert capability_location_at(x, y, cap) == slug
        assert get_location_id_at(x, y) == slug


def test_capability_not_declared_returns_none_even_inside_bounds():
    cx, cy = LOCATIONS["cafe"]["center"]
    assert capability_location_at(cx, cy, CAP_RESEARCH) is None
    assert capability_location_at(cx, cy, CAP_MARKET) is None


def test_outside_every_bound_returns_none():
    assert capability_location_at(0, 0, CAP_DINING) is None
    assert capability_location_at(0, 0, CAP_RESEARCH) is None


def test_smallest_area_wins_when_two_declared_locations_overlap(overlay):
    overlay("t_big", {"name": "大", "type": "public",
                     "bounds": (0, 0, 20, 20), "center": (10, 10)},
            capabilities={CAP_DINING: {}})
    overlay("t_small", {"name": "小", "type": "public",
                       "bounds": (5, 5, 7, 7), "center": (6, 6)},
            capabilities={CAP_DINING: {}})
    assert capability_location_at(6, 6, CAP_DINING) == "t_small"
    assert capability_location_at(1, 1, CAP_DINING) == "t_big"


def test_equal_area_falls_back_to_insertion_order(overlay):
    overlay("t_first", {"name": "甲", "type": "public",
                       "bounds": (30, 0, 33, 3), "center": (31, 1)},
            capabilities={CAP_DINING: {}})
    overlay("t_second", {"name": "乙", "type": "public",
                        "bounds": (30, 0, 33, 3), "center": (31, 1)},
            capabilities={CAP_DINING: {}})
    assert capability_location_at(31, 1, CAP_DINING) == "t_first"


def test_malformed_bounds_are_skipped_not_crashed(overlay):
    """civic_service._add_dynamic_location 零几何校验 —— 畸形行不得让查询崩。"""
    overlay("t_bad", {"name": "坏", "type": "public", "bounds": [1, 2]},
            capabilities={CAP_DINING: {}})
    assert capability_location_at(0, 0, CAP_DINING) is None
    cx, cy = LOCATIONS["cafe"]["center"]
    assert capability_location_at(cx, cy, CAP_DINING) == "cafe"


def test_location_without_bounds_key_is_skipped(overlay):
    overlay("t_nobounds", {"name": "无界", "type": "public"},
            capabilities={CAP_DINING: {}})
    cx, cy = LOCATIONS["tavern"]["center"]
    assert capability_location_at(cx, cy, CAP_DINING) == "tavern"
