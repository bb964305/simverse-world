"""P3 ①:公投建楼载荷的白名单投影(纯函数,无 IO 无 settings)。"""
from app.agent.actions import ActionType
from app.services.civic_build import (
    DEFAULT_TYPE, MAX_NAME_CHARS, normalize_location_data,
)

POST_OFFICE_DATA = {
    "slug": "post_office", "name": "邮局", "type": "public", "role": "logistics",
    "bounds": [44, 100, 48, 106], "center": [46, 103], "entrance": [46, 100],
    "description": "小镇邮局:寄信、收件、时间胶囊的中转站",
    "boosted_actions": ["WORK"],
}


def test_known_payload_passes_through_unchanged():
    clean, warns = normalize_location_data(POST_OFFICE_DATA)
    assert clean == POST_OFFICE_DATA
    assert warns == []


def test_missing_type_defaults_to_public():
    """缺 type 的一行会让 format_location_list_for_prompt 的 loc['type'] 硬下标
    抛 KeyError → 全镇当天 planner 挂。"""
    data = {k: v for k, v in POST_OFFICE_DATA.items() if k != "type"}
    clean, warns = normalize_location_data(data)
    assert clean["type"] == DEFAULT_TYPE == "public"
    assert warns == ["missing type -> public"]


def test_invalid_type_is_downgraded_not_rejected():
    clean, warns = normalize_location_data({**POST_OFFICE_DATA, "type": "castle"})
    assert clean["type"] == "public"
    assert warns == ["invalid type 'castle' -> public"]


def test_unknown_keys_are_dropped_with_a_warning():
    clean, warns = normalize_location_data(
        {**POST_OFFICE_DATA, "wallet": 999, "__proto__": "x"})
    assert "wallet" not in clean and "__proto__" not in clean
    assert sorted(warns) == ["dropped unknown key '__proto__'",
                             "dropped unknown key 'wallet'"]


def test_bogus_action_codes_never_reach_the_prompt():
    """prompts.py:80 把 boosted_actions 直接拼进 system prompt 且不校验成员。"""
    clean, warns = normalize_location_data(
        {**POST_OFFICE_DATA, "boosted_actions": ["WORK", "DANCE", 7]})
    assert clean["boosted_actions"] == ["WORK"]
    assert warns == ["dropped 2 non-ActionType boosted_actions"]
    assert all(a in {x.value for x in ActionType}
               for a in clean["boosted_actions"])


def test_boosted_actions_non_list_becomes_empty():
    clean, warns = normalize_location_data(
        {**POST_OFFICE_DATA, "boosted_actions": "WORK"})
    assert clean["boosted_actions"] == []
    assert warns == ["boosted_actions must be a list"]


def test_capabilities_canonical_dict_form_passes_through():
    clean, warns = normalize_location_data(
        {**POST_OFFICE_DATA,
         "capabilities": {"dining": {"host_duty": "cafe_host"}}})
    assert clean["capabilities"] == {"dining": {"host_duty": "cafe_host"}}
    assert warns == []


def test_list_form_is_normalized_to_the_canonical_dict():
    clean, warns = normalize_location_data(
        {**POST_OFFICE_DATA, "capabilities": ["postal", "stage"]})
    assert clean["capabilities"] == {"postal": {}, "stage": {}}
    assert warns == []


def test_research_market_and_unregistered_names_are_dropped():
    """research 是实验楼身份门的另一半;market 的 civic_grantable=False;
    未登记名由闭集注册表直接丢掉 —— 三者都不该落库。"""
    clean, warns = normalize_location_data(
        {**POST_OFFICE_DATA,
         "capabilities": ["postal", "research", "market", "wat"]})
    assert clean["capabilities"] == {"postal": {}}
    assert warns == ["dropped 3 disallowed capabilities"]


def test_dining_without_host_duty_is_dropped_not_kept():
    """缺 host_duty 的 dining 会让餐费兜底走 treasury_debit(纯销毁)。"""
    clean, warns = normalize_location_data(
        {**POST_OFFICE_DATA, "capabilities": ["dining"]})
    assert clean["capabilities"] == {}
    assert "dining without host_duty dropped" in warns


def test_the_whitelist_is_the_registry_itself():
    """跨模块对拍:civic_build 不得自带第二份词表。"""
    from app.agent import location_caps
    from app.services import civic_build
    assert (civic_build.CIVIC_GRANTABLE_CAPABILITIES
            is location_caps.CIVIC_GRANTABLE_CAPABILITIES)
    assert location_caps.CIVIC_GRANTABLE_CAPABILITIES == frozenset(
        {"dining", "postal", "stage"})


def test_free_text_is_clipped():
    clean, warns = normalize_location_data(
        {**POST_OFFICE_DATA, "name": "楼" * (MAX_NAME_CHARS + 5)})
    assert len(clean["name"]) == MAX_NAME_CHARS
    assert warns == ["name clipped"]


def test_slug_survives_and_empty_input_is_safe():
    clean, warns = normalize_location_data({"slug": "x"})
    assert clean["slug"] == "x" and clean["type"] == "public"
    assert warns == ["missing type -> public"]
    assert normalize_location_data({}) == ({"type": "public"},
                                          ["missing type -> public"])
