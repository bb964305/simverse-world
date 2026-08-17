"""P1-S1: 地点能力注册表与声明归一化(纯函数,无闸,无 I/O)。

这个模块是 map_data 与 actions 之间的共享词表,不得 import 任何 app 模块(任一方向
都会在 import 期成环,两侧今天全靠惰性 import 绕这个坑),所以 unlocks 存的是
ActionType 的 value 字符串。最后一条测试把这条约束钉死。
"""
import re
from pathlib import Path

from app.agent.actions import ActionType
from app.agent.location_caps import (
    CAP_DINING,
    CAP_MARKET,
    CAP_POSTAL,
    CAP_RESEARCH,
    CAP_STAGE,
    CAPABILITIES,
    CIVIC_GRANTABLE_CAPABILITIES,
    CapabilitySpec,
    capability_unlocks,
    normalize_capabilities,
)


def test_registry_is_a_closed_set_of_five():
    assert set(CAPABILITIES) == {CAP_DINING, CAP_RESEARCH, CAP_MARKET,
                                 CAP_POSTAL, CAP_STAGE}
    assert all(isinstance(v, CapabilitySpec) for v in CAPABILITIES.values())
    assert all(name == spec.name for name, spec in CAPABILITIES.items())


def test_postal_and_stage_are_inert_but_civic_grantable():
    """P2 只拿它们当「站在哪」的门:不解锁动作(零新增 ActionType)、不派生
    category(不得污染 EAT / nearest_dining 通路)、公投可授予。"""
    for cap in (CAP_POSTAL, CAP_STAGE):
        assert CAPABILITIES[cap].unlocks == ()
        assert CAPABILITIES[cap].category is None
        assert CAPABILITIES[cap].civic_grantable is True


def test_unlocks_are_real_action_type_values():
    """unlocks 存字符串是为了断依赖环;但每一个都必须能被 ActionType 接住,否则会
    像 boosted_actions=[DANCE] 那样进 prompt 后被 parse_action_result 静默丢弃。"""
    valid = {a.value for a in ActionType}
    for spec in CAPABILITIES.values():
        assert set(spec.unlocks) <= valid, spec


def test_dining_derives_a_category_and_research_does_not():
    assert CAPABILITIES[CAP_DINING].category == "dining"
    assert CAPABILITIES[CAP_DINING].unlocks == ("EAT",)
    assert CAPABILITIES[CAP_RESEARCH].category is None
    assert CAPABILITIES[CAP_RESEARCH].unlocks == ("RESEARCH",)
    assert CAPABILITIES[CAP_MARKET].unlocks == ()
    assert CAPABILITIES[CAP_MARKET].category is None


def test_research_is_never_civic_grantable():
    """公投/Lab 若能授予 research,等于绕过实验楼的地点门(actions.py:130)。"""
    assert CIVIC_GRANTABLE_CAPABILITIES == frozenset(
        {CAP_DINING, CAP_POSTAL, CAP_STAGE})
    assert CAPABILITIES[CAP_RESEARCH].civic_grantable is False
    assert CAPABILITIES[CAP_MARKET].civic_grantable is False


def test_normalize_accepts_the_canonical_dict_form():
    assert normalize_capabilities({"dining": {"host_duty": "cafe_host"}}) == {
        "dining": {"host_duty": "cafe_host"}}


def test_normalize_accepts_the_loose_list_form():
    """公投 effect.data 是手写的,列表形态更不易写错。"""
    assert normalize_capabilities(["dining"]) == {"dining": {}}
    assert normalize_capabilities(("research", "market")) == {
        "research": {}, "market": {}}


def test_normalize_is_default_safe_for_legacy_rows():
    """老 dynamic_locations 行没有这个键 —— 缺省安全是硬要求,绝不抛。"""
    for raw in (None, "dining", 7, 0, object()):
        assert normalize_capabilities(raw) == {}
    assert normalize_capabilities({"dining": "cafe_host"}) == {"dining": {}}


def test_normalize_drops_unknown_and_non_string_names():
    assert normalize_capabilities({"dining": {}, "DANCE": {}}) == {"dining": {}}
    assert normalize_capabilities([1, None, "dining"]) == {"dining": {}}
    assert normalize_capabilities({7: {}}) == {}


def test_normalize_copies_params_so_callers_cannot_mutate_the_declaration():
    src = {"dining": {"host_duty": "cafe_host"}}
    out = normalize_capabilities(src)
    out["dining"]["host_duty"] = "hacked"
    assert src["dining"]["host_duty"] == "cafe_host"


def test_capability_unlocks_is_total():
    assert capability_unlocks(CAP_DINING) == ("EAT",)
    assert capability_unlocks("nope") == ()


def test_module_imports_nothing_from_app():
    """依赖环守卫:location_caps 被 map_data 与 actions 双向引用。"""
    src = Path(__file__).resolve().parents[1] / "app" / "agent" / "location_caps.py"
    offenders = [
        ln for ln in src.read_text(encoding="utf-8").splitlines()
        if re.match(r"^\s*(from|import)\s+app\b", ln)
    ]
    assert not offenders, offenders
