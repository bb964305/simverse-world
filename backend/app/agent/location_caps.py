"""P1 地点能力声明:能力名的闭集注册表 + 声明归一化。

这个模块不 import 任何 app 模块 —— 它被 map_data 与 actions 两侧引用,任一方向的
import 都会成环(两侧今天全部用惰性 import 在绕这个坑)。因此 unlocks 存的是
ActionType 的 value 字符串,由消费侧自行 ActionType(v) 还原。

能力与 category 是超集关系:category 是能力派生出的粗分类视图(见
map_data.location_category),不是被它取代。map_data._DINING_LOCATIONS 白名单保留为
最后一级 fallback —— 删掉它,一旦某处声明没落地就会静默失去 dining。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

CAP_DINING = "dining"
CAP_RESEARCH = "research"
CAP_MARKET = "market"
# P2 全段的硬依赖(plan_P2_postal.json notes「新增依赖边 A」逐字给定)。不登记 →
# normalize_capabilities 只 logger.debug 静默丢弃 → 邮局/剧院的能力门永远拿不到,
# 全链零告警。P2-S1 的第一条测试就是这条依赖边的守卫。
CAP_POSTAL = "postal"
CAP_STAGE = "stage"


@dataclass(frozen=True)
class CapabilitySpec:
    """一种能力的静态元数据。

    unlocks          该能力解锁的动作码(ActionType.value)。只表达「站在哪」这一半
                     门槛 —— 身份门(如 has_trusted_lab_access)不在能力体系内,
                     能力永远不能替代它。
    category         该能力派生回 location_category 的粗分类;None = 不派生。
    civic_grantable  公投 / Lab 能否把这项能力授予一栋楼。research 恒 False:
                     否则一次公投就能绕过实验楼的地点门(actions.py:130)。
    """

    name: str
    unlocks: tuple[str, ...] = ()
    category: str | None = None
    civic_grantable: bool = False


CAPABILITIES: dict[str, CapabilitySpec] = {
    CAP_DINING: CapabilitySpec(
        CAP_DINING, unlocks=("EAT",), category="dining", civic_grantable=True),
    CAP_RESEARCH: CapabilitySpec(
        CAP_RESEARCH, unlocks=("RESEARCH",), category=None, civic_grantable=False),
    CAP_MARKET: CapabilitySpec(
        CAP_MARKET, unlocks=(), category=None, civic_grantable=False),
    CAP_POSTAL: CapabilitySpec(
        CAP_POSTAL, unlocks=(), category=None, civic_grantable=True),
    CAP_STAGE: CapabilitySpec(
        CAP_STAGE, unlocks=(), category=None, civic_grantable=True),
}

#: 公投 / Lab 允许授予的能力。P3 在落库前用它过滤 effect.data。
CIVIC_GRANTABLE_CAPABILITIES: frozenset[str] = frozenset(
    name for name, spec in CAPABILITIES.items() if spec.civic_grantable
)


def normalize_capabilities(raw: object) -> dict[str, dict]:
    """把一条地点声明归一成 {能力名: 参数字典}。

    宽松入口(公投 effect.data 是手写的):接受 dict[str, dict] 规范形态,也接受
    list/tuple/set[str](参数取空字典)。任何非法形态、未知能力名、非 str 键一律
    丢弃并返回剩余部分,绝不抛 —— 老 dynamic_locations 行没有这个键,缺省安全是
    硬要求(一条畸形行不得让全镇 planner 崩)。
    """
    if raw is None:
        return {}
    items: list[tuple[object, object]]
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = [(v, {}) for v in raw]
    else:
        logger.debug("capabilities declaration ignored (bad type %r)",
                     type(raw).__name__)
        return {}

    out: dict[str, dict] = {}
    for name, params in items:
        if not isinstance(name, str) or name not in CAPABILITIES:
            logger.debug("unknown capability declaration dropped: %r", name)
            continue
        out[name] = dict(params) if isinstance(params, dict) else {}
    return out


def capability_unlocks(cap: str) -> tuple[str, ...]:
    """该能力解锁的动作码(ActionType.value);未知能力返回空元组。"""
    spec = CAPABILITIES.get(cap)
    return spec.unlocks if spec else ()
