"""Town map data: named locations, coordinates, and utility functions."""
from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

LOCATIONS: dict[str, dict[str, Any]] = {
    # === Public Facilities ===
    "academy": {
        "name": "学院",
        "type": "public",
        "role": "growth",
        "bounds": (15, 18, 42, 34),
        "center": (28, 26),
        "entrance": (25, 18),
        "description": "小镇的学习中心，有多间教室和自习室",
        "boosted_actions": ["STUDY", "REFLECT"],
    },
    "tavern": {
        "name": "酒馆",
        "type": "public",
        "role": "social",
        "bounds": (72, 13, 83, 26),
        "center": (77, 19),
        "entrance": (72, 14),
        "description": "热闹的社交场所，居民们喜欢在这里聊天和交换消息",
        "boosted_actions": ["CHAT_RESIDENT", "GOSSIP"],
        # host_duty 脱离 dining 毫无意义,所以参数作用域跟着能力走。
        "capabilities": {"dining": {"host_duty": "tavern_hub"}},
    },
    "cafe": {
        "name": "咖啡馆",
        "type": "public",
        "role": "casual_social",
        "bounds": (53, 14, 62, 26),
        "center": (57, 20),
        "entrance": (53, 14),
        "description": "安静的休闲场所，适合一对一的深度对话",
        "boosted_actions": ["CHAT_RESIDENT", "IDLE"],
        "capabilities": {"dining": {"host_duty": "cafe_host"}},
    },
    "workshop": {
        "name": "工坊",
        "type": "public",
        "role": "production",
        "bounds": (108, 20, 124, 34),
        "center": (116, 27),
        "entrance": (108, 20),
        "description": "制造和修理物品的地方，工具和材料一应俱全",
        "boosted_actions": ["WORK"],
    },
    "library": {
        "name": "图书馆",
        "type": "public",
        "role": "knowledge",
        "bounds": (57, 43, 70, 53),
        "center": (63, 48),
        "entrance": (57, 43),
        "description": "藏书丰富的图书馆，适合研究和独处思考",
        "boosted_actions": ["STUDY", "REFLECT", "JOURNAL"],
    },
    "shop": {
        "name": "杂货铺",
        "type": "public",
        "role": "economy",
        "bounds": (75, 43, 93, 53),
        "center": (84, 48),
        "entrance": (75, 43),
        "description": "日用品和特色商品的交易场所",
        "boosted_actions": ["WORK", "OBSERVE"],
    },
    "town_hall": {
        "name": "市政厅",
        "type": "public",
        "role": "governance",
        "bounds": (106, 45, 132, 62),
        "center": (119, 53),
        "entrance": (106, 45),
        "description": "小镇的行政中心，处理公共事务和居民登记",
        "boosted_actions": ["WORK"],
    },
    # === Experiment Building (Lab / 元游戏入口) ===
    # Bounds chosen on empty land in the south-east; verified non-overlapping
    # against every existing LOCATIONS entry (see the archived
    # archive/2026-07-25/docs/FEATURE_SPEC_LAB.md §13 and LAB_HANDOFF risk note).
    # The visual tilemap tile is deferred to art (P4);
    # the location is logic-only for now (pathfinding/planning/codex).
    "experiment_building": {
        "name": "实验楼",
        "type": "public",
        "role": "research",
        "bounds": (108, 72, 124, 86),
        "center": (116, 79),
        "entrance": (116, 72),
        "description": "小镇的元游戏入口：研究员在此接入隔离沙箱，完成玩家委托、产出世界变更提案",
        "boosted_actions": ["RESEARCH"],
        # 只收编「站在哪」这一半门槛;身份门 has_trusted_lab_access 不在能力体系内。
        # research 的 civic_grantable=False —— 公投永远不能给别的楼授予它。
        "capabilities": {"research": {}},
    },
    # === Private Houses ===
    "house_a": {
        "name": "住宅A", "type": "private",
        "bounds": (65, 14, 69, 26), "center": (67, 20), "entrance": (65, 19),
        "capacity": 1,
    },
    "house_b": {
        "name": "住宅B", "type": "private",
        "bounds": (86, 13, 90, 25), "center": (88, 19), "entrance": (86, 18),
        "capacity": 1,
    },
    "house_c": {
        "name": "住宅C", "type": "private",
        "bounds": (93, 13, 97, 25), "center": (95, 19), "entrance": (93, 18),
        "capacity": 1,
    },
    "house_d": {
        "name": "住宅D", "type": "private",
        "bounds": (20, 59, 24, 70), "center": (22, 64), "entrance": (20, 65),
        "capacity": 1,
    },
    "house_e": {
        "name": "住宅E", "type": "private",
        "bounds": (27, 59, 33, 70), "center": (30, 64), "entrance": (28, 65),
        "capacity": 1,
    },
    "house_f": {
        "name": "住宅F", "type": "private",
        "bounds": (36, 59, 40, 70), "center": (38, 64), "entrance": (36, 65),
        "capacity": 1,
    },
    # === Apartments ===
    "apt_star": {
        "name": "星光公寓", "type": "apartment",
        "bounds": (51, 65, 62, 75), "center": (56, 70), "entrance": (54, 74),
        "capacity": 5,
    },
    "apt_moon": {
        "name": "月华公寓", "type": "apartment",
        "bounds": (69, 65, 80, 75), "center": (74, 70), "entrance": (72, 74),
        "capacity": 5,
    },
    "apt_dawn": {
        "name": "晨曦公寓", "type": "apartment",
        "bounds": (87, 65, 99, 75), "center": (93, 70), "entrance": (90, 74),
        "capacity": 5,
    },
    # === Expansion Housing: South Quarter ===
    "house_g": {
        "name": "南苑住宅G", "type": "private",
        "bounds": (20, 104, 24, 115), "center": (22, 109), "entrance": (20, 110),
        "capacity": 1,
    },
    "house_h": {
        "name": "南苑住宅H", "type": "private",
        "bounds": (27, 104, 33, 115), "center": (30, 109), "entrance": (28, 110),
        "capacity": 1,
    },
    "house_i": {
        "name": "南苑住宅I", "type": "private",
        "bounds": (36, 104, 40, 115), "center": (38, 109), "entrance": (36, 110),
        "capacity": 1,
    },
    "apt_pine": {
        "name": "松风公寓", "type": "apartment",
        "bounds": (51, 110, 62, 120), "center": (56, 115), "entrance": (54, 119),
        "capacity": 5,
    },
    "apt_lake": {
        "name": "湖畔公寓", "type": "apartment",
        "bounds": (69, 110, 80, 120), "center": (74, 115), "entrance": (72, 119),
        "capacity": 5,
    },
    "apt_sunrise": {
        "name": "朝阳公寓", "type": "apartment",
        "bounds": (87, 110, 99, 120), "center": (93, 115), "entrance": (90, 119),
        "capacity": 5,
    },
    # === Expansion Housing: East Gardens ===
    "apt_river": {
        "name": "河湾公寓", "type": "apartment",
        "bounds": (141, 65, 152, 75), "center": (146, 70), "entrance": (144, 74),
        "capacity": 5,
    },
    "apt_garden": {
        "name": "花园公寓", "type": "apartment",
        "bounds": (159, 65, 170, 75), "center": (164, 70), "entrance": (162, 74),
        "capacity": 5,
    },
    "apt_orchard": {
        "name": "果园公寓", "type": "apartment",
        "bounds": (143, 110, 155, 120), "center": (149, 115), "entrance": (152, 119),
        "capacity": 5,
    },
    "apt_harbor": {
        "name": "港湾公寓", "type": "apartment",
        "bounds": (162, 110, 173, 120), "center": (168, 115), "entrance": (170, 119),
        "capacity": 5,
    },
    # === Market Hall ===
    # A purpose-built market-day destination.  It is a visit/trade location,
    # not a resident spawn district; the caravan owns its parking anchor.
    "market_hall": {
        "name": "集市大厅",
        "type": "public",
        "role": "economy",
        "bounds": (105, 89, 119, 99),
        "center": (112, 94),
        "entrance": (105, 94),
        "caravan_parking": (109, 94),
        "allocatable": False,
        "description": "集市日开放的独立交易大厅，商队与本地摊主在此摆摊买卖",
        "boosted_actions": ["WORK", "OBSERVE"],
        # 仅供发现/导流(P3 冷启动、town_facts),禁止用于场地解析。场地权威是
        # settings.market_day_venue + event_location.resolve_event_location_id;
        # 路网几何(caravan_route._MARKET_AVENUE_X_BOUNDS / caravan_parking)是按这
        # 一栋楼的实际瓦片手调的,改成能力反查一旦出现第二个 market-capable 地点,
        # cohort 判据 / decide 目的地 / 商队停车锚点会指向不同的楼,静默分裂。
        "capabilities": {"market": {}},
    },
    # === Outdoor Areas ===
    "north_path": {
        "name": "北林荫道", "type": "outdoor",
        "bounds": (15, 35, 135, 42), "center": (75, 38),
        "description": "连接北区建筑群的林荫步道",
    },
    "central_plaza": {
        "name": "中央广场", "type": "outdoor",
        "bounds": (55, 54, 95, 58), "center": (75, 56),
        "description": "小镇中心的开阔广场，居民们经常路过",
    },
    "south_lawn": {
        "name": "南草坪", "type": "outdoor",
        "bounds": (15, 76, 99, 83), "center": (57, 79),
        "description": "南部公寓之间的绿地，适合散步和休息",
    },
    "town_entrance": {
        "name": "小镇入口", "type": "outdoor",
        "bounds": (100, 119, 104, 122), "center": (102, 121),
        "description": "南部商道尽头的木门楼，宽阔大道由此直通集市大厅",
    },
    "east_gardens": {
        "name": "东岸花园", "type": "outdoor",
        "bounds": (140, 35, 179, 58), "center": (160, 56),
        "description": "向东延伸的新街区，林荫道连接住宅与公共活动空间",
    },
    "south_quarter": {
        "name": "南苑新区", "type": "outdoor",
        "bounds": (42, 100, 135, 109), "center": (88, 104),
        "description": "小镇南部的新居住区，宽阔步道通往成排住宅",
    },
}

#: 模块 import 期的静态 slug 快照(不含动态合入的楼)。P1 的能力防漂移守卫只锁这个
#: 集合:既防代码误声明第二个 research 地点,又不挡 P3 的公投建楼在数据侧扩展
#: dining。load_dynamic_locations 只增删 LOCATIONS,不动这个 frozenset。
_STATIC_LOCATION_SLUGS: frozenset[str] = frozenset(LOCATIONS)


def _find_location_in_bounds(x: int, y: int) -> tuple[str | None, dict | None]:
    """Return (loc_id, loc) if (x,y) falls within any location's bounds, else (None, None)."""
    for loc_id, loc in LOCATIONS.items():
        x1, y1, x2, y2 = loc["bounds"]
        if x1 <= x <= x2 and y1 <= y <= y2:
            return loc_id, loc
    return None, None


def get_location_at(x: int, y: int) -> dict | None:
    """Return location dict if (x,y) falls within any location's bounds."""
    _, loc = _find_location_in_bounds(x, y)
    return loc


def get_location_id_at(x: int, y: int) -> str | None:
    """Return location ID if (x,y) falls within any location's bounds."""
    loc_id, _ = _find_location_in_bounds(x, y)
    return loc_id


def get_location_by_id(loc_id: str) -> dict | None:
    """Lookup location by ID."""
    return LOCATIONS.get(loc_id)


_DINING_LOCATIONS = {"cafe", "tavern"}


def location_category(loc_id: str | None) -> str | None:
    """Realism P1-10: coarse location category (e.g. "dining").

    三级优先级:显式 category 键 → (P1 闸开时)从 capabilities 声明派生 →
    _DINING_LOCATIONS 白名单。白名单保留为最后一级 fallback —— 删掉它,一旦某处
    声明没落地就会静默失去 dining,纯负收益。

    闸关时中间那一级整块跳过,返回值域与顺序与改前逐字相同。
    """
    if not loc_id:
        return None
    loc = get_location_by_id(loc_id)
    if loc and loc.get("category"):
        return loc["category"]
    from app.config import settings as _cap_settings
    if _cap_settings.location_capabilities_enabled:
        from app.agent.location_caps import CAPABILITIES
        # sorted: 今天只有 dining 带 category,排序保证将来加第二个带 category 的
        # 能力时结果确定,不受 dict 顺序影响。
        for cap in sorted(_declared_capabilities(loc, loc_id)):
            spec = CAPABILITIES.get(cap)
            if spec and spec.category:
                return spec.category
    return "dining" if loc_id in _DINING_LOCATIONS else None


# ── Location capability declarations (P1) ─────────────────────────────
# 能力 = 「地点声明自己提供什么」。老 dynamic_locations 行没有 capabilities 键 →
# 归一成空 dict → 不解锁任何东西,与改前逐位相同(缺省安全)。
#
# capability 与 category 双向相容:这里把 category == "dining" 无条件视为拥有
# dining 能力,于是 has_capability(x,"dining") 恒为 location_category(x)=="dining"
# 的超集 —— 任何既有 category 驱动的行为不可能因为忘写声明而丢失。反方向(能力
# 派生出 category)在 location_category 里做,且只读纯字典的 _declared_capabilities,
# 两个方向因此不成环。


def _declared_capabilities(loc: dict | None,
                           loc_id: str | None = None) -> dict[str, dict]:
    """一条地点 dict 上显式声明的能力(已归一、已丢弃未知项)。纯字典读。

    动态行(公投建楼)额外按 CIVIC_GRANTABLE_CAPABILITIES 白名单降级 —— 这是把
    location_caps 里「公投永远不能授予 research」那条安全边界在 P1 内**真正执行**,
    不依赖 P3 任何一道闸的开闸顺序:routers/polls.py 允许 admin 附带任意 effect
    dict,civic_service._add_dynamic_location 只校验 slug 非空 + "bounds" in data
    就整包落库。降级只作用于 _dynamic_slugs,静态地点不受影响。
    """
    from app.agent.location_caps import (
        CIVIC_GRANTABLE_CAPABILITIES, normalize_capabilities)
    if not loc:
        return {}
    caps = normalize_capabilities(loc.get("capabilities"))
    if loc_id is not None and loc_id in _dynamic_slugs:
        dropped = sorted(set(caps) - CIVIC_GRANTABLE_CAPABILITIES)
        if dropped:
            logger.warning(
                "dynamic location %s declared non-grantable capabilities %s",
                loc_id, dropped)
        caps = {k: v for k, v in caps.items()
                if k in CIVIC_GRANTABLE_CAPABILITIES}
    return caps


def location_capabilities(loc_id: str | None) -> frozenset[str]:
    """该地点提供的能力集合 = 显式声明 并上 category 派生。"""
    if not loc_id:
        return frozenset()
    from app.agent.location_caps import CAP_DINING
    caps = set(_declared_capabilities(get_location_by_id(loc_id), loc_id))
    if location_category(loc_id) == "dining":
        caps.add(CAP_DINING)
    return frozenset(caps)


def has_capability(loc_id: str | None, cap: str) -> bool:
    """该地点是否提供 cap。"""
    return cap in location_capabilities(loc_id)


def capability_param(loc_id: str | None, cap: str, key: str, default=None):
    """读取该地点某项能力的参数(如 dining 的 host_duty)。

    地点没声明该能力 / 声明了但没写这个参数 / 显式写了 null → 返回 default。
    调用方必须能接住 default:第三个 dining 地点忘写 host_duty 时不得静默把餐费
    错付给别人(execute/basic.py 的 else 分支就是这个历史 bug)。
    """
    if not loc_id:
        return default
    params = _declared_capabilities(
        get_location_by_id(loc_id), loc_id).get(cap)
    if not params:
        return default
    value = params.get(key, default)
    return default if value is None else value


def nearest_dining_location(from_tile: tuple[int, int]) -> str | None:
    """Nearest dining-category location entrance to ``from_tile``."""
    best, best_d = None, None
    for loc_id, loc in LOCATIONS.items():
        if location_category(loc_id) != "dining":
            continue
        entrance = loc.get("entrance") or loc.get("center")
        if not entrance:
            continue
        d = abs(from_tile[0] - entrance[0]) + abs(from_tile[1] - entrance[1])
        if best_d is None or d < best_d:
            best, best_d = loc_id, d
    return best


def location_is_indoor(loc_id: str | None) -> bool:
    """Realism P1-8: whether a location shelters from weather. Derived from the
    existing ``type`` (outdoor plazas/parks/streets are exposed) with an optional
    explicit ``indoor`` override. Deviation: not hand-tagging all ~20 locations —
    ``type`` already carries indoor/outdoor and stays in sync."""
    loc = get_location_by_id(loc_id) if loc_id else None
    if loc is None:
        return False
    if "indoor" in loc:
        return bool(loc["indoor"])
    return loc.get("type") != "outdoor"


def nearest_indoor_location(from_tile: tuple[int, int]) -> str | None:
    """Nearest public indoor location entrance to ``from_tile`` (for 躲雨)."""
    best, best_d = None, None
    for loc_id, loc in LOCATIONS.items():
        if loc.get("type") in ("private", "apartment"):
            continue
        if not location_is_indoor(loc_id):
            continue
        entrance = loc.get("entrance") or loc.get("center")
        if not entrance:
            continue
        d = abs(from_tile[0] - entrance[0]) + abs(from_tile[1] - entrance[1])
        if best_d is None or d < best_d:
            best, best_d = loc_id, d
    return best


def get_location_id_by_name(name: str | None) -> str | None:
    """Reverse-lookup a location id by its display name (first match).

    Used by realism plan/decision target resolution so a plan that only carries
    the location display name (not the slug) still resolves to an entrance tile.
    """
    if not name:
        return None
    for loc_id, loc in LOCATIONS.items():
        if loc.get("name") == name:
            return loc_id
    return None


# ── Dynamic world overlay (Lab governance) ────────────────────────────
# Slugs merged in from the ``dynamic_locations`` table so an approved
# WorldChangeProposal can add a building without a redeploy. Tracked so a
# reload can drop the previously-merged set before re-merging.
_dynamic_slugs: set[str] = set()


async def load_dynamic_locations() -> int:
    """Merge active ``dynamic_locations`` overlay rows into in-memory LOCATIONS.

    Called at process startup and on the ``sv:world:reload`` signal so a newly
    approved building appears in pathfinding / planning / codex without a
    redeploy (spec §4.6, §7). Previously-merged dynamic slugs are dropped first
    so a revert/rename doesn't linger. Fail-open: any DB hiccup returns 0 and
    leaves the static LOCATIONS intact. Returns the number merged.
    """
    from sqlalchemy import select
    from app.database import async_session
    from app.models.dynamic_location import DynamicLocation

    global _dynamic_slugs
    for slug in _dynamic_slugs:
        LOCATIONS.pop(slug, None)
    _dynamic_slugs = set()

    try:
        async with async_session() as db:
            rows = (await db.execute(
                select(DynamicLocation).where(DynamicLocation.active.is_(True))
            )).scalars().all()
    except Exception:
        return 0

    n = 0
    for row in rows:
        data = dict(row.data_json or {})
        # JSON stores lists; LOCATIONS consumers expect tuples for bounds/coords.
        for key in ("bounds", "center", "entrance"):
            if isinstance(data.get(key), list):
                data[key] = tuple(data[key])
        if "bounds" not in data:
            continue  # malformed overlay row — skip rather than crash lookups
        LOCATIONS[row.slug] = data
        _dynamic_slugs.add(row.slug)
        n += 1
    return n


def get_public_locations() -> list[dict]:
    """All public facilities."""
    return [loc for loc in LOCATIONS.values() if loc["type"] == "public"]


def get_housing_locations() -> list[dict]:
    """All private + apartment locations with capacity."""
    return [loc for loc in LOCATIONS.values() if loc["type"] in ("private", "apartment")]


def find_nearest_location(x: int, y: int, loc_type: str | None = None) -> tuple[str, dict] | None:
    """Find nearest location by center distance, optionally filtered by type."""
    best_id, best_loc, best_dist = None, None, float("inf")
    for loc_id, loc in LOCATIONS.items():
        if loc_type and loc["type"] != loc_type:
            continue
        cx, cy = loc["center"]
        dist = math.sqrt((x - cx) ** 2 + (y - cy) ** 2)
        if dist < best_dist:
            best_id, best_loc, best_dist = loc_id, loc, dist
    if best_id is None:
        return None
    return best_id, best_loc


def format_location_list_for_prompt(from_tile: tuple[int, int] | None = None) -> str:
    """Format public locations + outdoor areas into a string for LLM prompts.

    Realism P1-7: when ``from_tile`` is given (the resident's current tile) and
    realism is on, each candidate is annotated with an estimated commute time
    (manhattan distance ÷ move speed ≈ minutes) so the planner accounts for
    travel — exactly how a real person plans a day."""
    from app.config import settings
    show_commute = settings.realism_enabled and from_tile is not None
    speed = max(1, settings.realism_move_speed)
    lines = []
    for loc_id, loc in LOCATIONS.items():
        if loc["type"] in ("private", "apartment"):
            continue
        desc = loc.get("description", "")
        boosted = loc.get("boosted_actions", [])
        # The stable id is the only movement target accepted from new plans.
        # Display names remain for prose and for legacy-plan fallback.
        line = f"- {loc['name']}（id={loc_id}）：{desc}"
        if boosted:
            line += f"（适合：{', '.join(boosted)}）"
        entrance = loc.get("entrance")
        if entrance:
            line += f" 入口坐标=({entrance[0]},{entrance[1]})"
            if show_commute:
                dist = abs(from_tile[0] - entrance[0]) + abs(from_tile[1] - entrance[1])
                line += f" 约{max(1, round(dist / speed))}分钟路程"
        lines.append(line)
    return "\n".join(lines)


def get_valid_target_tile(loc_id: str) -> tuple[int, int] | None:
    """Return the entrance tile of a location for pathfinding."""
    loc = LOCATIONS.get(loc_id)
    if not loc:
        return None
    return loc.get("entrance", loc.get("center"))


# ── Housing Assignment ────────────────────────────────────────────────

_HOUSING_ORDER = [
    "house_a", "house_b", "house_c", "house_d", "house_e", "house_f",
    "apt_star", "apt_moon", "apt_dawn",
    "house_g", "house_h", "house_i",
    "apt_river", "apt_garden", "apt_pine", "apt_lake", "apt_sunrise",
    "apt_orchard", "apt_harbor",
]


def assign_home(occupied: dict[str, int]) -> str | None:
    """Find first available home. Returns location_id or None if all full.

    Args:
        occupied: {location_id: current_occupant_count}
    """
    for loc_id in _HOUSING_ORDER:
        loc = LOCATIONS.get(loc_id)
        if not loc:
            continue
        capacity = loc.get("capacity", 0)
        current = occupied.get(loc_id, 0)
        if current < capacity:
            return loc_id
    return None


async def allocate_home(db) -> str | None:
    """Query current housing occupancy and assign the next available home."""
    from sqlalchemy import select, func
    from app.models.resident import Resident

    rows = await db.execute(
        select(Resident.home_location_id, func.count())
        .where(Resident.home_location_id.isnot(None))
        .where(Resident.resident_type != "player")
        .group_by(Resident.home_location_id)
    )
    occupied = {row[0]: row[1] for row in rows.all()}
    return assign_home(occupied)
