"""公投/Lab 建楼载荷的净化层(P3 ①)。

``civic_service._add_dynamic_location`` 把 effect 的 ``data`` 除 slug 外整包落库
(civic_service.py:923),``map_data.load_dynamic_locations`` 又整包塞进内存
LOCATIONS(map_data.py:379-386) —— 中间没有任何一层看过这些键。于是:

* 缺 ``type`` 的一行会让 ``format_location_list_for_prompt`` 的硬下标
  ``loc["type"]``(map_data.py:429) 抛 KeyError,把全镇当天的 planner 打爆;
* ``boosted_actions`` 被 ``prompts.py:80`` 直接拼进 system prompt 且不校验成员,
  公投能造出 ``["DANCE"]`` 这种不存在的动作码 —— LLM 照抄后被
  ``parse_action_result``(schemas.py:124-127) 静默丢弃整个 tick。

**不拒绝整条**:拒绝会让「新字段先落库、代码后上线」的部署顺序把合法行判成非法。
只做白名单投影 + 逐键降级,被丢掉的键回一条 warning 供审计。纯函数、无 IO、
不读 settings —— 挂不挂闸由调用方决定。
"""
from __future__ import annotations

# capabilities 的唯一真值是 P1-S1 的闭集注册表,本模块绝不自立词表
# (两套词表必然漂移:黑名单挡不住未来新增的非 civic_grantable 能力)。
# location_caps 不 import 任何 app 模块,顶层 import 无环。
from app.agent.location_caps import (
    CAP_DINING, CIVIC_GRANTABLE_CAPABILITIES, normalize_capabilities,
)

#: 允许落进 dynamic_locations.data_json 的键(与 LOCATIONS 条目同构)。
ALLOWED_KEYS = frozenset({
    "name", "type", "role", "bounds", "center", "entrance", "description",
    "boosted_actions", "category", "capabilities", "indoor", "capacity",
    "duty_keys", "office_key", "opening_event_days",
})

LOCATION_TYPES = ("public", "private", "apartment", "outdoor")
DEFAULT_TYPE = "public"
MAX_NAME_CHARS = 20
# 与 map_data.LOCATION_LIST_DESC_CHARS 对齐:库里存 200、prompt 里砍 40 会让
# 两处口径漂移,公投作者写的后半段永远不会被任何居民看到。
MAX_DESCRIPTION_CHARS = 40
MAX_LIST_ITEMS = 6


def _action_codes() -> frozenset[str]:
    from app.agent.actions import ActionType
    return frozenset(a.value for a in ActionType)


def normalize_location_data(data: dict | None) -> tuple[dict, list[str]]:
    """Whitelist-project a civic build payload. Returns ``(clean, warnings)``.

    ``slug`` is preserved as-is (the caller strips it before persisting).
    """
    warnings: list[str] = []
    clean: dict = {}
    for key, value in (data or {}).items():
        if key == "slug":
            clean["slug"] = value
        elif key in ALLOWED_KEYS:
            clean[key] = value
        else:
            warnings.append(f"dropped unknown key '{key}'")

    loc_type = clean.get("type")
    if loc_type not in LOCATION_TYPES:
        warnings.append(
            f"missing type -> {DEFAULT_TYPE}" if loc_type is None
            else f"invalid type {loc_type!r} -> {DEFAULT_TYPE}")
        clean["type"] = DEFAULT_TYPE

    for key, limit in (("name", MAX_NAME_CHARS),
                       ("description", MAX_DESCRIPTION_CHARS)):
        value = clean.get(key)
        if isinstance(value, str) and len(value) > limit:
            clean[key] = value[:limit]
            warnings.append(f"{key} clipped")

    if "boosted_actions" in clean:
        raw = clean["boosted_actions"]
        if not isinstance(raw, list):
            clean["boosted_actions"] = []
            warnings.append("boosted_actions must be a list")
        else:
            codes = _action_codes()
            kept = [a for a in raw if isinstance(a, str) and a in codes]
            kept = kept[:MAX_LIST_ITEMS]
            if len(kept) != len(raw):
                warnings.append(
                    f"dropped {len(raw) - len(kept)} non-ActionType boosted_actions")
            clean["boosted_actions"] = kept

    if "capabilities" in clean:
        raw = clean["capabilities"]
        if not isinstance(raw, (dict, list, tuple, set, frozenset)):
            clean["capabilities"] = {}
            warnings.append("capabilities must be a dict or list")
        else:
            # 先归一成规范形态 dict[str, dict](list -> 参数空字典),再按
            # civic_grantable 白名单过滤 —— research/market 与任何未登记名
            # 天然被挡掉,不需要黑名单。输出保持 dict 形态,P1-S7 读得到
            # {"dining": {"host_duty": ...}} 这类参数。
            caps = normalize_capabilities(raw)
            kept = {n: p for n, p in caps.items()
                    if n in CIVIC_GRANTABLE_CAPABILITIES}
            dropped = len(raw) - len(kept)
            # 缺 host_duty 的 dining 会让餐费兜底走 coin_service.treasury_debit
            # (纯销毁、无对手方),所以整项丢掉 —— 丢键不拒条,但不留销毁口。
            if CAP_DINING in kept and not kept[CAP_DINING].get("host_duty"):
                kept.pop(CAP_DINING)
                dropped += 1
                warnings.append("dining without host_duty dropped")
            if dropped > 0:
                warnings.append(f"dropped {dropped} disallowed capabilities")
            clean["capabilities"] = kept

    return clean, warnings
