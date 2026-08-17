"""P1-S6: RESEARCH / EAT 的地点门改读能力声明,闸开闸关行为逐条相等。

最强的等价证明是最后那条参数化:对每一条静态地点的 center,闸开与闸关下
get_available_actions 的返回集合相等。它同时覆盖 RESEARCH 与 EAT 两个门,且不依赖
任何手写的期望表。

RESEARCH 的身份门 has_trusted_lab_access 不在能力体系内,本 step 一个字不碰。
"""
from unittest.mock import MagicMock

import pytest

from app.agent.actions import ActionType, get_available_actions
from app.agent.location_caps import CAP_DINING
from app.agent.map_data import LOCATIONS, _STATIC_LOCATION_SLUGS
from app.config import settings
from app.models.resident import Resident

POST_OFFICE = {"name": "邮局", "type": "public", "role": "logistics",
               "bounds": (44, 100, 48, 106), "center": (46, 103),
               "entrance": (46, 100), "boosted_actions": ["WORK"]}


def _resident(tile_x, tile_y, *, researcher=True):
    r = MagicMock(spec=Resident)
    r.id = "res-cap"
    r.slug = "res-cap"
    r.resident_type = "npc"
    r.creator_id = "system"          # trusted provenance
    r.status = "idle"
    r.tile_x = tile_x
    r.tile_y = tile_y
    r.home_tile_x = None
    r.home_tile_y = None
    r.home_location_id = None
    r.meta_json = ({"lab": {"access": True, "tier": "junior"}}
                   if researcher else {})
    return r


@pytest.fixture
def overlay():
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


@pytest.fixture
def realism_on(monkeypatch):
    monkeypatch.setattr(settings, "realism_enabled", True)
    return monkeypatch


@pytest.mark.parametrize("flag", [False, True])
def test_research_gate_is_identical_under_both_flag_states(flag, monkeypatch):
    monkeypatch.setattr(settings, "location_capabilities_enabled", flag)
    inside = _resident(116, 79)     # experiment_building center
    outside = _resident(76, 50)
    assert ActionType.RESEARCH in get_available_actions(inside, [])
    assert ActionType.RESEARCH not in get_available_actions(outside, [])


@pytest.mark.parametrize("flag", [False, True])
def test_identity_gate_still_rules_regardless_of_capability(flag, monkeypatch):
    """能力只收编「站在哪」那一半;非研究员站在实验楼里照样没有 RESEARCH。"""
    monkeypatch.setattr(settings, "location_capabilities_enabled", flag)
    r = _resident(116, 79, researcher=False)
    assert ActionType.RESEARCH not in get_available_actions(r, [])


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("slug", ["cafe", "tavern"])
def test_eat_is_available_at_both_diners_under_both_flag_states(
        flag, slug, realism_on):
    realism_on.setattr(settings, "location_capabilities_enabled", flag)
    cx, cy = LOCATIONS[slug]["center"]
    assert ActionType.EAT in get_available_actions(_resident(cx, cy), [])


@pytest.mark.parametrize("flag", [False, True])
def test_eat_is_unavailable_outside_a_diner_under_both_flag_states(
        flag, realism_on):
    realism_on.setattr(settings, "location_capabilities_enabled", flag)
    cx, cy = LOCATIONS["central_plaza"]["center"]
    assert ActionType.EAT not in get_available_actions(_resident(cx, cy), [])


def test_eat_stays_gated_behind_realism_master_switch(monkeypatch):
    monkeypatch.setattr(settings, "realism_enabled", False)
    monkeypatch.setattr(settings, "location_capabilities_enabled", True)
    cx, cy = LOCATIONS["cafe"]["center"]
    assert ActionType.EAT not in get_available_actions(_resident(cx, cy), [])


def test_masked_dynamic_diner_unlocks_eat_only_when_the_flag_is_on(
        overlay, realism_on):
    overlay("post_office", POST_OFFICE, capabilities={CAP_DINING: {}})
    r = _resident(46, 103)          # 站在邮局里,首命中却是 south_quarter
    realism_on.setattr(settings, "location_capabilities_enabled", False)
    assert ActionType.EAT not in get_available_actions(r, [])
    realism_on.setattr(settings, "location_capabilities_enabled", True)
    assert ActionType.EAT in get_available_actions(r, [])


def test_legacy_dynamic_row_without_declaration_unlocks_nothing(
        overlay, realism_on):
    """P1 动态侧迁移是 no-op 的机器证明。"""
    overlay("post_office", POST_OFFICE)
    realism_on.setattr(settings, "location_capabilities_enabled", True)
    avail = get_available_actions(_resident(46, 103), [])
    assert ActionType.EAT not in avail
    assert ActionType.RESEARCH not in avail


@pytest.mark.parametrize("slug", sorted(_STATIC_LOCATION_SLUGS))
def test_available_actions_are_identical_with_the_flag_on_and_off(
        slug, realism_on):
    cx, cy = LOCATIONS[slug]["center"]
    r = _resident(cx, cy)
    realism_on.setattr(settings, "location_capabilities_enabled", False)
    legacy = set(get_available_actions(r, []))
    realism_on.setattr(settings, "location_capabilities_enabled", True)
    derived = set(get_available_actions(r, []))
    assert derived == legacy, slug


def _hungry(tile):
    r = _resident(*tile)
    r.meta_json = {"lab": {"access": True, "tier": "junior"},
                   "needs": {"energy": 0.9, "satiety": 0.1, "social": 0.9}}
    return r


def _ctx_for(r):
    from app.agent.schemas import TickContext
    ctx = TickContext(db=MagicMock(), resident=r, world_time="12:00",
                      hour=12, schedule_phase="午后")
    ctx.available_actions = list(get_available_actions(r, []))
    return ctx


@pytest.mark.parametrize("slug", sorted(_STATIC_LOCATION_SLUGS))
@pytest.mark.parametrize("flag", [False, True])
def test_eat_available_implies_needs_action_picks_eat(slug, flag, realism_on):
    """P1 收口不变量:EAT 可用 ⟹ _maybe_needs_action 选 EAT。

    比任何手写期望表都硬,且能永久防住第四个 dining 消费点。
    """
    from app.agent.phases.decide.basic import BasicDecidePlugin
    realism_on.setattr(settings, "location_capabilities_enabled", flag)
    ctx = _ctx_for(_hungry(LOCATIONS[slug]["center"]))
    if ActionType.EAT not in ctx.available_actions:
        return
    res = BasicDecidePlugin()._maybe_needs_action(ctx)
    assert res is not None and res.action == ActionType.EAT, (slug, flag)


def test_masked_dynamic_diner_eats_in_place_instead_of_walking_in_circles(
        overlay, realism_on):
    """被 south_quarter 遮蔽的动态餐饮楼:闸开后就地 EAT,target 是楼不是街区。"""
    from app.agent.phases.decide.basic import BasicDecidePlugin
    overlay("post_office", POST_OFFICE, capabilities={CAP_DINING: {}})
    realism_on.setattr(settings, "location_capabilities_enabled", True)
    res = BasicDecidePlugin()._maybe_needs_action(_ctx_for(_hungry((46, 103))))
    assert res is not None and res.action == ActionType.EAT
    assert res.target_slug == "post_office"


def test_action_type_enum_is_untouched():
    """P1 是门的数据化,不是新动作。"""
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH
    assert actions[15] == ActionType.EAT
