"""P1 基线冻结:后续 step 的等价性对拍全部引用这里的数字。

本文件不测试新行为 —— 它把 plan 前言里的实测基线钉成机器可验的断言,并且必须在
P1 十步全部落地后仍然全绿。任一条红 = 前序认知有误,停下修正 plan,不要改期望值。
"""
import pytest

from app.agent.actions import ActionType
from app.agent.map_data import (
    LOCATIONS,
    _DINING_LOCATIONS,
    format_location_list_for_prompt,
    location_category,
)
from app.world_geometry import WALKABLE_X_RANGE, WALKABLE_Y_RANGE

#: P1-S4 将要声明能力的四条静态地点。
DECLARED_SLUGS = ("cafe", "tavern", "experiment_building", "market_hall")


def test_static_location_count_is_thirty_four():
    assert len(LOCATIONS) == 34


def test_no_static_entry_carries_an_explicit_category_key():
    """P1 全程不给任何静态条目写 category 键(能力派生走 capabilities)。
    这条在 S3/S4 之后仍必须为真 —— 它是「显式 category 优先」那一级在 P1 内
    不被触发的机器保证。"""
    for slug, loc in LOCATIONS.items():
        assert "category" not in loc, slug


def test_dining_today_is_exactly_cafe_and_tavern():
    assert _DINING_LOCATIONS == {"cafe", "tavern"}
    assert {s for s in LOCATIONS
            if location_category(s) == "dining"} == {"cafe", "tavern"}


@pytest.mark.parametrize("slug", DECLARED_SLUGS)
def test_the_four_declared_slugs_have_zero_bounds_overlap(slug):
    """S5/S6 的「最小面积匹配 == 首命中」等价性依赖这条。"""
    ax1, ay1, ax2, ay2 = LOCATIONS[slug]["bounds"]
    for other, loc in LOCATIONS.items():
        if other == slug:
            continue
        bx1, by1, bx2, by2 = loc["bounds"]
        assert (ax2 < bx1 or bx2 < ax1 or ay2 < by1 or by2 < ay1), (slug, other)


def test_action_type_enum_baseline():
    """P1 是门的数据化,不是新动作。tests/test_lab_building.py:85-88 同款。"""
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH
    assert actions[15] == ActionType.EAT


def test_walkable_domain_baseline():
    assert (WALKABLE_X_RANGE.start, WALKABLE_X_RANGE.stop - 1) == (14, 173)
    assert (WALKABLE_Y_RANGE.start, WALKABLE_Y_RANGE.stop - 1) == (12, 123)


def test_plan_prompt_never_mentions_capabilities():
    """format_location_list_for_prompt 不读该键 → 计划 prompt 逐字节不变
    (tests/test_plan_public_memories.py 的冻结快照因此安全)。P1 全程为真。"""
    text = format_location_list_for_prompt()
    assert "capabilit" not in text
    assert "host_duty" not in text
