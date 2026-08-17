"""P1-S2: map_data 侧的能力查询(纯读,无闸,本 step 内零生产调用方)。

核心不变量:has_capability(x,dining) 是 (location_category(x)==dining) 的超集。
这条保证任何既有 category 驱动的行为(actions.py:137 的 EAT 门 /
map_data.nearest_dining_location)不可能因为忘写声明而丢失 —— 它是 capability 与
category 共存而非取代的机器化表述。
"""
import pytest

from app.agent import map_data
from app.agent.location_caps import (
    CAP_DINING,
    CAP_MARKET,
    CAP_POSTAL,
    CAP_RESEARCH,
)
from app.agent.map_data import (
    LOCATIONS,
    capability_param,
    has_capability,
    location_capabilities,
    location_category,
)


@pytest.fixture
def temp_location():
    """往 LOCATIONS 临时塞一条并保证还原(LOCATIONS 是可变全局)。"""
    added: list[str] = []

    def _add(slug: str, extra: dict) -> str:
        assert slug not in LOCATIONS, slug
        data = {"name": "临时", "type": "public",
                "bounds": (0, 0, 1, 1), "center": (0, 0)}
        data.update(extra)
        LOCATIONS[slug] = data
        added.append(slug)
        return slug

    yield _add
    for slug in added:
        LOCATIONS.pop(slug, None)


@pytest.fixture
def temp_dynamic_location(temp_location):
    """临时地点 + 登记进 _dynamic_slugs —— 公投建楼落库后的真实形态。"""
    registered: list[str] = []

    def _add(slug: str, extra: dict) -> str:
        temp_location(slug, extra)
        map_data._dynamic_slugs.add(slug)
        registered.append(slug)
        return slug

    yield _add
    for slug in registered:
        map_data._dynamic_slugs.discard(slug)


def test_dining_category_always_implies_the_dining_capability():
    for loc_id in list(LOCATIONS):
        if location_category(loc_id) == "dining":
            assert has_capability(loc_id, CAP_DINING), loc_id


def test_cafe_and_tavern_carry_the_dining_capability():
    assert location_capabilities("cafe") == frozenset({CAP_DINING})
    assert location_capabilities("tavern") == frozenset({CAP_DINING})


def test_undeclared_locations_have_no_capabilities():
    assert location_capabilities("academy") == frozenset()
    assert location_capabilities("central_plaza") == frozenset()


def test_missing_and_none_ids_are_default_safe():
    assert location_capabilities(None) == frozenset()
    assert location_capabilities("") == frozenset()
    assert location_capabilities("no_such_place") == frozenset()
    assert has_capability(None, CAP_DINING) is False
    assert has_capability("no_such_place", CAP_RESEARCH) is False


def test_explicit_declaration_is_read(temp_location):
    temp_location("t_lab", {"capabilities": {"research": {}}})
    assert location_capabilities("t_lab") == frozenset({CAP_RESEARCH})
    assert has_capability("t_lab", CAP_RESEARCH) is True
    assert has_capability("t_lab", CAP_DINING) is False


def test_loose_list_declaration_is_read(temp_location):
    temp_location("t_market", {"capabilities": ["market"]})
    assert location_capabilities("t_market") == frozenset({CAP_MARKET})


def test_unknown_capability_names_are_dropped_not_crashed(temp_location):
    temp_location("t_junk", {"capabilities": {"DANCE": {}, "dining": {}}})
    assert location_capabilities("t_junk") == frozenset({CAP_DINING})


def test_legacy_row_without_the_key_is_inert(temp_location):
    """生产两栋公投楼(post_office/theater)的 data_json 就是这个形状。"""
    temp_location("t_legacy", {"name": "邮局", "role": "logistics",
                              "boosted_actions": ["WORK"]})
    assert location_capabilities("t_legacy") == frozenset()
    assert has_capability("t_legacy", CAP_DINING) is False


def test_capability_param_reads_declared_params(temp_location):
    temp_location("t_diner", {"capabilities": {"dining": {"host_duty": "t_host"}}})
    assert capability_param("t_diner", CAP_DINING, "host_duty") == "t_host"


def test_capability_param_falls_back_to_default_everywhere_it_can(temp_location):
    temp_location("t_noparam", {"capabilities": {"dining": {}}})
    assert capability_param("t_noparam", CAP_DINING, "host_duty", "fb") == "fb"
    assert capability_param("academy", CAP_DINING, "host_duty") is None
    assert capability_param("no_such_place", CAP_DINING, "host_duty") is None
    assert capability_param(None, CAP_DINING, "host_duty", "fb") == "fb"


def test_capability_param_treats_explicit_null_as_absent(temp_location):
    """公投 effect.data 里写 host_duty: null 不得被当成合法 duty key。"""
    temp_location("t_null", {"capabilities": {"dining": {"host_duty": None}}})
    assert capability_param("t_null", CAP_DINING, "host_duty", "fb") == "fb"


def test_civic_built_location_cannot_grant_research(temp_dynamic_location):
    """公投永远不能授予 research —— 否则一次公投绕过实验楼地点门。

    routers/polls.py 允许 admin 附带任意 effect dict,_add_dynamic_location 只校验
    slug 非空 + bounds 在 data 里就整包落库,所以白名单必须在读侧执行。
    """
    temp_dynamic_location("t_civic_lab", {"capabilities": {"research": {}}})
    assert has_capability("t_civic_lab", CAP_RESEARCH) is False
    assert location_capabilities("t_civic_lab") == frozenset()
    assert capability_param("t_civic_lab", CAP_RESEARCH, "anything") is None


def test_civic_built_location_can_grant_postal(temp_dynamic_location):
    """白名单内的能力(P2 邮局侧正向通路)不得被降级误伤。"""
    temp_dynamic_location("t_civic_post", {"capabilities": {"postal": {}}})
    assert has_capability("t_civic_post", CAP_POSTAL) is True
    assert location_capabilities("t_civic_post") == frozenset({CAP_POSTAL})
