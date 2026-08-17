"""P1-S4: 四条静态能力声明 + 防漂移守卫 + 闸开闸关的集合级等价。

守卫只锁静态集合(_STATIC_LOCATION_SLUGS):既防代码误声明第二个 research 地点,
又不挡 P3 的公投建楼在数据侧扩展 dining。
"""
import inspect

import pytest

from app.agent.location_caps import CAP_DINING, CAP_MARKET, CAP_RESEARCH
from app.agent.map_data import (
    LOCATIONS,
    _STATIC_LOCATION_SLUGS,
    capability_param,
    format_location_list_for_prompt,
    has_capability,
    location_capabilities,
    location_category,
)
from app.config import settings

DECLARED = {
    "cafe": {CAP_DINING: {"host_duty": "cafe_host"}},
    "tavern": {CAP_DINING: {"host_duty": "tavern_hub"}},
    "experiment_building": {CAP_RESEARCH: {}},
    "market_hall": {CAP_MARKET: {}},
}


def test_static_slug_snapshot_matches_the_literal():
    assert isinstance(_STATIC_LOCATION_SLUGS, frozenset)
    assert len(_STATIC_LOCATION_SLUGS) == 34
    assert _STATIC_LOCATION_SLUGS <= set(LOCATIONS)
    for slug in DECLARED:
        assert slug in _STATIC_LOCATION_SLUGS


@pytest.mark.parametrize("slug,expected", sorted(DECLARED.items()))
def test_the_four_declarations_have_the_exact_shape(slug, expected):
    assert LOCATIONS[slug]["capabilities"] == expected


def test_exactly_four_static_locations_declare_anything():
    declared = {s for s in _STATIC_LOCATION_SLUGS
                if LOCATIONS.get(s, {}).get("capabilities")}
    assert declared == set(DECLARED)


def test_only_experiment_building_declares_research_statically():
    """第二个 research 地点 = 绕过实验楼地点门。这条守卫防代码误声明。"""
    got = {s for s in _STATIC_LOCATION_SLUGS if has_capability(s, CAP_RESEARCH)}
    assert got == {"experiment_building"}


def test_only_cafe_and_tavern_declare_dining_statically():
    got = {s for s in _STATIC_LOCATION_SLUGS if has_capability(s, CAP_DINING)}
    assert got == {"cafe", "tavern"}


def test_only_market_hall_declares_market_statically():
    got = {s for s in _STATIC_LOCATION_SLUGS if has_capability(s, CAP_MARKET)}
    assert got == {"market_hall"}


def test_dining_capability_set_equals_the_legacy_category_set(monkeypatch):
    for state in (False, True):
        monkeypatch.setattr(settings, "location_capabilities_enabled", state)
        by_cap = {s for s in _STATIC_LOCATION_SLUGS if has_capability(s, CAP_DINING)}
        by_cat = {s for s in _STATIC_LOCATION_SLUGS
                  if location_category(s) == "dining"}
        assert by_cap == by_cat == {"cafe", "tavern"}, state


def test_host_duty_params_match_the_legacy_hardcoded_pair():
    """execute/basic.py:56 旧式:cafe→cafe_host,其余 dining→tavern_hub。"""
    assert capability_param("cafe", CAP_DINING, "host_duty") == "cafe_host"
    assert capability_param("tavern", CAP_DINING, "host_duty") == "tavern_hub"


@pytest.mark.parametrize("slug", sorted(DECLARED))
def test_declared_locations_have_no_bounds_overlap_with_anything(slug):
    """S5/S6 的「最小面积匹配 == 首命中」等价性依赖这条。"""
    ax1, ay1, ax2, ay2 = LOCATIONS[slug]["bounds"]
    for other, loc in LOCATIONS.items():
        if other == slug:
            continue
        bx1, by1, bx2, by2 = loc["bounds"]
        overlap = not (ax2 < bx1 or bx2 < ax1 or ay2 < by1 or by2 < ay1)
        assert not overlap, f"{slug} overlaps {other}"


def test_plan_prompt_text_never_mentions_capabilities():
    """format_location_list_for_prompt 不读该键 → 计划 prompt 逐字节不变
    (tests/test_plan_public_memories.py 的冻结快照因此安全)。"""
    text = format_location_list_for_prompt()
    assert "capabilit" not in text
    assert "host_duty" not in text


def test_world_router_payload_never_leaks_capabilities():
    """routers/world.py:29-40 是白名单式序列化 → 前端契约零变更。"""
    from app.routers import world as world_router
    assert "capabilities" not in inspect.getsource(world_router.get_locations)


def test_market_capability_is_discovery_only_not_venue_resolution():
    """场地权威仍是 settings.market_day_venue + resolve_event_location_id。"""
    from app.services import event_location
    assert "capabilit" not in inspect.getsource(event_location)
    assert location_capabilities("market_hall") == frozenset({CAP_MARKET})
