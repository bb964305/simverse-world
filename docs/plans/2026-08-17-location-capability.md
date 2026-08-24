# 三段式计划：让地点有功能、让公投建的楼活起来

> 生成于 2026-08-17，基线 `master @ 820484d`。
> 本文档由多轮 agent 工作流产出并经三路 critic 对抗式复核，共 54 条 blocking issue，其中 40 条已落成补丁、3 条转为新增 step、其余在文末「未处置」一节逐条说明理由。

## 总览

| 段 | 目标 | step 数 | 新增 step | 被 critic 修订的 step |
|---|---|---|---|---|
| **P1** | 地点能声明功能 | 11 | P1-S0T | 6 个 |
| **P2** | 修复邮局和剧院的功能 | 15 | — | 0 个 |
| **P3** | 修复法案落实新建建筑的功能 | 14 | P3-S8b, P3-S9b | 11 个 |
| | **合计** | | | **43 step** |

### 依赖关系

```
P1 地点能声明功能  ←── 地基
     │
     ├──> P2 修复邮局/剧院   （存量补救，硬依赖 P1-S1 的 CAP_POSTAL/CAP_STAGE 登记）
     └──> P3 修复公投建楼接线（硬依赖 P1-S1 的 capabilities 注册表作为唯一真值）
```

**P2 与 P3 之间无依赖，可并行。** 但两者都必须等 P1-S1 合并后才能开工。

### 红线

1. **迁移与开闸不得同批**（07-25 事故就在这窗口里）。全计划只有 `P3-S13`（alembic 068 修剧院坐标）是迁移批次。
2. **新行为一律 feature flag 默认关**，闸关时必须字节级 status quo。
3. **一 step 一 commit**，每 step 做完 build+test 验证再进下一步。
4. **零新增 ActionType** —— P2 全段用 `len(list(ActionType)) == 16` 钉死。
5. **新增 Settings 字段必须同 commit 改 `backend/.env.example`**，`CIVIC_`/`STAGE_` 前缀还要双写 `deploy/backend/.env.example`，且 `verify_cmd` 必须含 `tests/test_env_example_consistency.py`。
6. **观众收益零 SC 流动** —— `debate_service` 的 settle 已在真烧 5%，不得双花。
7. 老 `dynamic_locations` 行没有新字段时必须缺省安全，存量两栋楼不得失能。

---

# P1 地点能声明功能

## P1 地点能声明功能（capability declaration）—— bite-sized TDD 执行计划

### P1-S0 — 依赖图 + 本轮实测校正（不产生 commit，执行前必读）

**Flag / 批次**：无

**为什么**：本 step 不改代码，只固定执行顺序与两处对前序设计的实测校正。

**依赖图（→ 串行）**
S1 → S2 → S3 → S4 → S6 → S7；S2 → S5（S5 与 S3 可并行）；S4 → S8；S9 完全独立可全程并行；S10 依赖 S3。
可并行组：{S9} 与主链全程无文件交集；{S3, S5} 在 S2 之后可并行。其余严格串行。

**实测校正（前序设计有两处需更正）**
1. `REALISM_CROWD_ENABLED=true` **确实存在**于 `backend/.env.example:533`；缺的是 `deploy/backend/.env.example`（那里只有 :325 的一句说明性注释）。前序 P2 设计写成「backend 版是 true、deploy 版没有显式赋值行」，前半句需更正。不影响 P1，但 P2 开工前要按此复核 vm212 用的是哪份模板。
2. `backend/tests/test_env_example_consistency.py::test_every_settings_field_is_documented_or_allowlisted` **强制每个 Settings 字段都必须出现在 backend/.env.example**。因此 S3 新增 flag 的那一 commit 必须同批补 `.env.example` 行，否则既有测试当场变红。这是执行时最容易踩的坑，前序设计未提及。

**实测基线（backend/.venv 真实进程内跑出）**
- cafe(53,14,62,26) / tavern(72,13,83,26) / experiment_building(108,72,124,86) / market_hall(105,89,119,99) 与全表 **零 bounds 重叠** —— 这是 S5/S6「最小面积匹配 == 首命中」等价性的机器依据。
- location_category 返 dining 的静态 slug 恰为 ['cafe','tavern']；全 34 条静态条目无一条带 category 或 capabilities 键。
- 基线绿：tests/test_map_data.py + tests/test_agent_actions.py + tests/test_lab_building.py = 41 passed。

#### 先写的测试（必须跑出失败）

无。本 step 不改代码、不产生 commit。执行者读完依赖图与两处校正后直接进入 P1-S1。

#### 实现

无代码改动。

执行纪律（对应硬约束）：
- 每 step 严格先跑 test_first 拿红，再落 implementation 跑绿，再 commit。
- 全部 10 个 step 无一条迁移：P1 的动态侧迁移是 no-op（post_office/theater 今天既非 dining 也非 research，不写 capabilities 键即与今天逐位相同）。首个真实动态回填在 P2。
- 全部 10 个 step 无一条开闸：LOCATION_CAPABILITIES_ENABLED 引入即默认 False，两份 env 模板也写 false。开闸是批 3 的事。
- ActionType 一个成员不加；tests/test_lab_building.py:85-88 的 len==16 / actions[14]==RESEARCH 断言全程不得触碰。

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_map_data.py tests/test_agent_actions.py tests/test_lab_building.py tests/test_env_example_consistency.py -q
```

**验收**：上述命令 passed 且 failed=0（实测 41 + env 一致性用例全绿）。执行者能复述：S9 可并行；S3 必须同批改 backend/.env.example。

**commit**：

```
无 commit
```

### P1-S1 — 新增 app/agent/location_caps.py：能力闭集注册表 + 声明归一化 🔧

**Flag / 批次**：无（不引入 flag：纯新增模块、零生产调用方、行为面为零）

**为什么**：能力名必须是闭集，否则公投能造出 capabilities:["DANCE"] 进入行为链（现存同类漏洞：prompts.py:80 把 boosted_actions 裸拼进 system prompt 不校验，parse_action_result 会静默丢弃整 tick 且已花 LLM 钱）。

模块不 import 任何 app 模块：它被 map_data 与 actions 双向引用，任一方向 import 都会成环（这两个文件今天全靠惰性 import 绕这个坑）。因此 unlocks 存 ActionType 的 value 字符串，由消费侧自行 ActionType(v) 还原。

civic_grantable=False 是安全边界：公投/Lab 永远不能给任何楼授予 research，否则等于绕过 has_trusted_lab_access 的实验楼地点门（P3 落库前用这个白名单过滤）。

纯函数模块，无闸、无 I/O、无生产调用方 —— 落地后行为面为零。

#### 先写的测试（必须跑出失败）

文件：/Volumes/data/dev/simverse-world/backend/tests/test_location_caps.py

```python
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
    CAP_RESEARCH,
    CAPABILITIES,
    CIVIC_GRANTABLE_CAPABILITIES,
    CapabilitySpec,
    capability_unlocks,
    normalize_capabilities,
)


def test_registry_is_a_closed_set_of_three():
    assert set(CAPABILITIES) == {CAP_DINING, CAP_RESEARCH, CAP_MARKET}
    assert all(isinstance(v, CapabilitySpec) for v in CAPABILITIES.values())
    assert all(name == spec.name for name, spec in CAPABILITIES.items())


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
    assert CIVIC_GRANTABLE_CAPABILITIES == frozenset({CAP_DINING})
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
```

失败形态：ModuleNotFoundError: No module named 'app.agent.location_caps' → collection error。

#### 实现

新建文件：/Volumes/data/dev/simverse-world/backend/app/agent/location_caps.py（全文）

```python
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
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_location_caps.py -q
```

**验收**：1. 实现前同一命令得到 collection error（ModuleNotFoundError: app.agent.location_caps）。2. 实现后 12 个用例全 passed。3. grep -nE '^\s*(from|import)\s+app\b' app/agent/location_caps.py 零命中。4. git diff --stat 只含 2 个新文件，零既有文件改动。

**commit**：

```
feat(map): 地点能力闭集注册表与声明归一化——纯词表模块,零生产调用方
```

> #### 🔧 本 step 已被 critic 修订（5 处）
>
> 执行时以下列补丁为准，逐条覆盖上文对应字段。
>
> **修订 1 — 🔴 blocker · 字段 `implementation`**
>
> 处置：blocker-2 /【capabilities 权威裁决】「P1-S1 的 CAPABILITIES 必须追加 CAP_POSTAL 与 CAP_STAGE」；recheck_round2「P2 需要的 postal/stage 必须先在 P1-S1 的 CAPABILITIES 里登记(civic_grantable=True)」。
>
> 定位锚点：
>
> ```
> CAP_RESEARCH = "research"
> CAP_MARKET = "market"
> ```
>
> 替换为：
>
> CAP_RESEARCH = "research"
> CAP_MARKET = "market"
> # P2 全段的硬依赖(plan_P2_postal.json notes「新增依赖边 A」逐字给定)。不登记 →
> # normalize_capabilities 只 logger.debug 静默丢弃 → 邮局/剧院的能力门永远拿不到,
> # 全链零告警。P2-S1 的第一条测试就是这条依赖边的守卫。
> CAP_POSTAL = "postal"
> CAP_STAGE = "stage"
>
> **修订 2 — 🔴 blocker · 字段 `implementation`**
>
> 处置：blocker-2。unlocks=() 是「零新增 ActionType」的机器表述(P2 三段各带 len(list(ActionType))==16 复述)；category=None 防止污染 location_category → EAT / nearest_dining 通路；civic_grantable=True 是 P3-S4 白名单过滤的唯一真值来源。
>
> 定位锚点：
>
> ```
> unlocks=(), category=None, civic_grantable=False),
> ```
>
> 替换为：
>
>         CAP_MARKET, unlocks=(), category=None, civic_grantable=False),
>     CAP_POSTAL: CapabilitySpec(
>         CAP_POSTAL, unlocks=(), category=None, civic_grantable=True),
>     CAP_STAGE: CapabilitySpec(
>         CAP_STAGE, unlocks=(), category=None, civic_grantable=True),
>
> **修订 3 — 🔴 blocker · 字段 `test_first`**
>
> 处置：blocker-2 的连带：新增两个常量后测试文件的 import 清单同批扩充，否则 NameError。
>
> 定位锚点：
>
> ```
>     CAP_DINING,
>     CAP_MARKET,
>     CAP_RESEARCH,
> ```
>
> 替换为：
>
>     CAP_DINING,
>     CAP_MARKET,
>     CAP_POSTAL,
>     CAP_RESEARCH,
>     CAP_STAGE,
>
> **修订 4 — 🔴 blocker · 字段 `test_first`**
>
> 处置：blocker-2（裁决要求「同步改 test_registry_is_a_closed_set_of_three（改名+期望集合改 5 个）」）。本片段替换该函数全文并追加一条 inert 守卫。
>
> 定位锚点：
>
> ```
> def test_registry_is_a_closed_set_of_three():
> ```
>
> 替换为：
>
> def test_registry_is_a_closed_set_of_five():
>     assert set(CAPABILITIES) == {CAP_DINING, CAP_RESEARCH, CAP_MARKET,
>                                  CAP_POSTAL, CAP_STAGE}
>     assert all(isinstance(v, CapabilitySpec) for v in CAPABILITIES.values())
>     assert all(name == spec.name for name, spec in CAPABILITIES.items())
>
>
> def test_postal_and_stage_are_inert_but_civic_grantable():
>     """P2 只拿它们当「站在哪」的门:不解锁动作(零新增 ActionType)、不派生
>     category(不得污染 EAT / nearest_dining 通路)、公投可授予。"""
>     for cap in (CAP_POSTAL, CAP_STAGE):
>         assert CAPABILITIES[cap].unlocks == ()
>         assert CAPABILITIES[cap].category is None
>         assert CAPABILITIES[cap].civic_grantable is True
>
> **修订 5 — 🔴 blocker · 字段 `test_first`**
>
> 处置：blocker-2（裁决要求「同步改 …与 CIVIC_GRANTABLE_CAPABILITIES 的断言」）。research/market 仍 civic_grantable=False，本函数余下两条断言不变，安全边界语义未松。
>
> 定位锚点：
>
> ```
> CIVIC_GRANTABLE_CAPABILITIES == frozenset({CAP_DINING})
> ```
>
> 替换为：
>
> CIVIC_GRANTABLE_CAPABILITIES == frozenset(
>         {CAP_DINING, CAP_POSTAL, CAP_STAGE})
>

### P1-S2 — map_data 加能力查询：location_capabilities / has_capability / capability_param 🔧

**Flag / 批次**：无（纯查询函数不挂闸；闸只加在调用点，见 S3/S6/S7）

**为什么**：把注册表接到地点数据上。三条不变量在这里成立：

1. category 无条件折进 capability：location_category(x)==dining 的地点无条件视为拥有 dining 能力，保证 has_capability(x,dining) ⊇ (location_category(x)==dining)。任何既有 category 驱动的行为（EAT 门、nearest_dining_location）不可能因为忘写声明而丢失。
2. 不成环：location_capabilities 调 location_category；location_category（S3 改造后）只调纯字典读 _declared_capabilities，不回调 location_capabilities。
3. 缺省安全：老 dynamic_locations 行没有 capabilities 键 → 归一成 {} → 不解锁任何东西。

本 step 三个函数无生产调用方（S6 才接线），且不挂闸 —— 纯查询函数挂闸会让闸点集合失控；只有调用点读 flag，闸点因此有限且可审计（最终共 3 处：location_category / actions.py 两个门 / _charge_meal）。

#### 先写的测试（必须跑出失败）

文件：/Volumes/data/dev/simverse-world/backend/tests/test_location_capabilities.py

```python
"""P1-S2: map_data 侧的能力查询(纯读,无闸,本 step 内零生产调用方)。

核心不变量:has_capability(x,dining) 是 (location_category(x)==dining) 的超集。
这条保证任何既有 category 驱动的行为(actions.py:137 的 EAT 门 /
map_data.nearest_dining_location)不可能因为忘写声明而丢失 —— 它是 capability 与
category 共存而非取代的机器化表述。
"""
import pytest

from app.agent.location_caps import CAP_DINING, CAP_MARKET, CAP_RESEARCH
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
```

失败形态：ImportError: cannot import name 'location_capabilities' from 'app.agent.map_data'。

#### 实现

改文件：/Volumes/data/dev/simverse-world/backend/app/agent/map_data.py

锚点：在 location_category 结束行（map_data.py:282 的 `    return "dining" if loc_id in _DINING_LOCATIONS else None`）与 nearest_dining_location（map_data.py:285）之间插入整块。

before（map_data.py:280-286）：
```python
    if loc and loc.get("category"):
        return loc["category"]
    return "dining" if loc_id in _DINING_LOCATIONS else None


def nearest_dining_location(from_tile: tuple[int, int]) -> str | None:
    """Nearest dining-category location entrance to ``from_tile``."""
```

after：
```python
    if loc and loc.get("category"):
        return loc["category"]
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


def _declared_capabilities(loc: dict | None) -> dict[str, dict]:
    """一条地点 dict 上显式声明的能力(已归一、已丢弃未知项)。纯字典读。"""
    from app.agent.location_caps import normalize_capabilities
    if not loc:
        return {}
    return normalize_capabilities(loc.get("capabilities"))


def location_capabilities(loc_id: str | None) -> frozenset[str]:
    """该地点提供的能力集合 = 显式声明 并上 category 派生。"""
    if not loc_id:
        return frozenset()
    from app.agent.location_caps import CAP_DINING
    caps = set(_declared_capabilities(get_location_by_id(loc_id)))
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
    错付给别人(execute/basic.py:56 的 else 分支就是这个历史 bug)。
    """
    if not loc_id:
        return default
    params = _declared_capabilities(get_location_by_id(loc_id)).get(cap)
    if not params:
        return default
    value = params.get(key, default)
    return default if value is None else value


def nearest_dining_location(from_tile: tuple[int, int]) -> str | None:
    """Nearest dining-category location entrance to ``from_tile``."""
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_location_capabilities.py tests/test_map_data.py tests/test_location_caps.py -q
```

**验收**：1. 实现前红（ImportError: cannot import name 'location_capabilities'）。2. 实现后三个文件全 passed（新增 12 用例 + test_map_data 原 20 用例不变）。3. git diff --numstat app/agent/map_data.py 的 deletions 列为 0（纯插入）。

**commit**：

```
feat(map): 地点能力查询 location_capabilities/has_capability/capability_param
```

> #### 🔧 本 step 已被 critic 修订（1 处）
>
> 执行时以下列补丁为准，逐条覆盖上文对应字段。
>
> **修订 1 — 🟠 major · 字段 `implementation`**
>
> 处置：critic_adv/recheck_round2「P1 自己声明的安全边界在 P1 内零执行：CIVIC_GRANTABLE_CAPABILITIES 无任何消费方，research 就在 CAPABILITIES 里照收」的 fix(b)「把执行点前移到 P1」。
>
> 定位锚点：
>
> ```
> def _declared_capabilities(loc: dict | None)
> ```
>
> 替换为：
>
> （本片段替换 _declared_capabilities 全文，并要求同批在 map_data 顶部补 `import logging` + `logger = logging.getLogger(__name__)`——实测该文件今天零 logger）
>
> ```python
> def _declared_capabilities(loc: dict | None,
>                           loc_id: str | None = None) -> dict[str, dict]:
>     """一条地点 dict 上显式声明的能力(已归一、已丢弃未知项)。纯字典读。
>
>     动态行(公投建楼)额外按 CIVIC_GRANTABLE_CAPABILITIES 白名单降级 —— 这是把 S1
>     rationale 里「公投永远不能授予 research」那条安全边界在 P1 内**真正执行**,不
>     依赖 P3 任何一道闸的开闸顺序:routers/polls.py:94 允许 admin 附带任意 effect
>     dict,civic_service._add_dynamic_location 只校验 slug 非空 + \"bounds\" in data
>     就整包落库。降级只作用于 _dynamic_slugs(map_data.py:348),静态四条不受影响。
>     """
>     from app.agent.location_caps import (
>         CIVIC_GRANTABLE_CAPABILITIES, normalize_capabilities)
>     if not loc:
>         return {}
>     caps = normalize_capabilities(loc.get("capabilities"))
>     if loc_id is not None and loc_id in _dynamic_slugs:
>         dropped = sorted(set(caps) - CIVIC_GRANTABLE_CAPABILITIES)
>         if dropped:
>             logger.warning(
>                 "dynamic location %s declared non-grantable capabilities %s",
>                 loc_id, dropped)
>         caps = {k: v for k, v in caps.items()
>                 if k in CIVIC_GRANTABLE_CAPABILITIES}
>     return caps
> ```
>
> 三个调用点同批传 loc_id：`location_capabilities` 与 `capability_param` 改成
> `_declared_capabilities(get_location_by_id(loc_id), loc_id)`；S3 的 location_category
> 改成 `_declared_capabilities(loc, loc_id)`。`_dynamic_slugs` 定义在本函数之后，
> Python 调用时解析名字，顺序无关。
>
> 同批补测试（tests/test_location_capabilities.py）：往 LOCATIONS 追加一条带
> `capabilities:{"research":{}}` 的行并加进 `map_data._dynamic_slugs`，断言
> `has_capability(slug, CAP_RESEARCH) is False`；再断言同一条行声明 `postal` 时
> `has_capability(slug, CAP_POSTAL) is True`（P2 邮局侧的正向通路不能被误伤）。
>

### P1-S3 — 引入 LOCATION_CAPABILITIES_ENABLED（默认关）+ location_category 加能力派生层

**Flag / 批次**：新增 location_capabilities_enabled: bool = False（env LOCATION_CAPABILITIES_ENABLED，backend/.env.example 同步写 false）。默认关 = 逐字节旧行为。非迁移批次（纯代码 + 模板文档，零 DB 改动）。

**为什么**：P1 唯一的语义闸。三级优先级：显式 category 键 → （闸开时）capability 派生 → _DINING_LOCATIONS 白名单。白名单（map_data.py:269）不删 —— 删它则一旦某处声明没落地就静默失去 dining，纯负收益；它降级为最后一级 fallback。

闸关时 location_category 逐字节走旧路径（新代码整块被 if 跳过），返回值域与顺序不变。

必须同批改 backend/.env.example：tests/test_env_example_consistency.py 的 test_every_settings_field_is_documented_or_allowlisted 强制每个 Settings 字段都要有 example 行，漏了这一步该测试当场变红（这是执行时最容易踩的坑）。

派生时对能力名 sorted() 迭代：CAPABILITIES 今天只有 dining 带 category，但排序保证将来加第二个带 category 的能力时结果确定，不受 dict 顺序影响。

#### 先写的测试（必须跑出失败）

文件：/Volumes/data/dev/simverse-world/backend/tests/test_location_capability_gate.py

```python
"""P1-S3: LOCATION_CAPABILITIES_ENABLED 闸 + location_category 的能力派生层。

闸关 = location_category 逐字节旧行为(显式 category 键 → _DINING_LOCATIONS 白名单)。
闸开 = 中间多插一级「从声明派生」。白名单不删:它是最后一级 fallback,删掉的话一旦
某处声明没落地就静默失去 dining。
"""
from pathlib import Path

import pytest

from app.agent.location_caps import CAP_DINING
from app.agent.map_data import LOCATIONS, _DINING_LOCATIONS, location_category
from app.config import Settings, settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


@pytest.fixture
def temp_location():
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


def test_flag_defaults_to_off():
    assert Settings.model_fields["location_capabilities_enabled"].default is False


def test_flag_is_documented_as_false_in_backend_env_example():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "LOCATION_CAPABILITIES_ENABLED=false" in text


def test_dining_allowlist_is_not_deleted():
    assert _DINING_LOCATIONS == {"cafe", "tavern"}


@pytest.mark.parametrize("slug", sorted(LOCATIONS))
def test_category_is_identical_with_the_flag_on_and_off(slug, monkeypatch):
    monkeypatch.setattr(settings, "location_capabilities_enabled", False)
    legacy = location_category(slug)
    monkeypatch.setattr(settings, "location_capabilities_enabled", True)
    assert location_category(slug) == legacy, slug


def test_cafe_and_tavern_stay_dining_under_both_flag_states(monkeypatch):
    for state in (False, True):
        monkeypatch.setattr(settings, "location_capabilities_enabled", state)
        assert location_category("cafe") == "dining"
        assert location_category("tavern") == "dining"


def test_declaration_derives_a_category_only_when_the_flag_is_on(
        temp_location, monkeypatch):
    temp_location("t_canteen", {"capabilities": {CAP_DINING: {}}})
    monkeypatch.setattr(settings, "location_capabilities_enabled", False)
    assert location_category("t_canteen") is None
    monkeypatch.setattr(settings, "location_capabilities_enabled", True)
    assert location_category("t_canteen") == "dining"


def test_explicit_category_key_still_wins_over_the_declaration(
        temp_location, monkeypatch):
    temp_location("t_odd", {"category": "lodging",
                           "capabilities": {CAP_DINING: {}}})
    for state in (False, True):
        monkeypatch.setattr(settings, "location_capabilities_enabled", state)
        assert location_category("t_odd") == "lodging"


def test_capability_without_a_category_derives_nothing(
        temp_location, monkeypatch):
    temp_location("t_shed", {"capabilities": {"research": {}, "market": {}}})
    monkeypatch.setattr(settings, "location_capabilities_enabled", True)
    assert location_category("t_shed") is None


def test_nearest_dining_location_picks_up_a_declared_diner_when_the_flag_is_on(
        temp_location, monkeypatch):
    """「顺着 category 长」的红利:nearest_dining_location 一行不改就认新声明。"""
    from app.agent.map_data import nearest_dining_location
    temp_location("t_near", {"bounds": (0, 0, 2, 2), "center": (1, 1),
                            "entrance": (1, 1),
                            "capabilities": {CAP_DINING: {}}})
    monkeypatch.setattr(settings, "location_capabilities_enabled", False)
    assert nearest_dining_location((1, 1)) in _DINING_LOCATIONS
    monkeypatch.setattr(settings, "location_capabilities_enabled", True)
    assert nearest_dining_location((1, 1)) == "t_near"
```

失败形态：KeyError: 'location_capabilities_enabled'。

#### 实现

改动 1：/Volumes/data/dev/simverse-world/backend/app/config.py

锚点：`realism_gossip_event_lane_enabled: bool = False`（config.py:592，grep -c 确认全文唯一）。

before（config.py:591-593）：
```python
    realism_crowd_enabled: bool = False
    realism_gossip_event_lane_enabled: bool = False
    # P2 Task 1 — relation write deltas (reused, zero new LLM calls) + decay.
```

after：
```python
    realism_crowd_enabled: bool = False
    realism_gossip_event_lane_enabled: bool = False

    # --- P1 地点能力声明 (LOCATION_CAPABILITIES_*) ---
    # 把 dining/research 的硬编码 slug 门改成读地点自己的 capabilities 声明。
    # 关 = 逐字节旧行为(字面量 "experiment_building" / _DINING_LOCATIONS 白名单)。
    # 与 realism_enabled 正交:EAT 门本来就在 realism 内层,本闸是内层再套一层。
    # 声明随代码先落地,与开闸分属不同批次(07-25 事故红线)。
    location_capabilities_enabled: bool = False

    # P2 Task 1 — relation write deltas (reused, zero new LLM calls) + decay.
```

改动 2：/Volumes/data/dev/simverse-world/backend/app/agent/map_data.py —— 用下段整体替换 location_category（原 map_data.py:272-282）：

```python
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
        for cap in sorted(_declared_capabilities(loc)):
            spec = CAPABILITIES.get(cap)
            if spec and spec.category:
                return spec.category
    return "dining" if loc_id in _DINING_LOCATIONS else None
```

注：_declared_capabilities 定义在本函数之后（S2 插在 :282 之下），Python 调用时解析名字，顺序无关；且这里只调纯字典读的 _declared_capabilities，不回调 location_capabilities，因此不成环。

改动 3：/Volumes/data/dev/simverse-world/backend/.env.example

锚点：`REALISM_GOSSIP_EVENT_LANE_ENABLED=false  # staged: reserve recent event_id rumors`（.env.example:534）。在它与下一行 `# P2 Task 1 ...` 之间插入：

```

# ── P1 地点能力声明（LOCATION_CAPABILITIES_ENABLED）────────────────────────────
# 关（默认）= 逐字节旧行为：RESEARCH 门比字面量 experiment_building，dining 走
# _DINING_LOCATIONS={cafe,tavern} 白名单，餐费按 cafe_host/tavern_hub 硬编码分账。
# 开 = 这三处改读地点自己的 capabilities 声明；声明已随代码落地（cafe/tavern/
# experiment_building/market_hall 四条静态条目），开闸不带任何数据变更。
# 与 REALISM_ENABLED 正交：EAT 门本来就在 realism 内层，本闸是内层再套一层。
# 开闸前先确认 tests/test_capability_action_gates.py 的等价用例在本机为绿。
LOCATION_CAPABILITIES_ENABLED=false
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_location_capability_gate.py tests/test_env_example_consistency.py tests/test_map_data.py tests/test_realism_needs.py tests/test_meal_revenue.py -q
```

**验收**：1. 实现前 test_location_capability_gate.py 红（KeyError: 'location_capabilities_enabled'）。2. 实现后全绿：34 条静态地点的 location_category 在闸开/闸关下参数化对拍逐条相等；test_env_example_consistency 全绿。3. `.venv/bin/python -c "from app.config import Settings; assert Settings.model_fields['location_capabilities_enabled'].default is False"` 退出 0。

**commit**：

```
feat(map): location_category 加能力派生层,挂 LOCATION_CAPABILITIES_ENABLED 默认关
```

### P1-S4 — 四条静态 capabilities 声明 + _STATIC_LOCATION_SLUGS 防漂移守卫

**Flag / 批次**：location_capabilities_enabled=False（沿用 S3，不改动）。非迁移批次：动态侧（post_office/theater）在 P1 不需要任何声明——它们今天既非 dining 也非 research，缺键即缺省安全，与今天逐位相同。首个真实动态回填在 P2。

**为什么**：给 cafe / tavern / experiment_building / market_hall 写上声明。这是 P1 的全部静态数据面，且仅此四条。

- cafe → {dining: {host_duty: cafe_host}}、tavern → {dining: {host_duty: tavern_hub}}：host_duty 脱离 dining 毫无意义，所以参数作用域跟着能力走（这也是选 dict[str,dict] 而非 list[str]+平级键的理由）。
- market_hall → {market: {}} 仅供发现/导流，明令禁止用于场地解析：场地权威是 settings.market_day_venue + event_location.resolve_event_location_id()，而路网几何（caravan_route._MARKET_AVENUE_X_BOUNDS / map_data.py:204 的 caravan_parking）是按这一栋楼的实际瓦片手调的。改成能力反查一旦出现第二个 market-capable 地点，cohort 判据 / decide 目的地 / 商队停车锚点会指向不同的楼，静默分裂。

_STATIC_LOCATION_SLUGS 是 import 期的静态快照，用于只锁静态集合的防漂移守卫：既防代码误声明第二个 research 地点，又不挡 P3 的公投建楼在数据侧扩展 dining。

实测：这四条 bounds 与全表零重叠 —— 这是 S5/S6 等价性的机器依据，本 step 顺手钉成测试。

#### 先写的测试（必须跑出失败）

文件：/Volumes/data/dev/simverse-world/backend/tests/test_location_capability_declarations.py

```python
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
```

失败形态：ImportError: cannot import name '_STATIC_LOCATION_SLUGS'。

#### 实现

改文件：/Volumes/data/dev/simverse-world/backend/app/agent/map_data.py（4 处字典键 + 1 处新常量）

锚点 1 — tavern（map_data.py:19-28），before 末两行：
```python
        "description": "热闹的社交场所，居民们喜欢在这里聊天和交换消息",
        "boosted_actions": ["CHAT_RESIDENT", "GOSSIP"],
    },
```
after：
```python
        "description": "热闹的社交场所，居民们喜欢在这里聊天和交换消息",
        "boosted_actions": ["CHAT_RESIDENT", "GOSSIP"],
        # host_duty 脱离 dining 毫无意义,所以参数作用域跟着能力走。
        "capabilities": {"dining": {"host_duty": "tavern_hub"}},
    },
```

锚点 2 — cafe（map_data.py:29-38），before 末两行：
```python
        "description": "安静的休闲场所，适合一对一的深度对话",
        "boosted_actions": ["CHAT_RESIDENT", "IDLE"],
    },
```
after：
```python
        "description": "安静的休闲场所，适合一对一的深度对话",
        "boosted_actions": ["CHAT_RESIDENT", "IDLE"],
        "capabilities": {"dining": {"host_duty": "cafe_host"}},
    },
```

锚点 3 — experiment_building（map_data.py:85-94），before 末两行：
```python
        "description": "小镇的元游戏入口：研究员在此接入隔离沙箱，完成玩家委托、产出世界变更提案",
        "boosted_actions": ["RESEARCH"],
    },
```
after：
```python
        "description": "小镇的元游戏入口：研究员在此接入隔离沙箱，完成玩家委托、产出世界变更提案",
        "boosted_actions": ["RESEARCH"],
        # 只收编「站在哪」这一半门槛;身份门 has_trusted_lab_access 不在能力体系内。
        # research 的 civic_grantable=False —— 公投永远不能给别的楼授予它。
        "capabilities": {"research": {}},
    },
```

锚点 4 — market_hall（map_data.py:197-208），before 末两行：
```python
        "description": "集市日开放的独立交易大厅，商队与本地摊主在此摆摊买卖",
        "boosted_actions": ["WORK", "OBSERVE"],
    },
```
after：
```python
        "description": "集市日开放的独立交易大厅，商队与本地摊主在此摆摊买卖",
        "boosted_actions": ["WORK", "OBSERVE"],
        # 仅供发现/导流(P3 冷启动、town_facts),禁止用于场地解析。场地权威是
        # settings.market_day_venue + event_location.resolve_event_location_id;
        # 路网几何(caravan_route._MARKET_AVENUE_X_BOUNDS / caravan_parking)是按这
        # 一栋楼的实际瓦片手调的,改成能力反查一旦出现第二个 market-capable 地点,
        # cohort 判据 / decide 目的地 / 商队停车锚点会指向不同的楼,静默分裂。
        "capabilities": {"market": {}},
    },
```

锚点 5 — 静态快照常量：紧跟 LOCATIONS 字面量收尾的 `}`（改动前是 map_data.py:240，加完 4 处声明后下移 8 行），插在它与 _find_location_in_bounds 之间。

before：
```python
}


def _find_location_in_bounds(x: int, y: int) -> tuple[str | None, dict | None]:
```
after：
```python
}

#: 模块 import 期的静态 slug 快照(不含动态合入的楼)。P1 的能力防漂移守卫只锁这个
#: 集合:既防代码误声明第二个 research 地点,又不挡 P3 的公投建楼在数据侧扩展
#: dining。load_dynamic_locations 只增删 LOCATIONS,不动这个 frozenset。
_STATIC_LOCATION_SLUGS: frozenset[str] = frozenset(LOCATIONS)


def _find_location_in_bounds(x: int, y: int) -> tuple[str | None, dict | None]:
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_location_capability_declarations.py tests/test_location_capability_gate.py tests/test_map_data.py tests/test_map_integration.py tests/test_plan_public_memories.py tests/test_world_governance.py -q
```

**验收**：1. 实现前红（ImportError: _STATIC_LOCATION_SLUGS）。2. 实现后全绿，其中 test_dining_capability_set_equals_the_legacy_category_set 在闸开/闸关两态下都得到 {cafe, tavern}。3. tests/test_map_data.py::test_locations_has_all_entries（len==34）与 tests/test_plan_public_memories.py 的冻结快照全部不变。4. `.venv/bin/python -c "from app.agent.map_data import format_location_list_for_prompt as f; assert 'capabilit' not in f()"` 退出 0。

**commit**：

```
feat(map): cafe/tavern/实验楼/集市大厅四条能力声明 + 静态防漂移守卫
```

### P1-S5 — 新增 capability_location_at：绕开 outdoor 街区遮蔽的能力反查

**Flag / 批次**：location_capabilities_enabled=False（沿用；本函数本 step 内零生产调用方，只被测试调用）。

**为什么**：这是 P2 的硬依赖，也是 P1 必须自带的一块。

get_location_id_at 走 _find_location_in_bounds（map_data.py:243-249）首命中即返，命中序 = dict 插入序 = 静态在前、动态追加在尾（map_data.py:386）。生产两栋公投楼完全落在 outdoor 大街区内部：post_office(44,100,48,106) 在 south_quarter(42,100,135,109) 内、theater(172,40,178,50) 在 east_gardens(140,35,179,58) 内。于是 get_location_id_at(46,103) 返 south_quarter、(172,45) 返 east_gardens —— 任何以「站在楼里」为门的能力，对邮局/剧院今天恒为假。P2 若直接复用 get_location_id_at，命中率恒为 0。

所以新增 capability_location_at(x, y, cap)：扫描全部 bounds 命中且声明该 cap 的地点，取 bounds 面积最小者（最具体），平局取插入序先者。

刻意不改 get_location_id_at 的首命中契约：location_tracker._build_lookup（location_tracker.py:31-42）的 setdefault 与它同序且注释自陈必须同序，动它会波及首访 / lore / /exploration/me / 成就。那是 P3 的活。

等价性：四条已声明的静态 bounds 与全表零重叠（S4 已钉死），故对它们「最小面积匹配」== 「首命中」== 它自己。

#### 先写的测试（必须跑出失败）

文件：/Volumes/data/dev/simverse-world/backend/tests/test_capability_location_at.py

```python
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
```

失败形态：ImportError: cannot import name 'capability_location_at'。

注：test_malformed_bounds / test_location_without_bounds_key 会命中 _find_location_in_bounds 的硬下标 loc["bounds"]，因此这两条用例里 get_location_id_at 不被调用（只调 capability_location_at），这是刻意的——修 _find_location_in_bounds 的硬下标是 P3 的活。

#### 实现

改文件：/Volumes/data/dev/simverse-world/backend/app/agent/map_data.py

锚点：紧接 S2 插入的 capability_param 函数体之后、nearest_dining_location 之前，插入：

```python
def capability_location_at(x: int, y: int, cap: str) -> str | None:
    """站在 (x,y) 时提供 cap 的地点 id —— bounds 命中且声明该能力者中面积最小
    (最具体)的那个,平局取 LOCATIONS 插入序先者。

    为什么不复用 get_location_id_at:它是首命中即返(map_data.py:243-249),命中序 =
    dict 插入序 = 静态在前、动态追加在尾(:386)。生产两栋公投楼
    post_office(44,100,48,106) 与 theater(172,40,178,50) 分别完全落在 outdoor 街区
    south_quarter(42,100,135,109) / east_gardens(140,35,179,58) 内部,首命中永远返回
    街区 —— 任何以「站在楼里」为门的能力对它们恒为假(实测
    get_location_id_at(46,103) == "south_quarter")。

    这里换成「最具体者优先」把能力门从遮蔽里摘出来,同时不动 get_location_id_at 的
    首命中契约:location_tracker._build_lookup 的 setdefault 与它同序且注释自陈必须
    同序(location_tracker.py:26-27),改它会波及首访事件、location_lore、
    /exploration/me。那是 P3 的活。

    bounds 用 .get 防御性读取:civic_service._add_dynamic_location 零几何校验,一条
    畸形行不得让本查询崩。
    """
    best_id: str | None = None
    best_area: int | None = None
    for loc_id, loc in LOCATIONS.items():
        bounds = loc.get("bounds")
        if not bounds or len(bounds) != 4:
            continue
        try:
            x1, y1, x2, y2 = (int(v) for v in bounds)
        except (TypeError, ValueError):
            continue
        if not (x1 <= x <= x2 and y1 <= y <= y2):
            continue
        if cap not in location_capabilities(loc_id):
            continue
        area = (abs(x2 - x1) + 1) * (abs(y2 - y1) + 1)
        if best_area is None or area < best_area:
            best_id, best_area = loc_id, area
    return best_id
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_capability_location_at.py tests/test_map_data.py tests/test_location_tracker.py tests/test_lab_building.py -q
```

**验收**：1. 实现前红（ImportError: capability_location_at）。2. 实现后全绿：test_masking_is_real_and_get_location_id_at_keeps_its_contract 证明 get_location_id_at 契约未变，test_capability_lookup_sees_through_the_outdoor_mask 证明遮蔽已绕开。3. git diff --numstat app/agent/map_data.py 的 deletions 列为 0（纯插入，未修改 _find_location_in_bounds 一个字符）。

**commit**：

```
feat(map): capability_location_at 按最具体地点反查——绕开 outdoor 街区遮蔽
```

### P1-S6 — actions.py 两个地点门改读能力声明（挂闸），RESEARCH 身份门不动 🔧

**Flag / 批次**：location_capabilities_enabled=False（沿用）。闸关时两个门执行的是原表达式一字未改。

**为什么**：P1 的行为接线核心。两处替换：actions.py:130 的 `get_location_id_at(x,y) == "experiment_building"` → `capability_location_at(x, y, CAP_RESEARCH) is not None`；actions.py:137 的 `location_category(get_location_id_at(x,y)) == "dining"` → `capability_location_at(x, y, CAP_DINING) is not None`。

等价性由 S4 钉死的「四条 bounds 与全表零重叠」+ S3 钉死的「闸开闸关 category 逐条相等」共同保证；本 step 再加一层最强的机器证明：对每一条静态地点的 center，闸开与闸关下 get_available_actions 的返回集合逐条相等。

明确不碰：has_trusted_lab_access（actions.py:127-129）原样保留——能力只收编「站在哪」那一半，身份门不在能力体系内；ActionType 一个成员不加。

#### 先写的测试（必须跑出失败）

文件：/Volumes/data/dev/simverse-world/backend/tests/test_capability_action_gates.py

```python
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


def test_action_type_enum_is_untouched():
    """P1 是门的数据化,不是新动作。"""
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH
    assert actions[15] == ActionType.EAT
```

失败形态：test_masked_dynamic_diner_unlocks_eat_only_when_the_flag_is_on 在闸开时断言 EAT in ... 失败（今天 get_location_id_at(46,103) 返 south_quarter，EAT 永不解锁）。

#### 实现

改文件：/Volumes/data/dev/simverse-world/backend/app/agent/actions.py

改动 1 — RESEARCH 门（actions.py:122-131）。

before：
```python
    # RESEARCH (Lab): gated to authorized researchers standing inside the
    # experiment building. meta_json["lab"]["access"] is the admin-granted
    # whitelist flag (spec §14 "研究员资格：先手动授权"). This keeps the real
    # sandbox entirely off the tick — RESEARCH is narrative-only.
    from app.services.resident_privilege_policy import has_trusted_lab_access
    if has_trusted_lab_access(resident):
        from app.agent.map_data import get_location_id_at
        if get_location_id_at(resident.tile_x, resident.tile_y) == "experiment_building":
            available.append(ActionType.RESEARCH)
```

after：
```python
    # RESEARCH (Lab): gated to authorized researchers standing inside the
    # experiment building. meta_json["lab"]["access"] is the admin-granted
    # whitelist flag (spec §14 "研究员资格：先手动授权"). This keeps the real
    # sandbox entirely off the tick — RESEARCH is narrative-only.
    #
    # P1: 只有「站在哪」这一半改成读地点能力声明;身份门 has_trusted_lab_access
    # 原样保留,能力永远不能替代它(research 的 civic_grantable=False,公投也授不出
    # 这项能力)。闸关 = 逐字节的字面量比较。
    from app.services.resident_privilege_policy import has_trusted_lab_access
    if has_trusted_lab_access(resident):
        from app.config import settings as _cap_settings
        if _cap_settings.location_capabilities_enabled:
            from app.agent.location_caps import CAP_RESEARCH
            from app.agent.map_data import capability_location_at
            _research_here = capability_location_at(
                resident.tile_x, resident.tile_y, CAP_RESEARCH) is not None
        else:
            from app.agent.map_data import get_location_id_at
            _research_here = (
                get_location_id_at(resident.tile_x, resident.tile_y)
                == "experiment_building")
        if _research_here:
            available.append(ActionType.RESEARCH)
```

改动 2 — EAT 门（actions.py:133-138）。

before：
```python
    # EAT (realism P1-10): only inside a dining-category location.
    from app.config import settings as _settings
    if _settings.realism_enabled:
        from app.agent.map_data import location_category, get_location_id_at
        if location_category(get_location_id_at(resident.tile_x, resident.tile_y)) == "dining":
            available.append(ActionType.EAT)
```

after：
```python
    # EAT (realism P1-10): only inside a dining-category location.
    # P1: 闸开时改走能力反查 —— 除了收编白名单,还顺带绕开 outdoor 街区遮蔽
    # (get_location_id_at 首命中即返,公投楼落在大街区里就永远查不出来)。
    from app.config import settings as _settings
    if _settings.realism_enabled:
        if _settings.location_capabilities_enabled:
            from app.agent.location_caps import CAP_DINING
            from app.agent.map_data import capability_location_at
            _dining_here = capability_location_at(
                resident.tile_x, resident.tile_y, CAP_DINING) is not None
        else:
            from app.agent.map_data import location_category, get_location_id_at
            _dining_here = (location_category(
                get_location_id_at(resident.tile_x, resident.tile_y)) == "dining")
        if _dining_here:
            available.append(ActionType.EAT)
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_capability_action_gates.py tests/test_agent_actions.py tests/test_lab_building.py tests/test_resident_privilege_policy.py tests/test_realism_needs.py tests/test_map_integration.py -q
```

**验收**：1. 实现前 test_masked_dynamic_diner_unlocks_eat_only_when_the_flag_is_on 红。2. 实现后全绿：34 条静态地点的 get_available_actions 集合在闸开/闸关下逐条相等；test_action_type_enum_is_untouched 保住 len==16 / actions[14]==RESEARCH。3. tests/test_lab_building.py 与 tests/test_resident_privilege_policy.py 的 RESEARCH 用例零改动全绿。

**commit**：

```
feat(agent): RESEARCH/EAT 地点门改读能力声明,挂闸默认关——身份门不动
```

> #### 🔧 本 step 已被 critic 修订（3 处）
>
> 执行时以下列补丁为准，逐条覆盖上文对应字段。
>
> **修订 1 — 🔴 blocker · 字段 `implementation`**
>
> 处置：blocker-1（recheck_round2 P1-S6 fix(a)：「同一 commit 内把 decide/basic.py:315-317 挂到同一道闸与同一个 resolver」）。锚点已回源码取真实文本（awk 逐行核对 decide/basic.py:314-318）。本片段自锚点行起替换 implementation 剩余全文。
>
> 定位锚点：
>
> ```
>         if _dining_here:
> ```
>
> 替换为：
>
>         if _dining_here:
>             available.append(ActionType.EAT)
> ```
>
> 改动 3 — decide 侧第三个 dining 消费点（**同 commit 必做**，否则闸开即饥饿死锁）。
> 文件：/Volumes/data/dev/simverse-world/backend/app/agent/phases/decide/basic.py
> 锚点：_maybe_needs_action 内 decide/basic.py:314-317（已回源码逐字核实）。
>
> before：
> ```python
>         # satiety
>         here = get_location_id_at(ctx.resident.tile_x, ctx.resident.tile_y)
>         if location_category(here) == "dining" and ActionType.EAT in ctx.available_actions:
>             return ActionResult(ActionType.EAT, here, None, "饿了，吃点东西")
> ```
>
> after：
> ```python
>         # satiety
>         # P1: 与 actions.py 的 EAT 门挂同一道闸、用同一个 resolver。口径分叉 =
>         # 「EAT 已解锁,_maybe_needs_action 却判此处不是餐馆」→ 走
>         # nearest_dining_location → 目标恰是脚下这栋楼 → VISIT_DISTRICT 到自己的
>         # entrance → execute already-at-destination → 不进食 → satiety 单调到 0,
>         # 而 most_critical 取 min 后恒返 satiety,GO_HOME 被永久挡在门外。这就是
>         # 0809「7/11 居民饿死在自家门口」的同型链。
>         if settings.location_capabilities_enabled:
>             from app.agent.location_caps import CAP_DINING
>             from app.agent.map_data import capability_location_at
>             here = capability_location_at(
>                 ctx.resident.tile_x, ctx.resident.tile_y, CAP_DINING)
>             dining_here = here is not None
>         else:
>             here = get_location_id_at(ctx.resident.tile_x, ctx.resident.tile_y)
>             dining_here = location_category(here) == "dining"
>         if dining_here and ActionType.EAT in ctx.available_actions:
>             return ActionResult(ActionType.EAT, here, None, "饿了，吃点东西")
> ```
>
> 注：闸开时 `here` 被重绑定成能力命中的楼 id —— 正是要写进 ActionResult 的 target
> （旧代码写的是被遮蔽的街区 id）。闸关分支与 :318 起的 nearest_dining 段一字未改。
>
> **修订 2 — 🔴 blocker · 字段 `test_first`**
>
> 处置：blocker-1（recheck_round2 P1-S6 fix(b)：「补一条不变量测试：ActionType.EAT in get_available_actions(r,[]) ⟹ _maybe_needs_action(ctx).action == ActionType.EAT，参数化跑遍 LOCATIONS + 一条被 south_quarter 遮蔽的动态餐饮楼」）。_maybe_needs_action 是同步方法，故不需 anyio；TickContext 构造照 tests/test_realism_needs.py:59-61 实测姿势。
>
> 定位锚点：
>
> ```
> def test_action_type_enum_is_untouched():
> ```
>
> 替换为：
>
> def _hungry(tile):
>     r = _resident(*tile)
>     r.meta_json = {"lab": {"access": True, "tier": "junior"},
>                    "needs": {"energy": 0.9, "satiety": 0.1, "social": 0.9}}
>     return r
>
>
> def _ctx_for(r):
>     from app.agent.schemas import TickContext
>     ctx = TickContext(db=MagicMock(), resident=r, world_time="12:00",
>                       hour=12, schedule_phase="午后")
>     ctx.available_actions = list(get_available_actions(r, []))
>     return ctx
>
>
> @pytest.mark.parametrize("slug", sorted(_STATIC_LOCATION_SLUGS))
> @pytest.mark.parametrize("flag", [False, True])
> def test_eat_available_implies_needs_action_picks_eat(slug, flag, realism_on):
>     """P1 收口不变量:EAT 可用 ⟹ _maybe_needs_action 选 EAT。
>
>     比任何手写期望表都硬,且能永久防住第四个 dining 消费点。
>     """
>     from app.agent.phases.decide.basic import BasicDecidePlugin
>     realism_on.setattr(settings, "location_capabilities_enabled", flag)
>     ctx = _ctx_for(_hungry(LOCATIONS[slug]["center"]))
>     if ActionType.EAT not in ctx.available_actions:
>         return
>     res = BasicDecidePlugin()._maybe_needs_action(ctx)
>     assert res is not None and res.action == ActionType.EAT, (slug, flag)
>
>
> def test_masked_dynamic_diner_eats_in_place_instead_of_walking_in_circles(
>         overlay, realism_on):
>     """被 south_quarter 遮蔽的动态餐饮楼:闸开后就地 EAT,target 是楼不是街区。"""
>     from app.agent.phases.decide.basic import BasicDecidePlugin
>     overlay("post_office", POST_OFFICE, capabilities={CAP_DINING: {}})
>     realism_on.setattr(settings, "location_capabilities_enabled", True)
>     res = BasicDecidePlugin()._maybe_needs_action(_ctx_for(_hungry((46, 103))))
>     assert res is not None and res.action == ActionType.EAT
>     assert res.target_slug == "post_office"
>
>
> def test_action_type_enum_is_untouched():
>
> **修订 3 — 🔴 blocker · 字段 `acceptance`**
>
> 处置：blocker-1（recheck_round2「在 P1-S6 的 acceptance 里写明该不变量是 P1 收口硬门」）。
>
> 定位锚点：
>
> ```
> tests/test_resident_privilege_policy.py 的 RESEARCH 用例零改动全绿。
> ```
>
> 替换为：
>
> tests/test_resident_privilege_policy.py 的 RESEARCH 用例零改动全绿。4. **P1 收口硬门**：不变量 `ActionType.EAT ∈ get_available_actions(r,[]) ⟹ _maybe_needs_action(ctx).action == ActionType.EAT` 在 34 条静态地点 × 闸开/闸关两态下逐条成立，且对被 south_quarter 遮蔽的 post_office 声明 dining 时 `res.target_slug == "post_office"`（不是 "south_quarter"）。这条不变量红 = 0809 型饥饿死锁复现，禁止进入 S7。5. `git diff app/agent/phases/decide/basic.py` 只含 satiety 段那一块，`grep -n "Case 2 (E-09" app/agent/phases/decide/basic.py` 仍有命中（未误伤英文注释）。
>

### P1-S7 — _charge_meal 的 host_duty 改读能力参数——堵掉第三个餐馆的错付 🔧

**Flag / 批次**：location_capabilities_enabled=False（沿用）。闸关时执行的是原表达式一字未改；错付修复只在闸开时生效，且今天不可达。

**为什么**：execute/basic.py:56 的 `key = "cafe_host" if loc_id == "cafe" else "tavern_hub"` 是纯粹的地点参数硬编码，且带一个潜伏的错付缺陷：任何新增的 dining 地点（含 P3 公投建的带 category=dining 的动态楼）会把餐费静默转给 tavern_hub 的持有者。

闸开后改读 capability_param(loc_id, CAP_DINING, "host_duty")，且必须能接住 None：第三个餐馆忘写 host_duty 时 key=None → 不调 find_duty_resident → host=None → 走 legacy sink debit。这是修复不是回归（今天第三个 dining 地点不存在，所以不可达）。

同时把 loc_id 的解析在闸开时统一到 capability_location_at，让「EAT 门认哪栋楼」与「餐费付给谁 / 赊账记忆写在哪」用同一口径——否则会出现 EAT 已解锁但 _charge_meal 因遮蔽拿到街区 id 的分叉。对 cafe/tavern（零重叠）两种口径同解，逐字节不变。

#### 先写的测试（必须跑出失败）

文件：/Volumes/data/dev/simverse-world/backend/tests/test_meal_host_capability.py

```python
"""P1-S7: 餐费分账的 duty key 从硬编码改读 dining 能力的 host_duty 参数。

只钉「解析出了哪个 duty key」这一件事 —— 转账/赊账/钱包缓存的完整语义由
tests/test_meal_revenue.py(真 sqlite + 真 coin_service)守着,这里不重复。所以本文件
把 coin_service / duty_service / feed_service 全部打桩,断言 find_duty_resident 收到
的 key。

最后两条是同一场景的两面:第三个 dining 地点在旧代码里被静默判成 tavern_hub
(execute/basic.py:56 的 else 分支),新代码在没写 host_duty 时退回 legacy sink。这是
修复不是回归 —— 今天第三个 dining 地点不存在,所以旧行为不可达。
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.location_caps import CAP_DINING
from app.agent.map_data import LOCATIONS
from app.agent.phases.execute import basic as execute_basic
from app.config import settings

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def temp_location():
    added: list[str] = []

    def _add(slug: str, extra: dict) -> str:
        assert slug not in LOCATIONS, slug
        data = {"name": "临时食堂", "type": "public",
                "bounds": (2, 2, 6, 6), "center": (4, 4), "entrance": (4, 4)}
        data.update(extra)
        LOCATIONS[slug] = data
        added.append(slug)
        return slug

    yield _add
    for slug in added:
        LOCATIONS.pop(slug, None)


@pytest.fixture
def duty_keys(monkeypatch):
    """记录 find_duty_resident 收到的 key;并把整条钱链打桩。"""
    from app.services import coin_service, duty_service, feed_service

    seen: list[str] = []

    async def _find(db, key):
        seen.append(key)
        host = MagicMock()
        host.slug = f"{key}-holder"
        host.id = f"{key}-id"
        host.name = key
        return host

    monkeypatch.setattr(duty_service, "find_duty_resident", _find)
    monkeypatch.setattr(duty_service, "set_wallet_cache", lambda db, r, b: None)
    monkeypatch.setattr(coin_service, "treasury_transfer", AsyncMock(return_value=True))
    monkeypatch.setattr(coin_service, "treasury_debit", AsyncMock(return_value=True))
    monkeypatch.setattr(coin_service, "treasury_balance", AsyncMock(return_value=100))
    monkeypatch.setattr(feed_service, "push", AsyncMock())
    monkeypatch.setattr(settings, "npc_trade_enabled", True)
    return seen


def _diner(tile):
    r = MagicMock()
    r.id = "diner-id"
    r.slug = "diner"
    r.name = "食客"
    r.tile_x, r.tile_y = tile
    return r


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("slug,expected", [("cafe", "cafe_host"),
                                           ("tavern", "tavern_hub")])
async def test_the_two_authored_diners_resolve_the_same_key_either_way(
        flag, slug, expected, duty_keys, monkeypatch):
    monkeypatch.setattr(settings, "location_capabilities_enabled", flag)
    await execute_basic._charge_meal(
        AsyncMock(), _diner(LOCATIONS[slug]["center"]))
    assert duty_keys == [expected]


@pytest.mark.parametrize("flag", [False, True])
async def test_no_duty_lookup_outside_a_dining_location(
        flag, duty_keys, monkeypatch):
    monkeypatch.setattr(settings, "location_capabilities_enabled", flag)
    await execute_basic._charge_meal(
        AsyncMock(), _diner(LOCATIONS["central_plaza"]["center"]))
    assert duty_keys == []


async def test_legacy_misroutes_a_third_diner_to_tavern_hub(
        temp_location, duty_keys, monkeypatch):
    """闸关 = 旧行为原样保留(含这个已知的错付缺陷)。"""
    temp_location("t_canteen", {"category": "dining",
                               "capabilities": {CAP_DINING: {}}})
    monkeypatch.setattr(settings, "location_capabilities_enabled", False)
    await execute_basic._charge_meal(AsyncMock(), _diner((4, 4)))
    assert duty_keys == ["tavern_hub"]


async def test_third_dining_location_does_not_misroute_meal_revenue_to_tavern_hub(
        temp_location, duty_keys, monkeypatch):
    """闸开 = 没写 host_duty 就退回 legacy sink debit,绝不错付给别人。"""
    temp_location("t_canteen", {"category": "dining",
                               "capabilities": {CAP_DINING: {}}})
    monkeypatch.setattr(settings, "location_capabilities_enabled", True)
    await execute_basic._charge_meal(AsyncMock(), _diner((4, 4)))
    assert duty_keys == []


async def test_declared_host_duty_is_honored(
        temp_location, duty_keys, monkeypatch):
    temp_location("t_canteen", {
        "capabilities": {CAP_DINING: {"host_duty": "canteen_cook"}}})
    monkeypatch.setattr(settings, "location_capabilities_enabled", True)
    await execute_basic._charge_meal(AsyncMock(), _diner((4, 4)))
    assert duty_keys == ["canteen_cook"]
```

失败形态：test_third_dining_location_does_not_misroute_meal_revenue_to_tavern_hub 红（今天闸开也走 else 分支，duty_keys == ['tavern_hub']）。

#### 实现

改文件：/Volumes/data/dev/simverse-world/backend/app/agent/phases/execute/basic.py

锚点：_charge_meal 内 execute/basic.py:51-57。

before：
```python
        cost = settings.npc_meal_cost_sc
        # 经营者解析提前一次,转账目标与赊账分支两用 (cafe_host / tavern_hub)。
        loc_id = get_location_id_at(resident.tile_x, resident.tile_y)
        host = None
        if location_category(loc_id) == "dining":
            key = "cafe_host" if loc_id == "cafe" else "tavern_hub"
            host = await find_duty_resident(db, key)
```

after：
```python
        cost = settings.npc_meal_cost_sc
        # 经营者解析提前一次,转账目标与赊账分支两用 (cafe_host / tavern_hub)。
        #
        # P1: 闸开时 duty key 改读地点 dining 能力的 host_duty 参数,并把 loc_id
        # 统一到 capability_location_at —— 与 actions.py 的 EAT 门同口径,否则会出现
        # 「EAT 已解锁但这里因 outdoor 遮蔽拿到街区 id」的分叉。host_duty 缺失时 key
        # 为 None → 不查 duty → 走 legacy sink debit;旧代码在这种情况下会把餐费静默
        # 转给 tavern_hub 的持有者(错付)。
        loc_id = get_location_id_at(resident.tile_x, resident.tile_y)
        host = None
        key = None
        if settings.location_capabilities_enabled:
            from app.agent.location_caps import CAP_DINING
            from app.agent.map_data import capability_location_at, capability_param
            dining_id = capability_location_at(
                resident.tile_x, resident.tile_y, CAP_DINING)
            if dining_id:
                loc_id = dining_id
                key = capability_param(dining_id, CAP_DINING, "host_duty")
        elif location_category(loc_id) == "dining":
            key = "cafe_host" if loc_id == "cafe" else "tavern_hub"
        if key:
            host = await find_duty_resident(db, key)
```

注：location_category 与 get_location_id_at 的 from-import 在 execute/basic.py:49 已存在，闸关分支引用它们不需新增 import；闸开分支的两个新名字用惰性 import（与本文件既有风格一致）。

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_meal_host_capability.py tests/test_meal_revenue.py tests/test_capability_action_gates.py -q
```

**验收**：1. 实现前 test_third_dining_location_does_not_misroute_meal_revenue_to_tavern_hub 红（实测得到 ['tavern_hub']）。2. 实现后全绿；tests/test_meal_revenue.py（真 sqlite + 真 coin_service 的转账/赊账/钱包缓存全套语义）零改动全绿。3. cafe→cafe_host、tavern→tavern_hub 在闸开闸关四种组合下均成立。

**commit**：

```
fix(economy): 餐费分账改读 dining 能力的 host_duty——堵第三个餐馆错付 tavern_hub
```

> #### 🔧 本 step 已被 critic 修订（2 处）
>
> 执行时以下列补丁为准，逐条覆盖上文对应字段。
>
> **修订 1 — 🟠 major · 字段 `implementation`**
>
> 处置：major-1（recheck_round2 P1-S7 fix(b)：「把无 host 的兜底从 treasury_debit 改成守恒去向——记入镇库（coin_service 已有 town_treasury 通路）…不得留销毁」）。签名已回源码核实：treasury_service.tax_pending(db, amount, reason="", *, ref_key=None)、coin_service.treasury_debit_pending(db, slug, amount) -> bool。本片段自锚点行起替换 implementation 剩余全文。
>
> 定位锚点：
>
> ```
>         elif location_category(loc_id) == "dining":
> ```
>
> 替换为：
>
>         elif location_category(loc_id) == "dining":
>             key = "cafe_host" if loc_id == "cafe" else "tavern_hub"
>         if key:
>             host = await find_duty_resident(db, key)
> ```
>
> 注：`dining_id` 必须在 if 之前初始化为 None（闸关时也要在作用域内），改动 2 要读它。
> location_category / get_location_id_at 的 from-import 在 execute/basic.py:49 已存在。
>
> 改动 2 — 堵掉闸开新增的**净销毁口**（同 commit 必做）。锚点 execute/basic.py:59-64。
> 实测：coin_service.treasury_debit（coin_service.py:585-597）是
> `UPDATE balance_sc = balance_sc - amount` 的纯销毁、无对手方；treasury_transfer 才守恒。
> 闸开后 host_duty 缺失 → key=None → host=None → to_host=False → 每餐
> npc_meal_cost_sc 直接蒸发。旧行为虽错付 tavern_hub 但守恒，蒸发是净退步（生产
> TOWN_TREASURY + TOWN_DUTY_FUNDING 全开、工资已改为镇库支出）。
>
> before：
> ```python
>         to_host = settings.npc_trade_enabled and host is not None and host.slug != slug
>         if to_host:
>             paid = await coin_service.treasury_transfer(db, slug, host.slug, cost,
>                                                         reason="meal")
>         else:
>             paid = await coin_service.treasury_debit(db, slug, cost, reason="meal")
> ```
> after：
> ```python
>         to_host = settings.npc_trade_enabled and host is not None and host.slug != slug
>         # P1: 声明了 dining 却没写 host_duty 时,餐费记入镇库而不是销毁。两个
>         # _pending 变体同事务落库,由下方既有的 `await db.commit()` 收口,不会出现
>         # 「扣了没进账」；paid=False 时 pending 分支一个字都没写,赊账路径不变。
>         to_town = (not to_host and settings.location_capabilities_enabled
>                    and settings.town_treasury_enabled
>                    and dining_id is not None and cost > 0)
>         if to_host:
>             paid = await coin_service.treasury_transfer(db, slug, host.slug, cost,
>                                                         reason="meal")
>         elif to_town:
>             from app.services import treasury_service
>             paid = await coin_service.treasury_debit_pending(db, slug, cost)
>             if paid:
>                 await treasury_service.tax_pending(db, cost,
>                                                    reason="meal:no_host_duty")
>         else:
>             paid = await coin_service.treasury_debit(db, slug, cost, reason="meal")
> ```
>
> **修订 2 — 🟠 major · 字段 `test_first`**
>
> 处置：major-1（recheck_round2 P1-S7 fix(c)：「补一条守恒断言测试（真 sqlite，照 tests/test_meal_revenue.py 的姿势）…当前 test_first 全程 monkeypatch 掉 coin_service，结构上不可能发现这个缺陷」）。本片段替换原 test_third_dining_location_does_not_misroute_meal_revenue_to_tavern_hub 全文（该用例把销毁当成了正确结果）。
>
> 定位锚点：
>
> ```
> async def test_third_dining_location_does_not_misroute
> ```
>
> 替换为：
>
> async def test_third_dining_location_pays_the_town_instead_of_the_void(
>         temp_location, duty_keys, monkeypatch):
>     """闸开 + 没写 host_duty:不查 duty、不错付,且餐费进镇库而**不是被销毁**。
>
>     treasury_debit 是纯销毁(无对手方),treasury_transfer 才守恒 —— 把「错付」修成
>     「蒸发」在守恒维度上是净退步,生产工资已改镇库支出,这是闭环货币里的单向漏斗。
>     """
>     from app.services import coin_service, treasury_service
>     taxed: list[tuple[int, str]] = []
>
>     async def _tax(db, amount, reason="", **kw):
>         taxed.append((amount, reason))
>
>     monkeypatch.setattr(coin_service, "treasury_debit_pending",
>                         AsyncMock(return_value=True))
>     monkeypatch.setattr(treasury_service, "tax_pending", _tax)
>     temp_location("t_canteen", {"category": "dining",
>                                "capabilities": {CAP_DINING: {}}})
>     monkeypatch.setattr(settings, "location_capabilities_enabled", True)
>     monkeypatch.setattr(settings, "town_treasury_enabled", True)
>     await execute_basic._charge_meal(AsyncMock(), _diner((4, 4)))
>     assert duty_keys == []                          # 不错付给 tavern_hub
>     assert coin_service.treasury_debit.await_count == 0   # 零净销毁
>     assert [a for a, _ in taxed] == [settings.npc_meal_cost_sc]
> ```
>
> 另在 **tests/test_meal_revenue.py 同批追加一条真 sqlite + 真 coin_service 的守恒断言**
> （本文件全程 monkeypatch 掉 coin_service，结构上不可能发现销毁缺陷）：在任意 dining
> 地点吃一餐前后，`sum(ResidentTreasury.balance_sc) + treasury_service.balance(db)`
> 逐分相等；分别覆盖 host 存在（transfer）与 host_duty 缺失（tax）两条路径。
>
> ```python
>

### P1-S8 — 新增 capability_locations / nearest_capability_location（能力→目的地反查） 🔧

**Flag / 批次**：location_capabilities_enabled=False（沿用；两个新函数本 step 内零生产调用方）。

**为什么**：P1 缺的另一半：从能力反查地点。今天全仓没有任何 action→location 反查（get_public_locations 是死码且丢 slug，不能直接用）。

nearest_capability_location 的实现体逐字复用 nearest_indoor_location（map_data.py:313-327）：entrance or center、曼哈顿距离、严格 <（并列取插入序先者）、不做可达性校验（与既有两个 nearest_* 同口径——不顺手修既有不对称行为）。

exclude_types 默认排除 private/apartment（避免把居民往别人家门口送——festival_draw_target 用 list(LOCATIONS.keys()) 全量候选就有这个毛病）；nearest_dining_location 的等价对拍必须传 exclude_types=()，因为旧实现不排除任何 type（与 nearest_indoor_location:317-318 不对称，这是既有事实）。

P1 交付边界：只交付这两个纯函数 + 单测。_maybe_capability_errand 决策分支属 P2（需要真实消费者才可行为验证，提前落地就是无法测行为的死码）；P1 只在 decide/basic.py:118 留注释座位。

#### 先写的测试（必须跑出失败）

文件：/Volumes/data/dev/simverse-world/backend/tests/test_capability_locations.py

```python
"""P1-S8: 能力 → 地点的反查(capability_locations / nearest_capability_location)。

nearest_capability_location 的实现体与 nearest_indoor_location(map_data.py:313-327)
同构:entrance or center、曼哈顿距离、严格 <(并列取插入序先者)、不做可达性校验。
对 dining 的等价对拍必须传 exclude_types=() —— 旧 nearest_dining_location 不排除
private/apartment(与 nearest_indoor_location 不对称,这是既有事实,不得顺手修)。
"""
from pathlib import Path

import pytest

from app.agent.location_caps import CAP_DINING, CAP_MARKET, CAP_RESEARCH
from app.agent.map_data import (
    LOCATIONS,
    capability_locations,
    nearest_capability_location,
    nearest_dining_location,
)
from app.config import settings


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setattr(settings, "location_capabilities_enabled", True)


@pytest.fixture
def temp_location():
    added: list[str] = []

    def _add(slug: str, extra: dict) -> str:
        assert slug not in LOCATIONS, slug
        data = {"name": "临时", "type": "public",
                "bounds": (2, 2, 6, 6), "center": (4, 4), "entrance": (4, 4)}
        data.update(extra)
        LOCATIONS[slug] = data
        added.append(slug)
        return slug

    yield _add
    for slug in added:
        LOCATIONS.pop(slug, None)


def test_dining_lookup_is_the_two_authored_diners_in_insertion_order():
    """LOCATIONS 字面量里 tavern(:19) 在 cafe(:29) 之前。"""
    assert capability_locations(CAP_DINING, exclude_types=()) == ["tavern", "cafe"]
    assert capability_locations(CAP_DINING) == ["tavern", "cafe"]


def test_research_and_market_lookups():
    assert capability_locations(CAP_RESEARCH) == ["experiment_building"]
    assert capability_locations(CAP_MARKET) == ["market_hall"]


def test_unknown_capability_yields_an_empty_list():
    assert capability_locations("nope") == []


def test_private_and_apartment_are_excluded_by_default(temp_location):
    """避免把居民往别人家门口送(festival_draw_target 用全量候选就有这毛病)。"""
    temp_location("t_home_kitchen", {"type": "private", "capacity": 1,
                                    "capabilities": {CAP_DINING: {}}})
    assert "t_home_kitchen" not in capability_locations(CAP_DINING)
    assert "t_home_kitchen" in capability_locations(CAP_DINING, exclude_types=())


@pytest.mark.parametrize("tile", [
    (75, 56), (46, 103), (16, 20), (172, 45), (116, 79), (0, 0), (179, 127),
])
def test_nearest_dining_matches_the_legacy_helper(tile):
    assert nearest_capability_location(
        tile, CAP_DINING, exclude_types=()) == nearest_dining_location(tile)


def test_nearest_prefers_entrance(temp_location):
    temp_location("t_far_center", {"bounds": (0, 0, 40, 2),
                                  "center": (20, 1), "entrance": (0, 0),
                                  "capabilities": {CAP_MARKET: {}}})
    # entrance (0,0) 比 market_hall 的 entrance (105,94) 更靠近原点
    assert nearest_capability_location((0, 1), CAP_MARKET) == "t_far_center"


def test_nearest_falls_back_to_center_without_entrance(temp_location):
    temp_location("t_no_entrance", {"bounds": (0, 0, 2, 2), "center": (1, 1),
                                   "capabilities": {CAP_RESEARCH: {}}})
    LOCATIONS["t_no_entrance"].pop("entrance", None)
    assert nearest_capability_location((1, 1), CAP_RESEARCH) == "t_no_entrance"


def test_nearest_returns_none_when_nothing_declares_it():
    assert nearest_capability_location((75, 56), "nope") is None


def test_ties_go_to_the_earlier_insertion(temp_location):
    temp_location("t_a", {"bounds": (50, 60, 52, 62), "center": (51, 61),
                         "entrance": (51, 61),
                         "capabilities": {CAP_MARKET: {}}})
    temp_location("t_b", {"bounds": (50, 60, 52, 62), "center": (51, 61),
                         "entrance": (51, 61),
                         "capabilities": {CAP_MARKET: {}}})
    assert nearest_capability_location((51, 61), CAP_MARKET) == "t_a"


def test_decide_has_a_reserved_seat_comment_for_p2():
    """P1 只留座位不落分支:_maybe_capability_errand 需要真实消费者才可行为验证,
    提前落地就是无法测行为的死码。"""
    src = (Path(__file__).resolve().parents[1]
           / "app" / "agent" / "phases" / "decide" / "basic.py")
    text = src.read_text(encoding="utf-8")
    assert "_maybe_capability_errand" in text
    assert "skip_decide_when_planned" in text
```

失败形态：ImportError: cannot import name 'capability_locations'。

#### 实现

改动 1：/Volumes/data/dev/simverse-world/backend/app/agent/map_data.py

锚点：紧接 S5 插入的 capability_location_at 之后、nearest_dining_location 之前。

```python
def capability_locations(
    cap: str, *, exclude_types: tuple[str, ...] = ("private", "apartment"),
) -> list[str]:
    """声明了 cap 的地点 id,按 LOCATIONS 插入序(静态在前、动态追加在尾)。

    默认排除 private/apartment:把居民往别人家门口送不是导流。需要全量候选时显式传
    exclude_types=() —— 这也是与旧 nearest_dining_location 对拍时的口径。
    """
    out: list[str] = []
    for loc_id, loc in LOCATIONS.items():
        if loc.get("type") in exclude_types:
            continue
        if cap in location_capabilities(loc_id):
            out.append(loc_id)
    return out


def nearest_capability_location(
    from_tile: tuple[int, int], cap: str, *,
    exclude_types: tuple[str, ...] = ("private", "apartment"),
) -> str | None:
    """曼哈顿距离最近的 cap 地点入口。

    实现体与 nearest_indoor_location(map_data.py:313-327)同构:entrance or center、
    abs(dx)+abs(dy)、严格 <(并列取插入序先者)。不做可达性校验 —— 与既有两个
    nearest_* 同口径(get_walkable_tiles 含孤岛,真可达要 get_reachable_tiles,
    那是 P3 的活)。
    """
    best, best_d = None, None
    for loc_id in capability_locations(cap, exclude_types=exclude_types):
        loc = LOCATIONS.get(loc_id) or {}
        entrance = loc.get("entrance") or loc.get("center")
        if not entrance:
            continue
        d = abs(from_tile[0] - entrance[0]) + abs(from_tile[1] - entrance[1])
        if best_d is None or d < best_d:
            best, best_d = loc_id, d
    return best
```

改动 2：/Volumes/data/dev/simverse-world/backend/app/agent/phases/decide/basic.py —— 只加注释座位，不加分支。

锚点：_maybe_crowd_draw 调用块结束、Case 2 之前（decide/basic.py:117-119）。

before：
```python
            return ctx

        # Case 2: 有计划且配置为跳过 decide —— 零 LLM 直接执行
```

after：
```python
            return ctx

        # ── P2 座位:_maybe_capability_errand ──────────────────────────
        # 「按地点能力挑目的地」的规则分支要插在这里 —— crowd 之后、Case 2 之前,
        # 做成 _maybe_crowd_draw 的同级 peer。
        #   · 不能更靠下:三份出厂 YAML 全设 skip_decide_when_planned: true,下面的
        #     Case 2 一旦有计划就无条件 return,插在它之后 = 死码。
        #   · 不能更靠上:越过 _maybe_needs_action 就是复现 0809 生产死锁(7/11 居民
        #     饿死在自家门口);tests/test_realism_needs.py 的
        #     test_critical_need_remains_ahead_of_market_pull 专门钉死这条排序。
        #   · 不能越过 crowd:caravan cohort 是 gameplay 权威,不是装饰性效果。
        # 命中后必须置 ctx.plan_followed = False 并把 plan.status 改成 "interrupted"
        # (照 :112-117),否则 tick.py:127-131 会把这次自由移动误判成 planned_move
        # 写进粘性行程。
        # P1 只交付反查函数(map_data.capability_locations /
        # nearest_capability_location);分支本体在 P2 —— 没有真实消费者时它是无法
        # 做行为验证的死码。

        # Case 2: 有计划且配置为跳过 decide —— 零 LLM 直接执行
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_capability_locations.py tests/test_map_data.py tests/test_realism_needs.py -q && .venv/bin/python -m pytest tests/ -q -k "decide or crowd"
```

**验收**：1. 实现前红（ImportError: capability_locations）。2. 实现后全绿；7 个采样 tile 上 nearest_capability_location(t, CAP_DINING, exclude_types=()) == nearest_dining_location(t) 逐个相等。3. nearest_dining_location 与 nearest_indoor_location 源码一个字未改（git diff 中这两个函数体零 diff）。4. decide/basic.py 的 diff 只含注释行：`git diff app/agent/phases/decide/basic.py | grep '^+' | grep -v '^+++' | grep -vc '^+ *#'` 输出 0。

**commit**：

```
feat(map): 能力→地点反查(capability_locations/nearest_capability_location)+decide 留座
```

> #### 🔧 本 step 已被 critic 修订（6 处）
>
> 执行时以下列补丁为准，逐条覆盖上文对应字段。
>
> **修订 1 — 🟠 major · 字段 `rationale`**
>
> 处置：major-3（recheck_round2 P1-S8 fix(a)(b)：「加一层 entrance in pathfinder.get_reachable_tiles()——必须用 get_reachable_tiles」「nearest_dining_location 改为委托 nearest_capability_location(..., CAP_DINING, exclude_types=())，这同时消掉『location_category 与 location_capabilities 优先级不对称』导致的『能吃 vs 去哪吃』分叉」）+ major-2。
>
> 定位锚点：
>
> ```
> 不做可达性校验（与既有两个 nearest_* 同口径——不顺手修既有不对称行为）
> ```
>
> 替换为：
>
> **闸开时加一层可达性过滤**（这是对既有缺陷的修复，不是复制）：候选 entrance 必须
> 在 `pathfinder.get_reachable_tiles()` 内。必须用 get_reachable_tiles 而不是
> get_walkable_tiles —— 后者被 `pathfinder._get_forced_walkable`（pathfinder.py:60-68）
> 无条件塞入每个地点的 entrance/center，会自证成功（生产 theater center(175,45) 实测
> walkable=True 而 reachable=False，且它是越界落库的存量行，P3-S6 的落库校验
> grandfather 放行）。不加这层：_maybe_needs_action 把返回值当硬目标 → find_path 返
> None → movement_failed_reason='unreachable' → status=idle → satiety 单调到 0 且每
> tick 吃一格日行动配额，更远但可达的 cafe 永远轮不到。闸关时逐字节旧口径。
>
> **同批让 nearest_dining_location 在闸开时委托给它**（exclude_types=()），使「能不能
> 吃」（actions.py + decide 的 capability_location_at）与「去哪吃」共用同一份能力口径
> 和同一份可达性口径。这同时消掉 critic 指出的优先级不对称：`location_category` 是
> 显式 category 键优先、`location_capabilities` 是显式与派生取并集，一条
> `{category:"lodging", capabilities:{dining}}` 的地点在旧口径下 EAT 可用却进不了
> nearest_dining 候选集 —— 分叉点全部收敛到 location_capabilities 后不再存在。S3 的
> 三级优先级本身无需改动。
>
> **修订 2 — 🟠 major · 字段 `implementation`**
>
> 处置：major-3。本片段替换 nearest_capability_location 的循环体（自 `best, best_d = None, None` 到 `return best`），并追加改动 1b。
>
> 定位锚点：
>
> ```
>     best, best_d = None, None
> ```
>
> 替换为：
>
>     best, best_d = None, None
>     from app.config import settings as _cap_settings
>     reachable = None
>     if _cap_settings.location_capabilities_enabled:
>         from app.agent import pathfinder
>         # 必须 get_reachable_tiles:get_walkable_tiles 被 _get_forced_walkable
>         # (pathfinder.py:60-68)无条件塞入每个地点的 entrance/center,会自证成功
>         # (实测生产 theater center walkable=True 而 reachable=False)。
>         # 惰性 import:pathfinder.py:9 在模块级 import map_data,反向必须惰性。
>         reachable = pathfinder.get_reachable_tiles()
>     for loc_id in capability_locations(cap, exclude_types=exclude_types):
>         loc = LOCATIONS.get(loc_id) or {}
>         entrance = loc.get("entrance") or loc.get("center")
>         if not entrance:
>             continue
>         if reachable is not None and tuple(entrance) not in reachable:
>             continue   # 不可达目标 = find_path 恒 None,satiety 危急者永久空转
>         d = abs(from_tile[0] - entrance[0]) + abs(from_tile[1] - entrance[1])
>         if best_d is None or d < best_d:
>             best, best_d = loc_id, d
>     return best
> ```
>
> 改动 1b — nearest_dining_location 委托（map_data.py:285-297，**闸关时函数体一字未改**）：
>
> before：
> ```python
> def nearest_dining_location(from_tile: tuple[int, int]) -> str | None:
>     """Nearest dining-category location entrance to ``from_tile``."""
>     best, best_d = None, None
> ```
> after：
> ```python
> def nearest_dining_location(from_tile: tuple[int, int]) -> str | None:
>     """Nearest dining-category location entrance to ``from_tile``.
>
>     闸开时委托能力反查:与 actions.py / decide 的 capability_location_at 同一份能力
>     口径 + 同一份可达性口径,否则「能不能吃」与「去哪吃」会分叉(location_category
>     是显式 category 键优先,location_capabilities 是取并集)。exclude_types=() 保留
>     旧语义 —— 旧实现不排除 private/apartment(与 nearest_indoor_location 不对称,
>     这是既有事实,不得顺手修)。
>     """
>     from app.config import settings as _cap_settings
>     if _cap_settings.location_capabilities_enabled:
>         from app.agent.location_caps import CAP_DINING
>         return nearest_capability_location(from_tile, CAP_DINING,
>                                            exclude_types=())
>     best, best_d = None, None      # ↓ 以下为原函数体，逐字保留
> ```
>
> **修订 3 — 🔴 blocker · 字段 `implementation`**
>
> 处置：blocker-3（critic_gates + recheck_round2 P1-S8：「把 before 锚点换成真实文本…after 块保留这三行英文注释原样」）。锚点已用 awk 逐行核对 decide/basic.py:117-121。本片段自锚点行起替换 改动 2 的「锚点/before/after」三段全文。
>
> 定位锚点：
>
> ```
> 锚点：_maybe_crowd_draw 调用块结束、Case 2 之前（decide/basic.py:117-119）
> ```
>
> 替换为：
>
> 锚点：_maybe_crowd_draw 调用块结束、Case 2 之前（decide/basic.py:117-119）。落地前先
> `grep -n "Case 2 (E-09" app/agent/phases/decide/basic.py` 核对行号。
>
> before（**已回源码逐字核实，原注释是英文三行，前一版计划写的中文注释是编造的**）：
> ```python
>             return ctx
>
>         # Case 2 (E-09/E-10): plan-priority skip. Follow the plan without an LLM
> ```
>
> after（英文三行原样保留，座位注释插在 `return ctx` 后的空行与它之间）：
> ```python
>             return ctx
>
>         # ── P2 座位:_maybe_capability_errand ──────────────────────────
>         # 「按地点能力挑目的地」的规则分支要插在这里 —— crowd 之后、Case 2 之前,
>         # 做成 _maybe_crowd_draw 的同级 peer。
>         #   · 不能更靠下:三份出厂 YAML 全设 skip_decide_when_planned: true,下面的
>         #     Case 2 一旦有计划就无条件 return,插在它之后 = 死码。
>         #   · 不能更靠上:越过 _maybe_needs_action 就是复现 0809 生产死锁;
>         #     tests/test_realism_needs.py 的
>         #     test_critical_need_remains_ahead_of_market_pull 钉死这条排序。
>         #   · 不能越过 crowd:caravan cohort 是 gameplay 权威,不是装饰性效果。
>         # 命中后必须置 ctx.plan_followed = False 并把 plan.status 改成
>         # "interrupted"(照 :112-117),否则 tick.py:127-131 会把这次自由移动误判成
>         # planned_move 写进粘性行程。
>         # P1 只交付反查函数(map_data.capability_locations /
>         # nearest_capability_location);分支本体在 P2(实名 _maybe_duty_venue /
>         # _maybe_stage_draw)—— 没有真实消费者时它是无法做行为验证的死码。
>
>         # Case 2 (E-09/E-10): plan-priority skip. Follow the plan without an LLM
> ```
>
> 注：after 块**不得**改写 `# Case 2 (E-09/E-10)` 起的三行英文注释；照前一版计划的
> 中文 before 执行 Edit 必然 old_string 不匹配失败，且会误删原英文注释。
>
> **修订 4 — 🟠 major · 字段 `test_first`**
>
> 处置：major-3（recheck_round2 P1-S8 fix(d)：「构造一个不可达的 dining 声明 + 一个更远但可达的 cafe，断言 nearest 返回后者」）+ major-2 的委托验证 + 闸关等价。t_island 落在 WALKABLE_X_RANGE=range(14,174) 之外，故 forced-walkable 但不连通（与生产 theater 同型）。
>
> 定位锚点：
>
> ```
> def test_nearest_returns_none_when_nothing_declares_it():
> ```
>
> 替换为：
>
> def test_unreachable_declaration_loses_to_a_farther_reachable_one(
>         temp_location, monkeypatch):
>     """孤岛目标会让 satiety 危急者永久空转(find_path 恒 None → status=idle,每 tick
>     重跑同一条不可达路线且吃掉一格日行动配额)。必须让位给更远但可达的 cafe。"""
>     from app.agent import pathfinder
>     temp_location("t_island", {"bounds": (2, 2, 6, 6), "center": (4, 4),
>                               "entrance": (4, 4),
>                               "capabilities": {CAP_DINING: {}}})
>     pathfinder.reset_walkable_cache()
>     try:
>         # 前提证据:forced-walkable 会自证成功,只有连通分量能戳穿它。
>         assert (4, 4) in pathfinder.get_walkable_tiles()
>         assert (4, 4) not in pathfinder.get_reachable_tiles()
>         got = nearest_capability_location((4, 5), CAP_DINING, exclude_types=())
>         assert got in ("cafe", "tavern") and got != "t_island"
>     finally:
>         LOCATIONS.pop("t_island", None)
>         pathfinder.reset_walkable_cache()
>
>
> def test_reachability_filter_is_off_when_the_flag_is_off(
>         temp_location, monkeypatch):
>     """闸关 = 逐字节旧口径(纯曼哈顿,不查可达性)。"""
>     monkeypatch.setattr(settings, "location_capabilities_enabled", False)
>     temp_location("t_island", {"bounds": (2, 2, 6, 6), "center": (4, 4),
>                               "entrance": (4, 4),
>                               "capabilities": {CAP_DINING: {}}})
>     assert nearest_capability_location(
>         (4, 5), CAP_DINING, exclude_types=()) == "t_island"
>
>
> def test_nearest_dining_delegates_and_kills_the_priority_asymmetry(
>         temp_location):
>     """闸开后「去哪吃」与「能不能吃」同口径:显式 category 键不再把一条声明了
>     dining 的地点踢出候选集。"""
>     temp_location("t_lodge", {"category": "lodging",
>                              "bounds": (60, 60, 62, 62), "center": (61, 61),
>                              "entrance": (61, 61),
>                              "capabilities": {CAP_DINING: {}}})
>     assert nearest_dining_location((61, 61)) == "t_lodge"
>
>
> def test_nearest_returns_none_when_nothing_declares_it():
>
> **修订 5 — 🔴 blocker · 字段 `verify_cmd`**
>
> 处置：blocker-4（recheck_round2 P1-S9：「把 tests/test_market_hall_constant.py 加进 P1-S8 的 verify_cmd」）。前提是把 S9 调到 S8 之前执行（见 dep_graph_fix），否则该文件在 S8 时尚不存在。同时补 test_capability_action_gates.py（S6 的 EAT 不变量会被 nearest_dining 委托影响）与 test_meal_revenue.py（S7 的守恒断言）。
>
> 定位锚点：
>
> ```
> tests/test_map_data.py tests/test_realism_needs.py -q
> ```
>
> 替换为：
>
> tests/test_map_data.py tests/test_realism_needs.py tests/test_market_hall_constant.py tests/test_capability_action_gates.py tests/test_meal_revenue.py -q
>
> **修订 6 — 🔴 blocker · 字段 `acceptance`**
>
> 处置：blocker-3 的连带（recheck_round2：「acceptance 第 4 条改成 … | grep -v '^+$' | wc -l 期望 0，显式放行新增空行」）+ major-3 的验收 + blocker-4 的交叉验证。本片段自第 3 条起替换 acceptance 剩余全文。
>
> 定位锚点：
>
> ```
> 3. nearest_dining_location 与 nearest_indoor_location 源码一个字未改
> ```
>
> 替换为：
>
> 3. nearest_indoor_location 源码一个字未改（git diff 中该函数体零 diff）；nearest_dining_location 只在函数头新增闸开委托分支，`best, best_d = None, None` 起的原函数体逐字保留。4. decide/basic.py 的 diff 只含注释行与空行：`git diff app/agent/phases/decide/basic.py | grep '^+' | grep -v '^+++' | grep -v '^+ *#' | grep -v '^+$' | wc -l` 输出 0（显式放行 after 块新增的那一行裸 `+` 空行——前一版写成 `grep -vc '^+ *#'` 期望 0 必然得 1，是会假红的自验证）。5. 可达性：新增的 t_island 用例证明 `get_walkable_tiles` 自证成功而 `get_reachable_tiles` 戳穿；`grep -n 'get_walkable_tiles' app/agent/map_data.py` 零命中（不得误用 walkable）。6. tests/test_market_hall_constant.py（S9 已落地）在本 step 后仍全绿——座位注释里的 `capability_locations` 字面量只出现在注释行。
>

### P1-S9 — market_hall 三处字面量收敛到 MARKET_HALL_LOCATION_ID（纯重构，不挂闸） 🔧

**Flag / 批次**：无（纯常量收敛，同一字符串，零行为差，不挂闸）。与 S1-S8 无文件交集，可全程并行。

**为什么**："market_hall" 在 agent 侧硬编码三处：decide/basic.py:390、decide/basic.py:391、tick.py:162。而 event_location.py:18 已有唯一常量 MARKET_HALL_LOCATION_ID。

只做常量收敛，明确不做能力派生：market_hall 不是「一个地点声明自己能做买卖」，而是「全镇有且只有一个集市场地」。场地权威是 settings.market_day_venue + event_location.resolve_event_location_id()，路网几何（caravan_route._MARKET_AVENUE_X_BOUNDS / map_data.py:204 的 caravan_parking）按这一栋楼的实际瓦片手调。改成 capability_locations("market") 反查，一旦出现第二个 market-capable 地点，crowd_service 的 cohort 判据、decide 的目的地、商队停车锚点会指向不同的楼，静默分裂。

caravan_route.py:39 的 _MARKET_HALL_ID 本身已是常量且属商队 gameplay 权威链，不动。

本 step 与 S1-S8 无任何文件交集，可全程并行。

#### 先写的测试（必须跑出失败）

文件：/Volumes/data/dev/simverse-world/backend/tests/test_market_hall_constant.py

```python
"""P1-S9: agent 侧 market_hall 字面量收敛到 event_location.MARKET_HALL_LOCATION_ID。

纯重构,同一字符串,零行为差,不挂闸。

明确不做能力派生:market_hall 不是「一个地点声明自己能做买卖」,而是「全镇有且只有
一个集市场地」。场地权威是 settings.market_day_venue + resolve_event_location_id,
路网几何(caravan_route._MARKET_AVENUE_X_BOUNDS / map_data 的 caravan_parking)按这一
栋楼的实际瓦片手调 —— 改成 capability_locations(market) 反查,一旦出现第二个
market-capable 地点,cohort 判据 / decide 目的地 / 商队停车锚点会指向不同的楼。
"""
import re
from pathlib import Path

from app.agent.map_data import LOCATIONS
from app.services.event_location import MARKET_HALL_LOCATION_ID

AGENT = Path(__file__).resolve().parents[1] / "app" / "agent"
SOURCES = [AGENT / "phases" / "decide" / "basic.py", AGENT / "tick.py"]


def test_the_constant_still_names_the_real_location():
    assert MARKET_HALL_LOCATION_ID == "market_hall"
    assert MARKET_HALL_LOCATION_ID in LOCATIONS
    assert LOCATIONS[MARKET_HALL_LOCATION_ID]["caravan_parking"] == (109, 94)


def test_no_bare_market_hall_literal_left_in_the_agent_hot_path():
    offenders = []
    for path in SOURCES:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"[\"']market_hall[\"']", line):
                offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert not offenders, offenders


def test_both_files_import_the_canonical_constant():
    for path in SOURCES:
        text = path.read_text(encoding="utf-8")
        assert "MARKET_HALL_LOCATION_ID" in text, path.name
        assert "from app.services.event_location import" in text, path.name


def test_market_capability_is_not_used_for_venue_resolution():
    """收敛到常量,而不是收敛到能力反查。"""
    for path in SOURCES:
        text = path.read_text(encoding="utf-8")
        assert "capability_locations" not in text, path.name
        assert "nearest_capability_location" not in text, path.name
```

失败形态：test_no_bare_market_hall_literal_left_in_the_agent_hot_path 红，列出 basic.py:390、basic.py:391、tick.py:162 三行。

#### 实现

改动 1：/Volumes/data/dev/simverse-world/backend/app/agent/phases/decide/basic.py

锚点：decide/basic.py:383-391。

before：
```python
        from app.agent.map_data import get_location_id_at, get_valid_target_tile
        from app.services import crowd_service
        here = get_location_id_at(ctx.resident.tile_x, ctx.resident.tile_y)
        world_events = getattr(ctx, "world_events", None)
        cohort = await crowd_service.market_day_crowd_cohort(
            ctx.db,
            world_events,
            persisted_only=not crowd_enabled,
        )
        if ctx.resident.id in cohort and here != "market_hall":
            target = "market_hall"
```

after：
```python
        from app.agent.map_data import get_location_id_at, get_valid_target_tile
        from app.services import crowd_service
        # 集市场地的唯一真相源。不用能力反查:全镇有且只有一个集市场地,路网几何按这
        # 一栋楼的瓦片手调,第二个 market-capable 地点会让 cohort 判据、目的地与商队
        # 停车锚点指向不同的楼。
        from app.services.event_location import MARKET_HALL_LOCATION_ID
        here = get_location_id_at(ctx.resident.tile_x, ctx.resident.tile_y)
        world_events = getattr(ctx, "world_events", None)
        cohort = await crowd_service.market_day_crowd_cohort(
            ctx.db,
            world_events,
            persisted_only=not crowd_enabled,
        )
        if ctx.resident.id in cohort and here != MARKET_HALL_LOCATION_ID:
            target = MARKET_HALL_LOCATION_ID
```

改动 2：/Volumes/data/dev/simverse-world/backend/app/agent/tick.py

锚点 a — 模块顶部 import（tick.py:7-12）。before：
```python
from app.agent.actions import ActionType, ActionResult
from app.agent.registry import registry
from app.agent.schemas import TickContext, get_world_time
from app.config import settings
from app.models.resident import Resident
from app.redis_client import get_redis
```
after：
```python
from app.agent.actions import ActionType, ActionResult
from app.agent.registry import registry
from app.agent.schemas import TickContext, get_world_time
from app.config import settings
from app.models.resident import Resident
from app.redis_client import get_redis
from app.services.event_location import MARKET_HALL_LOCATION_ID
```

锚点 b — 粘性行程写入（tick.py:155-162）。before：
```python
                        trip.update({
                            "kind": "market_day",
                            "event_id": ctx.market_trip_event_id,
                            "plan_date": world_date_key(),
                            "plan_slot": 0,
                            "plan_hour_range": [0, 24],
                            "location": "market_hall",
```
after：
```python
                        trip.update({
                            "kind": "market_day",
                            "event_id": ctx.market_trip_event_id,
                            "plan_date": world_date_key(),
                            "plan_slot": 0,
                            "plan_hour_range": [0, 24],
                            "location": MARKET_HALL_LOCATION_ID,
```

import 安全性核对：app.services.event_location 只 `from app.config import settings` + stdlib typing，不反向 import app.agent，模块级 import 不成环。

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_market_hall_constant.py -q && .venv/bin/python -m pytest tests/ -q -k "market or caravan or crowd or tick"
```

**验收**：1. 实现前 test_no_bare_market_hall_literal_left_in_the_agent_hot_path 红且精确列出 3 行。2. 实现后两条命令均全绿。3. `.venv/bin/python -c "from app.services.event_location import MARKET_HALL_LOCATION_ID as m; assert m == 'market_hall'"` 退出 0。4. `grep -rn '"market_hall"' app/agent/phases/decide/basic.py app/agent/tick.py` 零命中（注释除外）。

**commit**：

```
refactor(agent): market_hall 字面量收敛到 MARKET_HALL_LOCATION_ID——不做能力派生
```

> #### 🔧 本 step 已被 critic 修订（3 处）
>
> 执行时以下列补丁为准，逐条覆盖上文对应字段。
>
> **修订 1 — 🔴 blocker · 字段 `test_first`**
>
> 处置：blocker-4（recheck_round2 P1-S9 fix 方案①：「把 P1-S9 的断言改成剥注释后再判，复用同文件 test_no_bare_market_hall_literal_left_in_the_agent_hot_path 里已有的 `if line.lstrip().startswith("#"): continue` 逐行过滤，语义变成『非注释代码里不得用能力反查解析场地』」）。选①不选②：②要删掉座位注释里的函数名，会让 P2-S4/S11 失去可 grep 的坐标，且未来 P2 注释再提到这些名字又会复发。
>
> 定位锚点：
>
> ```
> def test_market_capability_is_not_used_for_venue_resolution
> ```
>
> 替换为：
>
> def test_market_capability_is_not_used_for_venue_resolution():
>     """收敛到常量,而不是收敛到能力反查 —— 判据只看**非注释代码**。
>
>     逐行剥注释是硬要求:P1-S8 要在 decide/basic.py 插一段 P2 座位注释,其中逐字包含
>     「map_data.capability_locations /」与「nearest_capability_location);分支本体在
>     P2」。读全文断言会与 S8 互斥 —— 无论谁先落地都会把对方打红,两种顺序都违反
>     「每 step 验证通过再进下一步」。过滤口径与本文件
>     test_no_bare_market_hall_literal_left_in_the_agent_hot_path 完全一致。
>     """
>     offenders = []
>     for path in SOURCES:
>         for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
>             if line.lstrip().startswith("#"):
>                 continue
>             if ("capability_locations" in line
>                     or "nearest_capability_location" in line):
>                 offenders.append(f"{path.name}:{i}: {line.strip()}")
>     assert not offenders, offenders
>
> **修订 2 — 🟡 minor · 字段 `rationale`**
>
> 处置：minor-2（brief：「必须删掉 rationale 里『与 S1-S8 无任何文件交集，可全程并行』这句错误表述——S8 要往 decide/basic.py 插注释，两者同文件必须串行」）+ blocker-4 的顺序前提。
>
> 定位锚点：
>
> ```
> 本 step 与 S1-S8 无任何文件交集，可全程并行。
> ```
>
> 替换为：
>
> **执行顺序（不可并行）**：本 step 与 S6（satiety 段改造）、S8（P2 座位注释）同改
> `app/agent/phases/decide/basic.py`，三者必须严格串行。顺序定为 **S6 → S7 → S9 → S8**：
> S9 放在 S8 之前，S8 的 verify_cmd 才能把 tests/test_market_hall_constant.py 纳入并当场
> 证明「座位注释不破坏 S9 的守卫」；反过来（S8 先）则 S9 落地时那条守卫已被注释打红，
> 或 S8 自报绿而把红留给全量跑。S9 对 decide/basic.py 的改动只在 :383-391 的 crowd 段，
> 与 S6 的 :314-317 satiety 段、S8 的 :117-119 座位无行冲突，但同文件顺序编辑仍需串行。
> S9 本身不依赖 S1-S8 的任何交付物（纯常量收敛），因此可提前到 S1 之前执行也成立。
>
> **修订 3 — 🟡 minor · 字段 `flag`**
>
> 处置：minor-2（同一条错误表述在 flag 字段的副本；只改 rationale 会留下一条自相矛盾的计划文本）。
>
> 定位锚点：
>
> ```
> 无（纯常量收敛，同一字符串，零行为差，不挂闸）。与 S1-S8 无文件交集，可全程并行。
> ```
>
> 替换为：
>
> 无（纯常量收敛，同一字符串，零行为差，不挂闸）。**与 S6/S8 同改 app/agent/phases/decide/basic.py，必须串行**：本 step 排在 S8 之前（见 rationale 的顺序说明），不得声明为可并行。
>

### P1-S10 — deploy env 补 LOCATION_CAPABILITIES_ENABLED + 新增 LOCATION_ 前缀 parity 断言（P1 收口）

**Flag / 批次**：location_capabilities_enabled=False（两份 env 模板均写 false）。非迁移批次、非开闸批次——本 step 只补文档与 parity 断言。真正的开闸（改 vm212 的 .env 为 true）属批 3，零代码 diff，与本批不同批。

**为什么**：deploy/backend/.env.example 是 vm212 部署实际参照的模板；07-27B 审计把「多份 env 真值互相漂移」定为事故级问题类。

既有的 parity 断言按前缀分组（GOVERNANCE_PREFIXES = CIVIC_/REP_/POLIS_OFFICE_、REALISM_EVENT_MEMORY_、REALISM_POOL_、REALISM_PLAN_），没有一条覆盖 LOCATION_ 前缀 —— 而「扫不到」的表现与「deploy 模板里根本没有这个键」一模一样：全绿，运维照 deploy 模板起的环境里这个旋钮不存在。本 step 按仓内既定套路补一条同形状的 parity 断言 + 一条默认值断言。

同时把「开闸硬顺序」写进 deploy 模板（P3 那批的开闸清单要靠它）。

本 step 是 P1 的收口：合入后在闸全关状态下跑全量默认门，失败集必须严格等于 54 基线（49 lab + 5 postpone），零新增。

#### 先写的测试（必须跑出失败）

改文件：/Volumes/data/dev/simverse-world/backend/tests/test_env_example_consistency.py —— 在文件末尾追加（不改动既有任何一行）：

```python

#: P1 地点能力声明的旋钮前缀。必须单开一条:LOCATION_ 既不在 GOVERNANCE_PREFIXES
#: (CIVIC_/REP_/POLIS_OFFICE_)里,也不在 REALISM_POOL_ / REALISM_PLAN_ /
#: REALISM_EVENT_MEMORY_ 任何一条现成 parity 的前缀内 —— 四条现成的 parity 全都
#: 扫不到它。而「扫不到」的表现与「deploy 模板里根本没有这个键」一模一样:全绿,
#: 运维照 deploy 模板起的环境里这个旋钮不存在(07-27B 审计 H2 把「多份 env 真值
#: 互相漂移」定为事故级问题类)。
#:
#: 前缀取到 LOCATION_ 而不是这一个键的全名:P2/P3 再加地点侧旋钮时自动被覆盖。
LOCATION_CAPABILITY_PREFIX = "LOCATION_"

#: (Settings 字段, env 键)。默认必须都是 false = 逐字节旧行为。
LOCATION_CAPABILITY_KNOBS = [
    ("location_capabilities_enabled", "LOCATION_CAPABILITIES_ENABLED"),
]


def test_location_capability_knobs_exist_in_deploy_env_example_too():
    """地点能力声明的旋钮必须同时出现在两份 env 参考里。"""
    backend_keys = {k for k in _raw_keys(ENV_EXAMPLE)
                    if k.startswith(LOCATION_CAPABILITY_PREFIX)}
    assert backend_keys, "backend/.env.example 里没有任何地点能力旋钮?基线认知错误"
    assert backend_keys >= {env for _, env in LOCATION_CAPABILITY_KNOBS}, (
        f"backend/.env.example 缺地点能力旋钮: "
        f"{sorted({env for _, env in LOCATION_CAPABILITY_KNOBS} - backend_keys)}")
    missing = sorted(backend_keys - _raw_keys(DEPLOY_ENV_EXAMPLE))
    assert not missing, (
        f"deploy/backend/.env.example 缺地点能力旋钮(补上并保持默认关): {missing}")


def test_location_capability_knobs_default_to_false_everywhere():
    """false = 逐字节旧行为,所以三处默认必须都是 false。

    任何一处模板写成 true,运维照它起的环境就是默认开闸 —— 而开闸会同时改写
    RESEARCH/EAT 的可用性判据与餐费分账的收款人。
    """
    for field, env_key in LOCATION_CAPABILITY_KNOBS:
        assert Settings.model_fields[field].default is False, \
            f"Settings 里 {field} 的默认不是 False —— 新行为必须默认关"
        for path in (ENV_EXAMPLE, DEPLOY_ENV_EXAMPLE):
            assert f"{env_key}=false" in path.read_text(encoding="utf-8"), \
                f"{path} 里 {env_key} 的默认不是 false"


def test_deploy_env_states_the_capability_gate_ordering():
    """开闸硬顺序必须写在运维照着操作的那份模板里(同 TOWN_DUTY_FUNDING 先例)。

    P1 的闸本身无前置,但它是 P2/P3 的前置:P2 的邮局/剧院接线全部经
    capability_location_at,闸不开时那两栋楼的能力门恒判 False,会假报「P2 接线
    失败」。这句话不写进模板,开闸顺序就只活在某个人的记忆里。
    """
    text = _deploy_env_text()
    assert "LOCATION_CAPABILITIES_ENABLED" in text
    assert "P2" in text and "前置" in text
```

失败形态：test_location_capability_knobs_exist_in_deploy_env_example_too 红（deploy/backend/.env.example 缺地点能力旋钮: ['LOCATION_CAPABILITIES_ENABLED']）。

#### 实现

改文件：/Volumes/data/dev/simverse-world/deploy/backend/.env.example

锚点：文件末尾（实测当前末行是 `REALISM_GOSSIP_EVENT_LANE_ENABLED=false`）。追加：

```

# ── P1 地点能力声明（LOCATION_CAPABILITIES_ENABLED）────────────────────────────
# 这个键是手工同步到本文件的（LOCATION_ 既不在 GOVERNANCE_PREFIXES 里，也不在
# REALISM_POOL_ / REALISM_PLAN_ / REALISM_EVENT_MEMORY_ 任何一条现成 parity 的前缀
# 内，四条 parity 全都扫不到它），由 backend/tests/test_env_example_consistency.py
# 的 LOCATION_ 前缀那条守着。
#
# 关（默认）= 逐字节旧行为，三处：
#   1 RESEARCH 的地点门比字面量 experiment_building（app/agent/actions.py:130）
#   2 EAT 的地点门走 _DINING_LOCATIONS={cafe,tavern} 白名单（map_data.py:269）
#   3 餐费分账按 cafe_host / tavern_hub 硬编码（execute/basic.py:56）
# 开 = 这三处改读地点自己的 capabilities 声明。
#
# 声明已随代码落地（cafe / tavern / experiment_building / market_hall 四条静态条目），
# 开闸不带任何数据变更——存量两栋公投楼（post_office / theater）在 P1 不需要任何
# 声明：它们今天既非 dining 也非 research，缺键即缺省安全，开闸后与今天逐位相同。
#
# 与 REALISM_ENABLED 正交：EAT 门本来就在 realism 内层，本闸是内层再套一层。
# REALISM_ENABLED=false 时开本闸不会凭空产生 EAT。
#
# 开闸硬顺序（写死，别凭记忆）：本闸自身无前置，但它是 P2 的前置——P2 的邮局 /
# 剧院接线全部经 map_data.capability_location_at（它绕开了 outdoor 街区对这两栋楼
# 的坐标遮蔽：get_location_id_at(46,103) 实测返 south_quarter）。本闸不开时那两栋楼
# 的能力门恒判 False，会假报「P2 接线失败」。顺序：
#   LOCATION_CAPABILITIES_ENABLED → P2 的 DUTY_VENUE / STAGE_EVENT 各闸。
#
# 开闸后的核验：抓一次 agent-worker 的 agent.events，确认咖啡馆 / 酒馆里的居民仍能
# 拿到 EAT、实验楼里的研究员仍能拿到 RESEARCH（等价性由
# backend/tests/test_capability_action_gates.py 的 34 条穷举对拍守着，开闸前先在本机
# 把它跑绿）。
LOCATION_CAPABILITIES_ENABLED=false
```

说明：backend/.env.example 的对应行已在 P1-S3 落地，本 step 不再改动它。

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_env_example_consistency.py tests/test_deploy_env_protection.py tests/test_deploy_exposure.py -q && .venv/bin/python -m pytest -q 2>&1 | tail -3
```

**验收**：1. 实现前 test_location_capability_knobs_exist_in_deploy_env_example_too 红（提示 deploy 缺 LOCATION_CAPABILITIES_ENABLED）。2. 实现后三个 env 相关文件全绿。3. P1 收口硬门：全量默认门（第二条命令）的失败集严格等于 54 基线（49 lab + 5 postpone），零新增失败——用 `git stash && .venv/bin/python -m pytest -q 2>&1 | tail -3` 取改前基线数字逐字对比。4. `grep -c 'LOCATION_CAPABILITIES_ENABLED=false' .env.example ../deploy/backend/.env.example` 两处各为 1。

**commit**：

```
docs(env): LOCATION_CAPABILITIES_ENABLED 同步 deploy 模板 + LOCATION_ 前缀 parity 断言
```

## P1 新增 step（critic 要求）

### P1-S0T — P1 基线冻结：新增 backend/tests/test_p1_baseline.py（S0 的可提交落地形态） 🆕

**Flag / 批次**：无（纯新增测试文件，零生产代码、零 Settings 字段、零 env 模板改动、零迁移、零开闸）。

**为什么**：P1-S0 只有依赖图与实测数字、commit_msg 写「无 commit」，违反「一 step 一 commit、单 step 能独立验证」——产出物只存在于执行者的短期记忆里，后续 step 的等价性对拍无从引用、失败时无法回溯基线。本 step 把 S0 的实测基线钉成一个可提交、可被后续 step 引用的断言文件；S0 本体降级为 plan 前言（不再编号为 step，不产生 commit）。

本 step 不是行为变更，是**基线冻结**：所有断言都描述改动前的既有事实，且必须在整个 P1 十步跑完后仍然全绿（因此刻意不写任何会被 S3/S4 推翻的断言——例如「静态条目不带 capabilities 键」在 S4 后必然翻红，不写；「不带 category 键」在 P1 全程为真，写）。任一条红 = 前序认知有误，必须停下修正 plan，而不是改这里的期望值。

实测出处：backend/app/world_geometry.py:9-10 `WALKABLE_X_RANGE = range(14, MAP_WIDTH_TILES - 6)` / `WALKABLE_Y_RANGE = range(12, MAP_HEIGHT_TILES - 4)`（MAP_WIDTH_TILES=180 / MAP_HEIGHT_TILES=128）；map_data.py:269 `_DINING_LOCATIONS = {"cafe", "tavern"}`；tests/test_lab_building.py:85-88 的 len==16 / actions[14]==RESEARCH。

#### 先写的测试

文件：/Volumes/data/dev/simverse-world/backend/tests/test_p1_baseline.py（新建即交付物）

```python
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
```

红态定义：文件不存在时 `pytest tests/test_p1_baseline.py` 得 collection error（file or directory not found）。绿态定义：8 条断言全部与实测一致。

#### 实现

无生产代码改动 —— 本 step 的交付物就是上面那个测试文件本身（新增 1 个文件，零既有文件改动）。

落地纪律：
- 先确认 `git status --short backend/tests/test_p1_baseline.py` 为未跟踪，再写入、跑绿、commit。
- 该文件在 P1 剩余每一个 step 的 verify_cmd 里都要顺带跑一遍（最省事的做法是各 step 命令行追加 `tests/test_p1_baseline.py`）；它翻红即代表某一步偷改了基线事实。
- 不得在后续 step 里修改本文件的任何期望值。若某条基线确实需要变（例如 P3 迁移改 theater bounds），那是另一个批次的事，必须显式在那个 step 里改并说明理由。

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_p1_baseline.py -q && .venv/bin/python -m pytest tests/test_map_data.py tests/test_agent_actions.py tests/test_lab_building.py tests/test_env_example_consistency.py -q
```

**验收**：1. 写入前同一命令得到 collection error（no tests ran / file not found）。2. 写入后第一条命令 8 个用例（含 4 条 parametrize 展开共 11 项）全 passed、failed=0。3. 第二条命令与 P1 开工基线一致（实测 41 passed + env 一致性用例全绿）。4. `git show --stat HEAD` 只含 `backend/tests/test_p1_baseline.py` 一个新文件，deletions=0。5. 执行者能复述串行顺序：S0T → S1 → S2 → S3 → S4 → S5 → S6 → S7 → S9 → S8 → S10，以及「S3 必须同批改 backend/.env.example」这条踩坑点。

**commit**：

```
test(map): P1 基线冻结——34 条静态地点/四条声明零 bounds 重叠/ActionType=16/walkable 域
```

---

# P2 修复邮局和剧院的功能

## P2 邮局侧（design_P2.md 批次表 #5 + #6）—— bite-sized TDD 执行计划

<details><summary>依赖边 / 批次归属 / 与既有守卫的冲突面</summary>

## 依赖边（含对 P1 的**新增**依赖，必须先落）

**新增依赖边 A（硬阻塞，P2 → P1-S1）**：`postal` / `stage` 两个能力必须先在 `backend/app/agent/location_caps.py` 的 `CAPABILITIES` 里登记且 `civic_grantable=True`。按 critic 裁决，该注册表是 capabilities schema 的唯一真值，规范形态 `dict[str, dict]`，闭集词表在 `CAPABILITIES`；`normalize_capabilities` 会把未登记的名字**静默丢弃（只 logger.debug）**——不登记就等于 P2 全段静默失效、零告警。P1-S1 需照抄以下两处改动（逐字）：

常量区（`CAP_MARKET = "market"` 之后）追加：
```python
CAP_POSTAL = "postal"
CAP_STAGE = "stage"
```
`CAPABILITIES` 字面量内（`CAP_MARKET` 条之后）追加：
```python
    CAP_POSTAL: CapabilitySpec(
        CAP_POSTAL, unlocks=(), category=None, civic_grantable=True),
    CAP_STAGE: CapabilitySpec(
        CAP_STAGE, unlocks=(), category=None, civic_grantable=True),
```
`unlocks=()` 是零新增 ActionType 的机器表述；`category=None` 防止污染 `location_category` → EAT/nearest_dining 通路。连带 P1-S1 的既有用例需改两处：`test_registry_is_a_closed_set_of_three` 的期望集合改成 5 个（并改函数名），`test_research_is_never_civic_grantable` 里 `CIVIC_GRANTABLE_CAPABILITIES == frozenset({CAP_DINING})` 改成 `frozenset({CAP_DINING, CAP_POSTAL, CAP_STAGE})`。P2-S1 的第一条测试就是这条依赖边的守卫，P1-S1 没改会当场红并直接报出原因。

**依赖边 B（代码，非开闸）**：P2 用到 P1 的四个交付物——`location_caps.normalize_capabilities/CAPABILITIES/CIVIC_GRANTABLE_CAPABILITIES`（S1）、`map_data.location_capabilities`（S2）、`map_data.capability_location_at`（S5）、`map_data.nearest_capability_location`（S8）。

**校正 design_P2.md 末尾与 P1-S10 env 文案的一处口径**：P2 邮局侧**不依赖 `LOCATION_CAPABILITIES_ENABLED` 开闸**。`location_capabilities` 与 `capability_location_at` 都是**不读闸**的纯查询（P1-S2/S5 的 rationale 明写「纯查询函数不挂闸；闸只加在调用点」），那道闸只管 `location_category` 的能力派生层与 `actions.py` 的 RESEARCH/EAT 两个门。这与 critic 第 66/67 条「开闸硬顺序的理由与代码事实不符」是同一处认知，P2-S5 把正确表述写进 deploy 模板。**真正的硬前置是数据侧**：生产 `dynamic_locations` 里 `post_office` 那行的 `data_json` 必须已回填 `capabilities={"postal":{}}`。

**校正 design_P2.md §①-A 的伪代码**：设计写的是 `here = get_location_id_at(tile_x, tile_y)` + `if "postal" in location_capabilities(here)`。这条**恒为 False**：`_find_location_in_bounds` 首命中即返，`post_office(44,100,48,106)` 完全落在 `south_quarter(42,100,135,109)` 内，实测 `get_location_id_at(46,103) == "south_quarter"`。P2 全段改用 P1-S5 的 `capability_location_at`（最小面积匹配，穿透遮蔽）。P2-S2 的测试把这条遮蔽事实与穿透结果同时钉死。

**顺带说明（不在本段范围，别顺手做）**：design §①-B「软」里的 `duty_service.prompt_hint` 加一句「到期的信在邮局中转」是改 `resident.meta_json["duty"]["prompt_hint"]` 的**数据**（`prompt_hint()` 只读该字段），属数据批次，不是代码改动；`_maybe_duty_venue` 的 `stage` 侧（剧院/讲师）属 #7-#9，本段不碰；`theater` 的 `capabilities` 声明同理留给 #7。

## 批次归属

| step | 类型 | 闸 | 数据/迁移 |
|---|---|---|---|
| P2-S1 | 行为（纯字面量，零运行时行为） | 无 | 无 |
| P2-S2 | 行为（纯查询 + 同串重构，零生产调用方） | 无 | 无 |
| P2-S3 | 行为 | **新增 `DUTY_VENUE_ENABLED=false`** | 无 |
| P2-S4 | 行为 | 沿用 S3 | 无 |
| P2-S5 | 文档 + parity 断言 | 沿用 S3（两份模板均 false） | 无 |

五个 step **无一条迁移、无一条开闸**（`DUTY_VENUE_ENABLED` 引入即默认关，两份 env 模板也写 false）。真正的开闸与 `post_office` 存量行的 `data_json` 回填分属另外两个批次，且二者之间也必须分车（07-25 事故红线：迁移/数据变更与开闸/行为变更不得同一次变更）。回填批次的正确顺序是：**先合并本段代码（闸关）→ 单独一批数据回填 → 再单独一次开闸**。回填本身零风险：不回填就开闸也不会出事，只是现场分支恒不命中、M2 的 `on_site` 恒为 0，与今天等价（这正是 §①-A「降级语义」的落点）。

## 串并行

严格串行：**S1 → S2 → S3 → S4 → S5**。
- S2 依赖 S1？不依赖代码，但依赖同一条 P1-S1 注册边，且 S2 的测试要用 `CAP_POSTAL`；顺序执行最省事。
- S3 依赖 S2（`duty_venue_location_at`）。
- S4 依赖 S2（`duty_venue_capability` / `duty_work_done` / `nearest_duty_venue`）与 S3（`duty_venue_enabled` 字段）。
- S5 依赖 S3（backend 模板里已有该键）。
S1 与 S2 无文件交集（`civic_service.py` vs `duty_service.py`），理论上可并行，但 S2 的 verify 不含 S1 的用例，并行收益 ≈ 0，不推荐。

## 与既有守卫的冲突面（已逐条避开）

1. **P1-S9 的 `test_market_capability_is_not_used_for_venue_resolution`** 对 `decide/basic.py` 与 `tick.py` 断言 `"capability_locations" not in text` 且 `"nearest_capability_location" not in text`。所以 P2-S4 的分支体**一律经 `duty_service` 的三个包装函数**取地点，decide 侧一个字都不出现这两个名字（`capability_location_at` 也不出现）。P2-S4 的 acceptance 用 grep 把这条钉死。
2. **P1-S8 的 `test_decide_has_a_reserved_seat_comment_for_p2`** 断言 `"_maybe_capability_errand" in text`。P2-S4 用真分支取代那段注释座位，故**必须同 commit 改写这条用例**（改名 + 改断言目标为 `_maybe_duty_venue`），否则该 commit 带着已知红入库。这与 critic 第 115 条「注释座位 step 应直接并入 P2 分支 step」是同一处。座位里的占位名 `_maybe_capability_errand` 与本段实名 `_maybe_duty_venue` 的差异，也在 P2-S4 的 rationale 里点明。
3. **`tests/test_env_example_consistency.py::test_every_settings_field_is_documented_or_allowlisted`**：新 Settings 字段必须同 commit 补 `backend/.env.example`，且 verify_cmd 必须含该文件——S3 已照做（critic 报过的硬红，P3-S5..S11 就栽在这里）。
4. **`tests/test_office_duty_boundary.py` 的 AST 扫描**禁止业务代码手写 `meta_json['duty']['key']`。P2-S2 的所有新函数一律走 `duty_key()`，结构上落不进判据。
5. **`tests/test_lab_building.py:85-88`（`len==16` / `actions[14]==RESEARCH`）** 全程不得触碰；S1/S3/S4 各自带一条同款断言复述这个不变量。
6. **零新增经济出口**：邮局侧不涉及任何 SC 流动（`_work_postman` 的工资在 `on_work` 的 `_pay_wage`，本段一个字不改）；观众收益（记忆/心情/social/关系，明确不发币）属 #10，不在本段。

## 本段落地后仍未闭合的两件事（交接给后续批次，别当已完成）

1. **每 tick 吃一格日行动 cap**：`_maybe_duty_venue` 按要求置 `ctx.plan_followed=False`，于是 `tick.py:127-131` 的 `planned_move` 三选一全不满足（无 `continuation_trip`、`plan_followed` 为 False、未设 `market_trip_event_id`），行程**不落粘性 Redis**，每 tick 重算目的地且每 tick 计一次 `agent_max_daily_actions`（默认 20）。邮差从镇中心 (75,56) 走到邮局入口 (46,100) 曼哈顿 73 格 ÷ `realism_move_speed=8` ≈ 9 tick ≈ 9/20 配额。这与既有 festival 抽签分支是同一形状（它也是 `plan_followed=False` 且不落行程），故不算新缺陷，但**开闸后 burn-in 必须盯**：若邮差当天配额被走路烧光，`arrivals` 会上去而 `on_site` 上不去（M1 绿、M2 红）。若要治，正解是给本分支也开一条 `ctx.duty_trip_*` 粘性通道，属独立 step，不在本段。
2. **到场后不保证选 WORK**：`_maybe_duty_venue` 只负责把人送到，落地后分支返回 None、交还普通 decide。`WORK ∈ _ALWAYS_AVAILABLE` 但选不选由计划/LLM 定。设计里的软导流（`prompt_hint` 加一句）是数据改动，见上文。M2 的 `on_site/work_runs ≥ 0.5` 验收线依赖这一环，开闸后若不达标，先查这里而不是查代码。

</details>

### P2-S1 — CIVIC_AGENDA 邮局 effect.data 声明 postal 能力（规范 dict 形态）+ 能力白名单与 topic 冻结守卫

**Flag / 批次**：无（纯字面量：两张建楼票均已关闭，seed 幂等键 topic 未变 → 零运行时行为；改动只对「将来重投重建」生效）。非迁移批次、非开闸批次。

**为什么**：design_P2.md §①-B「软」级：邮局的能力声明走 `civic_service._add_dynamic_location:923` 的 `payload = {k: v for k, v in data.items() if k != "slug"}` 整包落库通路 → `map_data.load_dynamic_locations:386` 整包塞进 LOCATIONS，**零迁移、零模型改动**（exp_tests.json 的 contract:「CIVIC_AGENDA 里加什么键，LOCATIONS 里就多什么键」）。

按 critic 权威裁决改写设计里的 `capabilities:["postal"]`：规范形态是 `dict[str, dict]`，`list[str]` 只是外部输入形态、须经 `normalize_capabilities` 归一。这里直接写规范形态（归一化的不动点），省掉一次归一，也让 data_json 落库即规范。

**只改 `data`，`topic` 一个字符都不动**：`seed_civic_agenda` 的幂等键是 `Poll.question` 精确匹配（civic_service.py:208-210），改一个字就重开一张票；而同 slug 再建走的是 `_add_dynamic_location` 的**整包覆盖**分支（`existing.data_json = payload`，旧键全丢）。测试把两条 topic 逐字冻结。

本 step 零运行时行为：生产两张建楼票都已关闭，`seed_civic_agenda` 每晚跳过；改动只在「将来重投重建」时生效——这是**产能修复**（修「以后建的邮局自带声明」），不是存量修复。存量那行 `dynamic_locations.data_json` 的回填是纯数据变更，属独立批次（见 notes），本批不做。这与 P1「声明随代码先落地、与数据/开闸分批」是同一条纪律。

白名单守卫存在的理由（critic 第 42/87 条）：`CIVIC_AGENDA` 是「公投能造出什么」的源头，而 `routers/polls.py:94-96` 允许 admin 附带任意 effect dict、`_add_dynamic_location` 只校验 slug 非空 + `"bounds" in data` 就整包落库。声明的能力必须全部落在 `CIVIC_GRANTABLE_CAPABILITIES` 内（`research` 恒不在其中，否则一张票就能绕过实验楼的地点门）。

#### 先写的测试（必须跑出失败）

新建文件：/Volumes/data/dev/simverse-world/backend/tests/test_civic_agenda_capabilities.py

```python
"""P2-S1: 邮局 effect.data 的 postal 能力声明 —— 规范 dict 形态 + 两条守卫。

两条守卫各防一类事故:
  · 能力白名单:CIVIC_AGENDA 是「公投能造出什么」的源头(routers/polls.py:94-96 允许
    admin 附带任意 effect dict,_add_dynamic_location 只校验 slug 非空 + bounds 在就
    整包落库)。声明的能力必须全部落在 CIVIC_GRANTABLE_CAPABILITIES 内 ——
    research 恒不在其中,否则一张票就能绕过实验楼的地点门(actions.py:130)。
  · topic 冻结:seed_civic_agenda 的幂等键是 Poll.question 精确匹配
    (civic_service.py:208-210)。topic 改一个字符就重开一张票,而同 slug 再建走的是
    _add_dynamic_location 的整包覆盖分支(existing.data_json = payload),旧键全丢。

第一条是 P2 → P1-S1 的依赖边守卫:postal/stage 没登记 → normalize_capabilities 会把
它们静默丢弃(只 logger.debug),全链零告警。所以这里用字符串字面量而不是 import
常量,好让失败信息直接说清该改哪。
"""
import pytest
from sqlalchemy import select

from app.agent.actions import ActionType
from app.agent.location_caps import (
    CAPABILITIES,
    CIVIC_GRANTABLE_CAPABILITIES,
    normalize_capabilities,
)
from app.models.season import Poll
from app.services import civic_service
from app.services.civic_service import CIVIC_AGENDA

#: 生产两张建楼票的 topic 逐字快照。**任何 data 改动都不得让它变化。**
FROZEN_TOPICS = ["在南苑空地兴建一座邮局", "在东岸花园兴建一座剧院"]


def _agenda_data(slug: str) -> dict:
    for item in CIVIC_AGENDA:
        for opt in item["options"]:
            data = ((opt.get("effect") or {}).get("data") or {})
            if data.get("slug") == slug:
                return data
    raise AssertionError(f"CIVIC_AGENDA 里没有 slug={slug} 的建楼选项")


def test_postal_and_stage_are_registered_by_p1_s1():
    """P2 → P1-S1 的依赖边:两个能力必须先在闭集注册表里登记且可被公投授予。"""
    missing = [c for c in ("postal", "stage") if c not in CAPABILITIES]
    assert not missing, (
        f"app/agent/location_caps.py 的 CAPABILITIES 缺 {missing} —— "
        "P1-S1 必须先登记 postal/stage(civic_grantable=True,unlocks=(),category=None),"
        "见 P2 计划 notes 的「新增依赖边 A」")
    for cap in ("postal", "stage"):
        spec = CAPABILITIES[cap]
        assert spec.civic_grantable is True, cap
        assert spec.unlocks == (), f"{cap} 不得解锁任何动作 —— P2 零新增 ActionType"
        assert spec.category is None, f"{cap} 不得派生 category(会污染 EAT 通路)"
    assert {"postal", "stage"} <= CIVIC_GRANTABLE_CAPABILITIES


def test_post_office_declares_postal_in_the_canonical_dict_form():
    assert _agenda_data("post_office")["capabilities"] == {"postal": {}}


def test_the_declaration_is_a_fixed_point_of_normalization():
    """规范形态 = 归一化的不动点。写成 [\"postal\"] 也能用,但落库的就不是规范形态。"""
    declared = _agenda_data("post_office")["capabilities"]
    assert normalize_capabilities(declared) == declared
    assert normalize_capabilities(["postal"]) == declared  # 宽松入口仍等价


def test_every_capability_in_the_agenda_is_civic_grantable():
    for item in CIVIC_AGENDA:
        for opt in item["options"]:
            data = ((opt.get("effect") or {}).get("data") or {})
            declared = normalize_capabilities(data.get("capabilities"))
            assert set(declared) <= CIVIC_GRANTABLE_CAPABILITIES, data.get("slug")
            assert "research" not in declared, data.get("slug")


def test_only_the_data_changed_topics_stay_frozen():
    assert [item["topic"] for item in CIVIC_AGENDA] == FROZEN_TOPICS


def test_the_rest_of_the_post_office_payload_is_untouched():
    data = _agenda_data("post_office")
    assert data["bounds"] == [44, 100, 48, 106]
    assert data["center"] == [46, 103]
    assert data["entrance"] == [46, 100]
    assert data["type"] == "public" and data["role"] == "logistics"
    assert data["boosted_actions"] == ["WORK"]
    assert data["description"] == "小镇邮局:寄信、收件、时间胶囊的中转站"


@pytest.mark.anyio
async def test_seed_is_still_idempotent_on_the_frozen_topics(db_session):
    """topic 没动 → 已有票的世界不会因为 data 改动重开票(否则同 slug 整包覆盖)。"""
    for topic in FROZEN_TOPICS:
        db_session.add(Poll(question=topic, options_json=[], status="closed"))
    await db_session.commit()

    assert await civic_service.seed_civic_agenda(db_session) == 0
    rows = (await db_session.execute(select(Poll))).scalars().all()
    assert len(rows) == len(FROZEN_TOPICS)


def test_action_type_enum_is_untouched():
    """P2 全段零新增 ActionType(design_P2.md §「为什么不新增 ActionType」)。"""
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
```

先跑一次拿红。预期失败形态（按 P1-S1 是否已改分两种，两种都必须先出现）：
- P1-S1 未登记 → `test_postal_and_stage_are_registered_by_p1_s1` 失败，信息直指 `CAPABILITIES 缺 ['postal', 'stage']`；
- P1-S1 已登记 → `test_post_office_declares_postal_in_the_canonical_dict_form` 抛 `KeyError: 'capabilities'`。

#### 实现

改文件：/Volumes/data/dev/simverse-world/backend/app/services/civic_service.py

锚点：`CIVIC_AGENDA` 的第一条（邮局），civic_service.py:175-180。`grep -c '"slug": "post_office"' app/services/civic_service.py` 应为 1，确认锚点唯一。

before：
```python
            {"label": "赞成兴建", "effect": {"type": "dynamic_location", "data": {
                "slug": "post_office", "name": "邮局", "type": "public", "role": "logistics",
                "bounds": [44, 100, 48, 106], "center": [46, 103], "entrance": [46, 100],
                "description": "小镇邮局:寄信、收件、时间胶囊的中转站",
                "boosted_actions": ["WORK"],
            }}},
```

after：
```python
            {"label": "赞成兴建", "effect": {"type": "dynamic_location", "data": {
                "slug": "post_office", "name": "邮局", "type": "public", "role": "logistics",
                "bounds": [44, 100, 48, 106], "center": [46, 103], "entrance": [46, 100],
                "description": "小镇邮局:寄信、收件、时间胶囊的中转站",
                "boosted_actions": ["WORK"],
                # P2 #5:邮局是「投递现场」不是「准入条件」。规范形态是 dict[str, dict]
                # (location_caps.normalize_capabilities 的不动点);effect.data 除 slug
                # 外整包落进 dynamic_locations.data_json(:923),再整包进 LOCATIONS
                # (map_data.py:386)—— 零迁移、零模型改动。
                # 只改 data:topic 一个字符都不能动,seed_civic_agenda 的幂等键是
                # Poll.question 精确匹配(:208-210),改字就重开票,而同 slug 再建走的是
                # 整包覆盖分支(existing.data_json = payload),旧键全丢。
                # 存量那行的回填是纯数据变更,属独立批次(迁移与开闸不同车)。
                "capabilities": {"postal": {}},
            }}},
```

本 step 不改任何其它文件。剧院（`theater`）的 `stage` 声明属 design_P2.md 的 #7，本段不碰。

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_civic_agenda_capabilities.py tests/test_location_caps.py tests/test_world_governance.py tests/test_m3_civic.py tests/test_lab_building.py -q
```

**验收**：1. 实现前同一命令必须红，且失败项是上面两种形态之一（若是第一种，先按 notes 的「新增依赖边 A」改 P1-S1 并让 `tests/test_location_caps.py` 重新全绿，再回到本 step）。2. 实现后全部 passed、failed=0。3. `git diff --numstat app/services/civic_service.py` 的 deletions 列为 0（纯插入，未删除任何原有行）。4. `git diff app/services/civic_service.py | grep -c '在南苑空地兴建一座邮局\|在东岸花园兴建一座剧院'` 输出 0（两条 topic 逐字未动）。5. `len(list(ActionType)) == 16` 由 `test_action_type_enum_is_untouched` 与 `tests/test_lab_building.py` 双份钉死。

**commit**：

```
feat(civic): 邮局 effect.data 声明 postal 能力(规范 dict 形态)——topic 冻结,零迁移零行为
```

### P2-S2 — duty_service 加「营生 → 现场能力」映射与四个纯查询 + WORK 冷却键收敛

**Flag / 批次**：无（纯查询 + 同串重构，零生产调用方；闸只加在调用点，见 P2-S3/S4）。非迁移批次。

**为什么**：把 #5/#6 共用的那半句话（「这个人的营生有没有现场、现场在哪、今天上过工没有」）落成 duty_service 的公开面，理由有三：

1. **两侧不互相硬编码 slug**：「营生有没有现场」是营生的属性（`DUTY_VENUE_CAPABILITY`），「哪栋楼是那个现场」是地点的能力声明（P2-S1 落的 `capabilities`）。中间用能力名对接，任何一侧都不出现对方的字面量——这正是 P1 能力体系存在的意义。
2. **绕开 P1-S9 的守卫**：`tests/test_market_hall_constant.py::test_market_capability_is_not_used_for_venue_resolution` 对 `decide/basic.py` 与 `tick.py` 断言 `"capability_locations" not in text` 且 `"nearest_capability_location" not in text`。P2-S4 的 decide 分支只能经这里的包装函数取地点，decide 侧一个字不提那两个名字。
3. **口径唯一**：`_work_postman`（S3）判「在不在现场」与 decide（S4）判「在不在现场 / 去哪」必须同一套解析，否则会出现「decide 认为没到、work 认为到了」的分叉。

**必须用 `capability_location_at` 而不是 `get_location_id_at`**（这是对 design §①-A 伪代码的校正）：后者首命中即返（map_data.py:243-249），命中序 = dict 插入序 = 静态在前动态追加在尾（:386），而 `post_office(44,100,48,106)` 完全落在 outdoor 街区 `south_quarter(42,100,135,109)` 内部——生产实测 `get_location_id_at(46,103)` 返 `"south_quarter"`。照设计原样写，邮差站在邮局正中也判不出「在现场」，命中率恒 0。测试把「遮蔽是真的」与「穿透查得到」同时钉死。

**冷却键收敛**：design §①-B「硬」要求 decide 的判据用「与 `duty_service.py:184-186` 同一个 Redis 键」。同一字符串在两处手写迟早漂移，漂移的表现是「走到了现场但冷却还没过」的空跑（白花日行动 cap）。这里收敛成 `_duty_work_cooldown_key`，并用源码扫描守住「没有裸字面量残留」。纯重构、同一字符串、零行为差。

本 step 全部是纯查询 + 同串重构，**零生产调用方、不挂闸**（闸只加在调用点，与 P1-S2 同一条纪律）。`duty_work_done` 的失败方向是 **fail-closed**（Redis 抖动 → 视为已上工 → 不导流）：宁可少一次导流，也不能因为 Redis 挂了把全镇有现场的营生持有人整齐赶去同一栋楼。

#### 先写的测试（必须跑出失败）

新建文件：/Volumes/data/dev/simverse-world/backend/tests/test_duty_venue_lookup.py

```python
"""P2-S2: 营生 → 现场能力的映射与四个纯查询(零生产调用方,不挂闸)。

核心是一条对 design §①-A 伪代码的校正:现场解析必须走 capability_location_at,
不能走 get_location_id_at —— 后者首命中即返,而 post_office(44,100,48,106) 完全落在
outdoor 街区 south_quarter(42,100,135,109) 内部。test_masking_is_real_and_the_venue
_lookup_sees_through_it 同时钉死「遮蔽是真的」与「穿透查得到」两件事。
"""
import re
from pathlib import Path

import pytest

from app.agent.location_caps import (
    CAPABILITIES, CAP_POSTAL, CIVIC_GRANTABLE_CAPABILITIES,
)
from app.agent.map_data import LOCATIONS, get_location_id_at
from app.services import duty_service

DUTY_SERVICE_SRC = (Path(__file__).resolve().parents[1]
                    / "app" / "services" / "duty_service.py")

# 生产 dynamic_locations 里 post_office 那行的 data_json(2026-08 公投建,active=t),
# capabilities 由调用方按场景决定加不加 —— 存量行今天**没有**这个键。
POST_OFFICE = {
    "name": "邮局", "type": "public", "role": "logistics",
    "bounds": (44, 100, 48, 106), "center": (46, 103), "entrance": (46, 100),
    "description": "小镇邮局:寄信、收件、时间胶囊的中转站",
    "boosted_actions": ["WORK"],
}


@pytest.fixture
def overlay():
    """模拟 load_dynamic_locations 的合入:追加到 LOCATIONS 尾部,再还原。"""
    added: list[str] = []

    def _merge(slug: str, data: dict, capabilities=None) -> str:
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


def _postman(tile=(0, 0), *, duty_key="postman", resident_type="npc"):
    from types import SimpleNamespace
    meta = {"duty": {"key": duty_key}} if duty_key else {}
    return SimpleNamespace(
        id="post-1", slug="luo-xiaozhou", name="骆小舟",
        resident_type=resident_type, status="idle",
        tile_x=tile[0], tile_y=tile[1], meta_json=meta,
    )


# ── 映射表本身 ─────────────────────────────────────────────────────────

def test_the_mapping_is_exactly_one_entry_for_now():
    """讲师的 stage/academy 归 design_P2.md 的 #8,不在本段。"""
    assert duty_service.DUTY_VENUE_CAPABILITY == {"postman": CAP_POSTAL}


def test_mapped_capabilities_are_registered_and_civic_grantable():
    for cap in duty_service.DUTY_VENUE_CAPABILITY.values():
        assert cap in CAPABILITIES, cap
        assert cap in CIVIC_GRANTABLE_CAPABILITIES, cap


def test_mapped_duty_keys_all_have_a_work_handler():
    """没有 WORK 产出的营生谈不上「现场」。"""
    assert set(duty_service.DUTY_VENUE_CAPABILITY) <= set(duty_service._WORK_HANDLERS)


# ── duty_venue_capability ─────────────────────────────────────────────

def test_capability_is_read_for_the_postman_only():
    assert duty_service.duty_venue_capability(_postman()) == CAP_POSTAL
    assert duty_service.duty_venue_capability(_postman(duty_key="tavern_hub")) is None
    assert duty_service.duty_venue_capability(_postman(duty_key=None)) is None


def test_untrusted_provenance_cannot_self_declare_a_duty_venue():
    """UGC 居民往 meta_json 里塞 duty 无效(resident_privilege_policy.py:105-110)。"""
    assert duty_service.duty_venue_capability(
        _postman(resident_type="character")) is None


# ── duty_venue_location_at / nearest_duty_venue ───────────────────────

def test_masking_is_real_and_the_venue_lookup_sees_through_it(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    # 遮蔽是真的:首命中返回的是 outdoor 街区,不是邮局。
    assert get_location_id_at(46, 103) == "south_quarter"
    assert get_location_id_at(46, 100) == "south_quarter"
    # 能力反查穿透遮蔽。
    assert duty_service.duty_venue_location_at(_postman((46, 103))) == "post_office"
    assert duty_service.duty_venue_location_at(_postman((46, 100))) == "post_office"


def test_outside_the_venue_returns_none(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    assert duty_service.duty_venue_location_at(_postman((75, 56))) is None


def test_legacy_row_without_the_declaration_is_inert(overlay):
    """存量 dynamic_locations 行没有 capabilities 键 —— 未回填时必须降级到「不在现场」,
    绝不能抛,也绝不能瞎认。"""
    overlay("post_office", POST_OFFICE)
    assert duty_service.duty_venue_location_at(_postman((46, 103))) is None
    assert duty_service.nearest_duty_venue(_postman((75, 56))) is None


def test_no_duty_means_no_venue_anywhere(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    plain = _postman((46, 103), duty_key=None)
    assert duty_service.duty_venue_location_at(plain) is None
    assert duty_service.nearest_duty_venue(plain) is None


def test_nearest_duty_venue_finds_the_only_postal_place(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    assert duty_service.nearest_duty_venue(_postman((75, 56))) == "post_office"
    # 全镇没有 postal 地点时(未 overlay)返回 None —— 见下一条。


def test_nearest_duty_venue_is_none_when_nothing_declares_postal():
    assert duty_service.nearest_duty_venue(_postman((75, 56))) is None


# ── 冷却键 ────────────────────────────────────────────────────────────

def test_cooldown_key_is_the_same_string_as_before():
    assert duty_service._duty_work_cooldown_key("abc") == "sv:duty_work:abc"


def test_no_bare_cooldown_literal_survives_outside_the_helper():
    offenders = []
    for i, line in enumerate(
            DUTY_SERVICE_SRC.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if "sv:duty_work:" in line and "_duty_work_cooldown_key" not in line:
            offenders.append(f"duty_service.py:{i}: {line.strip()}")
    assert len(offenders) == 1 and "def _duty_work_cooldown_key" not in offenders[0], (
        offenders)


@pytest.mark.anyio
async def test_duty_work_done_reads_the_cooldown_key():
    from app.redis_client import get_redis
    r = _postman()
    assert await duty_service.duty_work_done(r) is False
    await get_redis().set(duty_service._duty_work_cooldown_key(r.id), "1")
    assert await duty_service.duty_work_done(r) is True


@pytest.mark.anyio
async def test_duty_work_done_fails_closed_when_redis_is_down(monkeypatch):
    """Redis 抖动 → 视为已上工 → 不导流。宁可少一次导流,也不能因为 Redis 挂了
    把全镇有现场的营生持有人整齐赶去同一栋楼。"""
    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(duty_service, "get_redis", _boom)
    assert await duty_service.duty_work_done(_postman()) is True


@pytest.mark.anyio
async def test_on_work_still_honors_the_same_cooldown_key(monkeypatch):
    """收敛后 on_work 与 duty_work_done 仍然读写同一个键(同串重构的机器证明)。"""
    from unittest.mock import AsyncMock
    from app.redis_client import get_redis

    r = _postman()
    handler = AsyncMock(return_value="done")
    monkeypatch.setitem(duty_service._WORK_HANDLERS, "postman", handler)
    monkeypatch.setattr(duty_service, "_pay_wage", AsyncMock())

    assert await duty_service.on_work(AsyncMock(), r) == "done"
    assert await get_redis().exists(duty_service._duty_work_cooldown_key(r.id))
    assert await duty_service.duty_work_done(r) is True
    assert await duty_service.on_work(AsyncMock(), r) is None  # 冷却生效
    assert handler.await_count == 1
```

先跑一次拿红。预期失败形态：`ImportError`/`AttributeError: module 'app.services.duty_service' has no attribute 'DUTY_VENUE_CAPABILITY'`（收集期即报 `AttributeError`，全文件红）。

#### 实现

改文件：/Volumes/data/dev/simverse-world/backend/app/services/duty_service.py（两处）

**改动 1 —— 插入新块**。锚点：`max_perk` 结束（duty_service.py:115-118）与 `find_duty_resident`（:121）之间。

before：
```python
def max_perk(residents, key: str, default: float = 1.0) -> float:
    """Highest perk value among a group (used for presence-based boosts)."""
    values = [perk(r, key, default) for r in residents] or [default]
    return max([default, *values])


async def find_duty_resident(db, key: str) -> Resident | None:
```

after：
```python
def max_perk(residents, key: str, default: float = 1.0) -> float:
    """Highest perk value among a group (used for presence-based boosts)."""
    values = [perk(r, key, default) for r in residents] or [default]
    return max([default, *values])


# ── P2 营生场所 (duty venue) ───────────────────────────────────────────
# 「营生有没有现场」是营生自己的属性(下面这张表),「哪栋楼是那个现场」是地点自己
# 的能力声明(dynamic_locations.data_json / LOCATIONS 的 capabilities)。两侧用能力名
# 对接,谁都不硬编码对方的 slug。
#
# 今天只登记邮差(投递现场 = postal)。讲师的 stage/academy 归 design_P2.md 的 #8,
# 不在本段;没有 WORK 产出的营生谈不上现场,所以键必须是 _WORK_HANDLERS 的子集。
DUTY_VENUE_CAPABILITY: dict[str, str] = {
    "postman": CAP_POSTAL,
}


def _duty_work_cooldown_key(resident_id: str) -> str:
    """WORK 冷却键的唯一真相源。

    decide 侧的「今天还没上工」判据必须与 on_work 写的是同一个键 —— 两处手写同一
    字符串迟早漂移,漂移的表现是「人走到了现场但冷却还没过」的空跑(白花一格日行动
    cap,tick.py:108-117)。
    """
    return f"sv:duty_work:{resident_id}"


def duty_venue_capability(resident) -> str | None:
    """该居民营生声明的「现场」能力名;无营生 / 无现场语义 → None。纯函数。

    走 duty_key() 而不是裸读 meta_json:UGC 居民自己往 meta_json 里塞 duty 不算数
    (trusted_duty 的 provenance 门,resident_privilege_policy.py:105-110)。
    """
    return DUTY_VENUE_CAPABILITY.get(duty_key(resident) or "")


def duty_venue_location_at(resident) -> str | None:
    """居民此刻脚下、且提供其营生现场能力的地点 id;不在现场则 None。

    用 map_data.capability_location_at 而**不是** get_location_id_at:后者首命中即返
    (map_data.py:243-249),命中序 = dict 插入序 = 静态在前、动态追加在尾(:386),而
    post_office(44,100,48,106) 完全落在 outdoor 街区 south_quarter(42,100,135,109)
    内部 —— 生产实测 get_location_id_at(46,103) 返 "south_quarter"。照 get_location_id_at
    写,邮差站在邮局正中也判不出「在现场」,命中率恒 0。

    存量 dynamic_locations 行没有 capabilities 键 → 归一成空 dict → 这里返 None →
    调用方走老行为。缺省安全,回填前后都不炸。
    """
    cap = duty_venue_capability(resident)
    if not cap:
        return None
    from app.agent.map_data import capability_location_at
    return capability_location_at(resident.tile_x, resident.tile_y, cap)


def nearest_duty_venue(resident) -> str | None:
    """离居民最近的、提供其营生现场能力的地点 id(无营生现场 / 全镇没有这样的地点
    → None)。decide 侧的导流目的地由它给出。

    返回值必须是 map_data.LOCATIONS 的合法 key —— 下游 memorize 的
    metadata['move']['target'] 与生产的到访统计口径都吃它
    (memorize/basic.py:62-63 经 resolve_location_id)。
    """
    cap = duty_venue_capability(resident)
    if not cap:
        return None
    from app.agent.map_data import nearest_capability_location
    return nearest_capability_location((resident.tile_x, resident.tile_y), cap)


async def duty_work_done(resident) -> bool:
    """本冷却窗内是否已经上过工(与 on_work 同一个 Redis 键)。

    fail-**closed**:Redis 抖动时返回 True(视为已上工)。宁可少一次导流,也不能因为
    Redis 挂了把全镇有现场的营生持有人整齐赶去同一栋楼 —— 这与本模块其余部分的
    fail-open 方向相反,是刻意的。
    """
    try:
        return bool(await get_redis().exists(_duty_work_cooldown_key(resident.id)))
    except Exception:
        logger.debug("duty work cooldown probe failed for %s",
                     getattr(resident, "slug", "?"), exc_info=True)
        return True


async def find_duty_resident(db, key: str) -> Resident | None:
```

**改动 2 —— 顶部 import**。锚点：duty_service.py:52-56 的 import 段。

before：
```python
from app.models.resident import Resident
from app.redis_client import get_redis
from app.services.resident_privilege_policy import (
    trusted_duty,
)
```
after：
```python
from app.agent.location_caps import CAP_POSTAL
from app.models.resident import Resident
from app.redis_client import get_redis
from app.services.resident_privilege_policy import (
    trusted_duty,
)
```
import 安全性核对：`app/agent/location_caps.py` 不 import 任何 app 模块（P1-S1 的 `test_module_imports_nothing_from_app` 守着），`app/agent/__init__.py` 为 0 字节 —— 模块级 import 不成环。

**改动 3 —— 冷却键收敛**。锚点：`on_work` 内 duty_service.py:182-185。

before：
```python
        r = get_redis()
        cd_key = f"sv:duty_work:{resident.id}"
        if await r.exists(cd_key):
            return None
```
after：
```python
        r = get_redis()
        cd_key = _duty_work_cooldown_key(resident.id)
        if await r.exists(cd_key):
            return None
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_duty_venue_lookup.py tests/test_duty_service.py tests/test_office_duty_boundary.py tests/test_capability_location_at.py tests/test_market_hall_constant.py tests/test_lab_building.py -q
```

**验收**：1. 实现前红（收集期 `AttributeError: module 'app.services.duty_service' has no attribute 'DUTY_VENUE_CAPABILITY'`）。2. 实现后全绿：`tests/test_duty_service.py` 与 `tests/test_office_duty_boundary.py`（AST 扫描：业务代码不得手写 `meta_json['duty']['key']` 原始链）零改动全绿。3. `test_masking_is_real_and_the_venue_lookup_sees_through_it` 同时给出两条实证：`get_location_id_at(46,103) == "south_quarter"`（遮蔽真存在）与 `duty_venue_location_at(...) == "post_office"`（能力反查穿透）。4. `grep -rn 'sv:duty_work:' app/ | grep -v '_duty_work_cooldown_key'` 只剩 helper 定义那一行。5. 本 step 零生产调用方：`grep -rn 'duty_venue_location_at\|nearest_duty_venue\|duty_work_done' app/ | grep -v 'app/services/duty_service.py'` 输出为空。

**commit**：

```
feat(duty): 营生→现场能力映射与四个纯查询,WORK 冷却键收敛到单一真相源
```

### P2-S3 — 引入 DUTY_VENUE_ENABLED（默认关）+ _work_postman 现场分支与 metadata['duty'] + 胶囊向后兼容硬清单

**Flag / 批次**：新增 `duty_venue_enabled: bool = False`（env `DUTY_VENUE_ENABLED`，`backend/.env.example` 同 commit 写 false）。关 = 逐字节旧行为。非迁移批次（纯代码 + 模板文档，零 DB 改动）；deploy 模板同步与 parity 断言在 P2-S5。

**为什么**：design_P2.md §①-A 的本体：邮差 WORK 时解析「投递现场」，命中则写现场叙事 + `metadata['duty']`；不命中则**逐字节保持今天的行为**（仍然投递、仍然写同一条记忆、仍然 `_feed`）。

**为什么 metadata 两个分支都写（而不是只在现场写）**：M2 的验收 SQL 是 `count(*) FILTER (WHERE metadata_json->'duty'->>'at' = 'post_office') / count(*)`，分母 `work_runs` 取的是所有带 `duty.key='postman'` 的记忆。只在现场写，比值恒为 1.0，指标失去意义。所以闸开时两个分支都写 `{"key","at","delivered"}`，`at` 为 `None` 表示不在现场。**闸关时一个字节都不多写**（`metadata_json=None`、feed payload 不多键、记忆文本走原字符串），这就是「逐字节旧行为」的准确边界。

**降级语义是回滚安全的全部根据**：不在现场 = 老行为、功能不减，只少了现场叙事与统计标记。任何时刻把闸翻回 false 都不会让胶囊积压。

**向后兼容硬清单（design §① + §「向后兼容硬清单」）逐条变成断言**，因为这几条恰恰是最容易被后人「顺手优化掉」的：
- `deliver_due_capsules` 的 WHERE（capsule_service.py:87）**不得**加任何 location 条件——它是全局幂等状态翻转，加地点门 + 去掉夜间兜底 = 胶囊永久卡 sealed；
- `nightly_cron.py:173-179` 的无条件调用**必须保留**——它是所有新逻辑的兜底；
- **封存/领取都不得改成「必须在邮局」**：封存入口是玩家 HTTP API（`routers/capsules.py:34-41`），服务端没有权威的「玩家当前 tile」读法给 REST 用（tile 只在 WS move 帧里流经 `location_tracker.on_move`，纯内存、单 worker、不落库），加地点门 = 直接把玩家功能锁死；
- 不给 `time_capsules` 加列（尤其 NOT NULL）；
- `serialize()` 的返回字段是前端契约，只能追加。

**新增 Settings 字段必须同 commit 改 `backend/.env.example`**，且 verify_cmd 必须含 `tests/test_env_example_consistency.py::test_every_settings_field_is_documented_or_allowlisted`（该断言要求 `set(Settings.model_fields) - (_example_keys() | UNDOCUMENTED_OK)` 为空，漏了当场红）——这是 critic 报过的硬红，P3-S5..S11 七个 step 就栽在这里。

#### 先写的测试（必须跑出失败）

新建文件：/Volumes/data/dev/simverse-world/backend/tests/test_duty_venue_postman.py

```python
"""P2-S3: _work_postman 的现场分支、metadata['duty'],以及胶囊的向后兼容硬清单。

三条硬约束在本文件里是可执行断言,不是注释:
  1 投递的合法性与地点无关 —— 三种闸/位置组合下,到期的 sealed 胶囊都必须被送达;
  2 封存/领取都不得改成「必须在邮局」 —— capsule_service 全文不得出现地点语义,
    两个公开函数的签名不得多出地点参数;
  3 闸关 = 逐字节旧行为 —— 记忆文本逐字相等,且不写任何 metadata['duty'],
    feed payload 不多键。
"""
import inspect
import re
from datetime import datetime, timedelta, UTC
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.agent.actions import ActionType
from app.agent.location_caps import CAP_POSTAL
from app.agent.map_data import LOCATIONS
from app.config import Settings, settings
from app.models.memory import Memory
from app.models.resident import Resident
from app.models.time_capsule import TimeCapsule
from app.models.user import User
from app.services import capsule_service, duty_service

BACKEND = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = BACKEND / ".env.example"
CAPSULE_SRC = BACKEND / "app" / "services" / "capsule_service.py"
NIGHTLY_SRC = BACKEND / "app" / "tasks" / "nightly_cron.py"

POST_OFFICE = {
    "name": "邮局", "type": "public", "role": "logistics",
    "bounds": (44, 100, 48, 106), "center": (46, 103), "entrance": (46, 100),
    "description": "小镇邮局:寄信、收件、时间胶囊的中转站",
    "boosted_actions": ["WORK"],
}

LEGACY_NOTE_DELIVERED = "今天送到了 1 封到期的信,看着收信的人拆开,值了。"
LEGACY_NOTE_IDLE = "今天把该走的路线跑了一遍,没有迟到的信。"


@pytest.fixture
def overlay():
    added: list[str] = []

    def _merge(slug: str, data: dict, capabilities=None) -> str:
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


async def _postman_row(db, tile=(0, 0)) -> Resident:
    r = Resident(id="post-1", slug="luo-xiaozhou", name="骆小舟", creator_id="system",
                 resident_type="npc", district="south_quarter", status="idle",
                 tile_x=tile[0], tile_y=tile[1],
                 meta_json={"duty": {"key": "postman"}})
    db.add(r)
    await db.commit()
    return r


async def _overdue_capsule(db) -> TimeCapsule:
    u = User(name="u", email="venue@t.com", soul_coin_balance=100)
    db.add(u)
    await db.commit()
    c = TimeCapsule(user_id=u.id, carrier_resident_slug="luo-xiaozhou",
                    deliver_on=datetime.now(UTC).date() - timedelta(days=2),
                    content="到期的信", status="sealed")
    db.add(c)
    await db.commit()
    return c


async def _run_postman(db, resident):
    with patch("app.services.notification_service.manager.is_online",
               AsyncMock(return_value=False)):
        return await duty_service._work_postman(db, resident)


async def _only_memory(db) -> Memory:
    rows = (await db.execute(select(Memory))).scalars().all()
    assert len(rows) == 1, rows
    return rows[0]


# ── 闸本身 ────────────────────────────────────────────────────────────

def test_flag_defaults_to_off():
    assert Settings.model_fields["duty_venue_enabled"].default is False


def test_flag_is_documented_as_false_in_backend_env_example():
    assert "DUTY_VENUE_ENABLED=false" in ENV_EXAMPLE.read_text(encoding="utf-8")


# ── 闸关 = 逐字节旧行为 ───────────────────────────────────────────────

@pytest.mark.anyio
async def test_gate_off_keeps_the_legacy_note_and_writes_no_duty_metadata(
        db_session, overlay, monkeypatch):
    monkeypatch.setattr(settings, "duty_venue_enabled", False)
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    r = await _postman_row(db_session, tile=(46, 103))   # 就站在邮局里
    await _overdue_capsule(db_session)

    line = await _run_postman(db_session, r)

    assert line == "骆小舟跑完了今天的投递(送达 1 封)"
    mem = await _only_memory(db_session)
    assert mem.content == LEGACY_NOTE_DELIVERED
    assert "duty" not in (mem.metadata_json or {})


@pytest.mark.anyio
async def test_gate_off_idle_note_is_byte_identical(db_session, monkeypatch):
    monkeypatch.setattr(settings, "duty_venue_enabled", False)
    r = await _postman_row(db_session)
    await _run_postman(db_session, r)
    assert (await _only_memory(db_session)).content == LEGACY_NOTE_IDLE


# ── 闸开:不在现场 = 老叙事 + 统计标记 ────────────────────────────────

@pytest.mark.anyio
async def test_gate_on_off_site_keeps_the_legacy_note_but_records_at_null(
        db_session, overlay, monkeypatch):
    monkeypatch.setattr(settings, "duty_venue_enabled", True)
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    r = await _postman_row(db_session, tile=(75, 56))    # 镇中心,不在邮局
    await _overdue_capsule(db_session)

    await _run_postman(db_session, r)

    mem = await _only_memory(db_session)
    assert mem.content == LEGACY_NOTE_DELIVERED          # 叙事不变
    assert mem.metadata_json["duty"] == {
        "key": "postman", "at": None, "delivered": 1}    # 但分母进了统计


# ── 闸开:在现场 ──────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_gate_on_on_site_records_the_venue_and_names_it(
        db_session, overlay, monkeypatch):
    monkeypatch.setattr(settings, "duty_venue_enabled", True)
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    r = await _postman_row(db_session, tile=(46, 103))
    await _overdue_capsule(db_session)

    await _run_postman(db_session, r)

    mem = await _only_memory(db_session)
    assert mem.metadata_json["duty"] == {
        "key": "postman", "at": "post_office", "delivered": 1}
    assert "邮局" in mem.content        # 地点显示名,不是硬编码 slug
    assert mem.content != LEGACY_NOTE_DELIVERED


@pytest.mark.anyio
async def test_gate_on_legacy_row_without_declaration_degrades_to_off_site(
        db_session, overlay, monkeypatch):
    """存量 dynamic_locations 行没有 capabilities 键 —— 未回填就开闸不出事,
    只是 at 恒为 None(与今天等价)。"""
    monkeypatch.setattr(settings, "duty_venue_enabled", True)
    overlay("post_office", POST_OFFICE)
    r = await _postman_row(db_session, tile=(46, 103))
    await _overdue_capsule(db_session)

    await _run_postman(db_session, r)

    mem = await _only_memory(db_session)
    assert mem.content == LEGACY_NOTE_DELIVERED
    assert mem.metadata_json["duty"]["at"] is None


@pytest.mark.anyio
async def test_realism_raw_importance_and_duty_metadata_coexist(
        db_session, overlay, monkeypatch):
    """add_memory 在 realism 开时会往 metadata 里塞 raw_importance
    (memory/service.py:120-123)——两个键必须共存,不能互相覆盖。"""
    monkeypatch.setattr(settings, "duty_venue_enabled", True)
    monkeypatch.setattr(settings, "realism_enabled", True)
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    r = await _postman_row(db_session, tile=(46, 103))

    await _run_postman(db_session, r)

    meta = (await _only_memory(db_session)).metadata_json
    assert meta["duty"]["at"] == "post_office"
    assert "raw_importance" in meta


# ── 硬门:存量胶囊在任何组合下都不得失效 ──────────────────────────────

@pytest.mark.anyio
@pytest.mark.parametrize("gate,tile", [
    (False, (46, 103)), (True, (46, 103)), (True, (75, 56)), (True, (0, 0)),
])
async def test_overdue_capsules_are_always_delivered(
        db_session, overlay, monkeypatch, gate, tile):
    """M2′ 护栏的单测形态:任何闸态 / 任何站位下,到期 sealed 胶囊必须清零。"""
    monkeypatch.setattr(settings, "duty_venue_enabled", gate)
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    r = await _postman_row(db_session, tile=tile)
    cap = await _overdue_capsule(db_session)

    await _run_postman(db_session, r)

    await db_session.refresh(cap)
    assert cap.status == "delivered" and cap.delivered_at is not None
    overdue_sealed = (await db_session.execute(
        select(TimeCapsule).where(TimeCapsule.status == "sealed")
    )).scalars().all()
    assert overdue_sealed == []


# ── 向后兼容硬清单 ───────────────────────────────────────────────────

def test_capsule_service_has_no_location_semantics_at_all():
    """封存/领取都不得改成「必须在邮局」——邮局是投递现场,不是准入条件。"""
    text = CAPSULE_SRC.read_text(encoding="utf-8")
    hits = re.findall(r"location|venue|capabilit|tile_[xy]|duty_venue", text, re.I)
    assert hits == [], hits


def test_capsule_public_signatures_are_frozen():
    assert list(inspect.signature(capsule_service.create_capsule).parameters) == [
        "db", "user_id", "carrier_slug", "deliver_on", "content"]
    assert list(inspect.signature(capsule_service.deliver_due_capsules).parameters) == [
        "db", "today"]


def test_deliver_where_clause_has_no_location_condition():
    src = inspect.getsource(capsule_service.deliver_due_capsules)
    assert "TimeCapsule.deliver_on <= today, TimeCapsule.status == \"sealed\"" in src
    assert "location" not in src and "venue" not in src


def test_nightly_cron_keeps_the_unconditional_fallback():
    text = NIGHTLY_SRC.read_text(encoding="utf-8")
    assert "n = await deliver_due_capsules(db)" in text
    assert "duty_venue" not in text and "capabilit" not in text


def test_time_capsule_columns_are_unchanged():
    assert set(TimeCapsule.__table__.columns.keys()) == {
        "id", "user_id", "carrier_resident_slug", "deliver_on", "content",
        "resident_note", "status", "created_at", "delivered_at"}


def test_serialize_contract_is_append_only_and_unchanged():
    c = SimpleNamespace(
        id="c1", carrier_resident_slug="luo-xiaozhou", deliver_on="2026-09-01",
        status="sealed", content="x", resident_note=None,
        delivered_at=None, created_at=None)
    assert set(capsule_service.serialize(c, include_content=True)) == {
        "id", "carrier_resident_slug", "deliver_on", "status", "content",
        "resident_note", "delivered_at", "created_at"}


def test_action_type_enum_is_untouched():
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
```

先跑一次拿红。预期失败形态：`test_flag_defaults_to_off` 抛 `KeyError: 'duty_venue_enabled'`；`test_gate_on_on_site_records_the_venue_and_names_it` 抛 `TypeError: 'NoneType' object is not subscriptable`（`metadata_json` 为 None）。

#### 实现

**改动 1** —— /Volumes/data/dev/simverse-world/backend/app/config.py

锚点：P1-S3 插入的 `location_capabilities_enabled: bool = False` 那一行与其后的空行 + `# P2 Task 1 —` 注释之间（`grep -c 'location_capabilities_enabled' app/config.py` 应为 1）。

before：
```python
    location_capabilities_enabled: bool = False

    # P2 Task 1 — relation write deltas (reused, zero new LLM calls) + decay.
```
after：
```python
    location_capabilities_enabled: bool = False

    # --- P2 营生场所 (DUTY_VENUE_*) ---
    # 营生的「现场」语义:邮差在提供 postal 能力的地点上工时走现场分支(写
    # metadata['duty'] 供 M2 口径统计 + 现场叙事),并让 decide 在还没上工时先把人
    # 导流过去。关 = 逐字节旧行为:投递照旧发生、记忆文本逐字相同、metadata 不写、
    # feed payload 不多键、decide 零新分支。
    # **投递的合法性与地点无关**:deliver_due_capsules 的 WHERE 与 nightly_cron 的
    # 无条件兜底都不得因本闸改变(存量胶囊不得失效)。邮局是「投递现场」不是「准入
    # 条件」,所以任何时刻把闸翻回去都不会让胶囊积压。
    # 与 LOCATION_CAPABILITIES_ENABLED 无依赖关系:location_capabilities 与
    # capability_location_at 都是不读闸的纯查询,那道闸只管 location_category 的能力
    # 派生层与 RESEARCH/EAT 两个门。
    duty_venue_enabled: bool = False

    # P2 Task 1 — relation write deltas (reused, zero new LLM calls) + decay.
```

**改动 2** —— /Volumes/data/dev/simverse-world/backend/app/services/duty_service.py，用下段整体替换 `_work_postman`（duty_service.py:459-472，即 `async def _work_postman` 到 `return f"{resident.name}跑完了今天的投递(送达 {delivered} 封)"`）：

```python
async def _work_postman(db, resident) -> str | None:
    """骆小舟:跑一趟投递——把到期的时间胶囊送到,并留一条投递记忆。

    P2 #5(DUTY_VENUE_ENABLED):闸开时多做两件事——解析「投递现场」(人是否站在提供
    postal 能力的地点里),并给这条记忆写 metadata['duty'] = {key, at, delivered}
    作为 M2 口径的数据源。**两个分支都写**:M2 的比值是 on_site / work_runs,只在
    现场写会让分母塌成分子,比值恒 1.0、指标失效;不在现场时 at 为 None。

    **投递本身与地点无关,这是硬约束**:deliver_due_capsules 的 WHERE 不带任何
    location 条件(capsule_service.py:87),nightly_cron.py:173-179 的无条件兜底也原样
    保留 —— 邮局是「投递现场」不是「准入条件」。不在现场 = 老行为、功能不减,只少了
    现场叙事与统计标记;任何时刻把闸翻回 false 都不会让胶囊积压。

    闸关 = 逐字节旧行为:不解析地点、不写 metadata、feed payload 不多键、记忆文本
    走原字符串。
    """
    delivered = 0
    try:
        from app.services.capsule_service import deliver_due_capsules
        delivered = await deliver_due_capsules(db)
    except Exception:
        logger.warning("postman capsule delivery failed", exc_info=True)

    from app.config import settings
    venue: str | None = None
    if settings.duty_venue_enabled:
        try:
            venue = duty_venue_location_at(resident)
        except Exception:
            # 地点解析永远不能拖垮投递:胶囊已经送出去了。
            logger.warning("postman venue resolve failed", exc_info=True)

    if venue:
        from app.agent.map_data import get_location_by_id
        where = (get_location_by_id(venue) or {}).get("name") or "投递点"
        note = (f"今天在{where}把 {delivered} 封到期的信分拣出来送走了,看着收信的人拆开,值了。"
                if delivered else f"今天在{where}把该走的路线跑了一遍,没有迟到的信。")
    else:
        note = (f"今天送到了 {delivered} 封到期的信,看着收信的人拆开,值了。"
                if delivered else "今天把该走的路线跑了一遍,没有迟到的信。")

    metadata = None
    if settings.duty_venue_enabled:
        metadata = {"duty": {"key": "postman", "at": venue, "delivered": delivered}}
    from app.memory.service import MemoryService
    await MemoryService(db).add_memory(
        resident.id, "event", note, 0.5, "observation", metadata_json=metadata)

    payload = {"duty": "postman", "delivered": delivered}
    if settings.duty_venue_enabled:
        payload["at"] = venue
    await _feed(resident.slug, "duty_output", payload)
    return f"{resident.name}跑完了今天的投递(送达 {delivered} 封)"
```

**改动 3** —— /Volumes/data/dev/simverse-world/backend/.env.example

锚点：P1-S3 插入的 `LOCATION_CAPABILITIES_ENABLED=false` 那一行之后（`grep -c '^LOCATION_CAPABILITIES_ENABLED=false' .env.example` 应为 1）。在其后追加：

```

# ── P2 营生场所（DUTY_VENUE_ENABLED）──────────────────────────────────────────
# 关（默认）= 逐字节旧行为：邮差 WORK 时照旧投递、照旧写同一条记忆文本、metadata
# 不写、feed payload 不多键，decide 不产生任何导流。
# 开 = 两件事：
#   1 _work_postman 解析「投递现场」（站在提供 postal 能力的地点里），并给记忆写
#     metadata['duty'] = {key, at, delivered}——这是验收 M2 的唯一数据源，两个分支
#     都写（at 为 null 表示不在现场），否则比值的分母塌成分子；
#   2 decide 新增 _maybe_duty_venue 分支：营生有现场声明、今天还没上工、人不在现场
#     时，把这一 tick 定成 VISIT_DISTRICT 去现场（零 LLM）。
#
# 不改变的事（硬约束，也是回滚安全的全部根据）：胶囊的封存与投递**都不要求在邮局**。
# deliver_due_capsules 的 WHERE 不带任何 location 条件，nightly_cron 的无条件兜底原样
# 保留——邮局是「投递现场」不是「准入条件」。所以任何时刻把本闸翻回 false 都不会让
# 胶囊积压。护栏 SQL（恒为 0，违反即回滚）：
#   select count(*) from time_capsules
#    where status='sealed' and deliver_on < current_date - 1;
#
# 开闸硬前置（写死，别凭记忆）：
#   1 代码侧——P1 的 location_caps / capability_location_at / nearest_capability_location
#     必须已合入，且 CAPABILITIES 里登记了 postal（civic_grantable=true）。
#     注意：本闸**不**依赖 LOCATION_CAPABILITIES_ENABLED——location_capabilities 与
#     capability_location_at 都是不读闸的纯查询，那道闸只管 location_category 的能力
#     派生层与 RESEARCH/EAT 两个门。
#   2 数据侧——生产 dynamic_locations 里 post_office 那行的 data_json 必须已带上
#     capabilities={"postal":{}}。存量行是公投建的，没有这个键；回填是纯数据变更，
#     **必须独立批次**（迁移/数据变更与开闸不同车）。没回填就开闸不会出事，只是现场
#     分支恒不命中、M2 的 on_site 恒为 0，与今天等价。
DUTY_VENUE_ENABLED=false
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_duty_venue_postman.py tests/test_env_example_consistency.py tests/test_capsules.py tests/test_duty_service.py tests/test_duty_venue_lookup.py tests/test_lab_building.py -q
```

**验收**：1. 实现前红：`test_flag_defaults_to_off` 抛 `KeyError: 'duty_venue_enabled'`，`test_gate_on_on_site_records_the_venue_and_names_it` 抛 `TypeError`。2. 实现后全绿，其中 `tests/test_capsules.py`（封存收费 / 投递幂等 / sealed 隐私）与 `tests/test_duty_service.py` 零改动全绿。3. `tests/test_env_example_consistency.py::test_every_settings_field_is_documented_or_allowlisted` 绿 —— 新字段已进 `backend/.env.example`。4. 硬门：`test_overdue_capsules_are_always_delivered` 的 4 个参数化组合全绿（M2′ 护栏的单测形态）。5. `.venv/bin/python -c "from app.config import Settings; assert Settings.model_fields['duty_venue_enabled'].default is False"` 退出 0。6. `git diff app/services/duty_service.py` 里 `deliver_due_capsules(db)` 那一行未被任何条件包裹（投递与地点解耦）。

**commit**：

```
feat(duty): 邮差 WORK 加投递现场分支与 metadata['duty'],挂 DUTY_VENUE_ENABLED 默认关
```

### P2-S4 — decide 新分支 _maybe_duty_venue（插 decide/basic.py:118，crowd 之后 / Case 2 之前）+ 取代 P1-S8 的注释座位

**Flag / 批次**：`duty_venue_enabled=False`（沿用 P2-S3，本 step 不改闸值也不改两份 env 模板）。闸关时 `_maybe_duty_venue` 第一行即 return None，decide 排序与今天逐字节等价。

**为什么**：design_P2.md §①-B「硬」的本体，也是 P1-S8 那个注释座位的兑现（P1-S8 用的占位名是 `_maybe_capability_errand`，本段按批次表实名为 `_maybe_duty_venue`；critic 第 115 条已裁定「注释座位 step 应直接并入 P2 的分支 step，不再单独存在」）。

**插入点必须是 decide/basic.py:118**（`_maybe_crowd_draw` 块结束、Case 2 之前），三条边界各有硬依据：
- **不能更靠下**：三份出厂 YAML 全设 `skip_decide_when_planned: true`（default.yaml:30 / introvert.yaml:32 / extravert.yaml:33），Case 2 一旦有计划就无条件 `return`，插在它之后 = 死码；
- **不能更靠上**：越过 `_maybe_needs_action` 就是复现 0809 生产死锁（7/11 居民饿死在自家门口），`tests/test_crowd.py::test_critical_need_remains_ahead_of_market_pull` 专门钉死这条排序；
- **不能越过 crowd**：caravan lifecycle 的 cohort 是 gameplay 权威，不是装饰性效果。

**做成 `_maybe_crowd_draw` 的 async peer**：签名 `async def _maybe_duty_venue(self, ctx) -> ActionResult | None`。exp_mapdata.json 的 pitfall 明写「照抄 `_maybe_shelter` 的 `def` 形状再在里面 await，会静默返回 coroutine 对象，`if result is not None` 恒真」——本分支要 await Redis，必须是 async。守卫集合逐条对齐 crowd：`available_actions` 检查、status 排除 `(sleeping, chatting, socializing)`、`continuation_trip is None`、current/scheduled plan 非 GO_HOME。

**命中后必须 `ctx.plan_followed = False` + `plan.status = "interrupted"`**（TickContext 的 `plan_followed` 默认 True，漏置会让 `tick.py:127-131` 把这次自由移动误判成 planned_move 写进粘性行程 —— `spontaneous.py:41-44` 的 F5 注释就是这个坑的历史记录）。

**动作必须是 `VISIT_DISTRICT`**：`memorize/basic.py:175` 只在 action ∈ {WANDER, VISIT_DISTRICT, GO_HOME} 时写 `metadata['move']`，而 M1 的验收口径正是 `metadata_json->'move'->>'target' = 'post_office'`。产出别的动作，M1 完全看不到。且 `target_slug` 必须是 `LOCATIONS` 的合法 key（经 `resolve_location_id` 可解析），否则 `move.target` 写成 null。

**decide 侧一个字都不出现 `capability_locations` / `nearest_capability_location`**：P1-S9 的 `test_market_capability_is_not_used_for_venue_resolution` 对本文件全文断言这两个名字不存在（读全文、不剥注释）。所以地点解析一律经 P2-S2 的 `duty_service` 包装函数。

**Redis 判据与 on_work 同键**：`duty_work_done` 读的就是 `on_work` 写的 `sv:duty_work:{id}`（P2-S2 已收敛成单一 helper），避免「走到了但冷却还没过」的空跑。

#### 先写的测试（必须跑出失败）

新建文件：/Volumes/data/dev/simverse-world/backend/tests/test_duty_venue_decide.py

```python
"""P2-S4: decide 的 _maybe_duty_venue 分支 —— 插在 crowd 之后、Case 2 之前。

三类断言:
  1 分支自身的守卫(闸/可用集/status/粘性行程/GO_HOME/已上工/已在现场);
  2 命中后的上下文副作用(plan_followed=False + plan.status=interrupted),
    漏置会让 tick.py:127-131 把这次自由移动误判成 planned_move 写进粘性行程;
  3 排序不变式:临界需求仍排在本分支之前(0809 死锁的守卫),caravan 的 market
    cohort 仍压过本分支(gameplay 权威),且源码顺序被文本断言钉死。
"""
import random
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.actions import ActionType
from app.agent.location_caps import CAP_POSTAL
from app.agent.map_data import LOCATIONS
from app.agent.plan_target import resolve_location_id
from app.agent.schemas import HourlyPlan, TickContext
from app.config import settings
from app.redis_client import get_redis
from app.services import crowd_service, duty_service

DECIDE_SRC = (Path(__file__).resolve().parents[1]
              / "app" / "agent" / "phases" / "decide" / "basic.py")

POST_OFFICE = {
    "name": "邮局", "type": "public", "role": "logistics",
    "bounds": (44, 100, 48, 106), "center": (46, 103), "entrance": (46, 100),
    "description": "小镇邮局:寄信、收件、时间胶囊的中转站",
    "boosted_actions": ["WORK"],
}
MARKET_DAY = {
    "id": "market-1", "type": "festival", "title": "集市日",
    "starts_at": "2026-08-13T00:00:00+00:00",
    "ends_at": "2026-08-14T00:00:00+00:00",
    "payload_json": {"market_day": True, "location_id": "market_hall"},
}


@pytest.fixture
def overlay():
    added: list[str] = []

    def _merge(slug: str, data: dict, capabilities=None) -> str:
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
def _quiet_world(monkeypatch):
    """默认关掉会抢在本分支之前的两条通路,单测只留一个变量。"""
    monkeypatch.setattr(settings, "duty_venue_enabled", True)
    monkeypatch.setattr(settings, "realism_crowd_enabled", False)
    monkeypatch.setattr(settings, "caravan_lifecycle_enabled", False)


def _postman(tile=(75, 56), *, duty_key="postman", status="idle", needs=None):
    meta = {}
    if duty_key:
        meta["duty"] = {"key": duty_key}
    if needs:
        meta["needs"] = needs
    return SimpleNamespace(
        id="post-1", slug="luo-xiaozhou", name="骆小舟", resident_type="npc",
        status=status, tile_x=tile[0], tile_y=tile[1], meta_json=meta,
        home_location_id=None, home_tile_x=5, home_tile_y=5,
    )


def _ctx(resident, world_events=None, plan=None):
    ctx = TickContext(db=AsyncMock(), resident=resident, world_time="10:00",
                      hour=10, schedule_phase="上午",
                      current_plan=plan, scheduled_plan=plan)
    ctx.world_events = world_events or []
    ctx.available_actions = [ActionType.VISIT_DISTRICT, ActionType.WORK,
                             ActionType.IDLE]
    return ctx


def _plugin(**params):
    from app.agent.phases.decide.basic import BasicDecidePlugin
    plug = BasicDecidePlugin(params=params or None)
    plug._load_memories = AsyncMock()
    return plug


# ── 命中 ──────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_pulls_the_postman_to_the_only_postal_venue(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    res = await _plugin()._maybe_duty_venue(_ctx(_postman()))
    assert res is not None
    assert res.action == ActionType.VISIT_DISTRICT
    assert res.target_slug == "post_office"
    assert res.target_tile == (46, 100)      # entrance,不是越界的 center


@pytest.mark.anyio
async def test_target_slug_is_resolvable_so_the_move_metric_can_see_it(overlay):
    """memorize 的 move.target 经 resolve_location_id 解析(memorize/basic.py:62-63);
    解析不出就写成 null,生产的到访统计完全看不到这次导流。"""
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    res = await _plugin()._maybe_duty_venue(_ctx(_postman()))
    assert resolve_location_id(res.target_slug, res.target_slug) == "post_office"


@pytest.mark.anyio
async def test_hit_marks_the_plan_interrupted_and_unfollowed(overlay):
    """漏置 plan_followed=False 会让 tick.py:127-131 把自由移动误判成 planned_move。"""
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    plan = HourlyPlan(2, (9, 12), "STUDY", None, "图书馆", 3, "看书")
    ctx = _ctx(_postman(), plan=plan)

    out = await _plugin(skip_decide_when_planned=True).execute(ctx)

    assert out.action_result.action == ActionType.VISIT_DISTRICT
    assert out.action_result.target_slug == "post_office"
    assert out.plan_followed is False
    assert plan.status == "interrupted"


# ── 守卫 ──────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_gated_off_is_inert(overlay, monkeypatch):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    monkeypatch.setattr(settings, "duty_venue_enabled", False)
    assert await _plugin()._maybe_duty_venue(_ctx(_postman())) is None


@pytest.mark.anyio
async def test_already_on_site_does_not_pull(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    assert await _plugin()._maybe_duty_venue(_ctx(_postman((46, 103)))) is None


@pytest.mark.anyio
async def test_already_worked_today_does_not_pull(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    r = _postman()
    await get_redis().set(duty_service._duty_work_cooldown_key(r.id), "1")
    assert await _plugin()._maybe_duty_venue(_ctx(r)) is None


@pytest.mark.anyio
async def test_no_duty_venue_declaration_does_not_pull(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    assert await _plugin()._maybe_duty_venue(
        _ctx(_postman(duty_key="tavern_hub"))) is None
    assert await _plugin()._maybe_duty_venue(
        _ctx(_postman(duty_key=None))) is None


@pytest.mark.anyio
async def test_legacy_row_without_declaration_does_not_pull(overlay):
    """存量未回填 → 全镇没有 postal 地点 → 不导流(而不是乱导)。"""
    overlay("post_office", POST_OFFICE)
    assert await _plugin()._maybe_duty_venue(_ctx(_postman())) is None


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["sleeping", "chatting", "socializing"])
async def test_protected_status_is_never_interrupted(overlay, status):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    assert await _plugin()._maybe_duty_venue(
        _ctx(_postman(status=status))) is None


@pytest.mark.anyio
async def test_active_trip_wins(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    ctx = _ctx(_postman())
    ctx.continuation_trip = {"action": "VISIT_DISTRICT"}
    assert await _plugin()._maybe_duty_venue(ctx) is None


@pytest.mark.anyio
async def test_going_home_is_not_a_work_commute(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    plan = HourlyPlan(0, (9, 12), "GO_HOME", None, "home", 3, "回家")
    assert await _plugin()._maybe_duty_venue(
        _ctx(_postman(), plan=plan)) is None


@pytest.mark.anyio
async def test_requires_visit_district_to_be_available(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    ctx = _ctx(_postman())
    ctx.available_actions = [ActionType.IDLE]
    assert await _plugin()._maybe_duty_venue(ctx) is None


# ── 排序不变式 ───────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_critical_need_still_outranks_the_duty_commute(overlay, monkeypatch):
    """0809「饿死在自家门口」的守卫:临界需求必须排在导流之前。"""
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    monkeypatch.setattr(settings, "realism_enabled", True)
    res = _postman(needs={"energy": 0.05, "satiety": 0.8, "social": 0.8})
    ctx = _ctx(res)

    out = await _plugin(skip_decide_when_planned=True).execute(ctx)

    assert out.action_result.action == ActionType.GO_HOME


@pytest.mark.anyio
async def test_market_cohort_still_outranks_the_duty_commute(overlay, monkeypatch):
    """caravan cohort 是 gameplay 权威,不得被营生导流盖掉。"""
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    monkeypatch.setattr(settings, "caravan_lifecycle_enabled", True)
    ctx = _ctx(_postman(), [MARKET_DAY])

    with patch.object(crowd_service, "market_day_crowd_cohort",
                      AsyncMock(return_value=frozenset({"post-1"}))):
        out = await _plugin(skip_decide_when_planned=True).execute(ctx)

    assert out.action_result.target_slug == "market_hall"


def test_source_order_is_crowd_then_duty_venue_then_case_two():
    text = DECIDE_SRC.read_text(encoding="utf-8")
    i_crowd = text.index("crowd = await self._maybe_crowd_draw(ctx)")
    i_duty = text.index("duty_venue = await self._maybe_duty_venue(ctx)")
    i_case2 = text.index("# Case 2 (E-09/E-10): plan-priority skip.")
    assert i_crowd < i_duty < i_case2


def test_the_p1_seat_comment_is_replaced_by_the_real_branch():
    text = DECIDE_SRC.read_text(encoding="utf-8")
    assert "_maybe_capability_errand" not in text
    assert "async def _maybe_duty_venue" in text


def test_decide_never_names_the_p1_reverse_lookup_helpers():
    """P1-S9 的 test_market_capability_is_not_used_for_venue_resolution 读本文件全文,
    地点解析必须全部经 duty_service 的包装函数。"""
    text = DECIDE_SRC.read_text(encoding="utf-8")
    assert "capability_locations" not in text
    assert "nearest_capability_location" not in text


def test_action_type_enum_is_untouched():
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
```

先跑一次拿红。预期失败形态：`AttributeError: 'BasicDecidePlugin' object has no attribute '_maybe_duty_venue'`（多条），以及 `test_source_order_...` 抛 `ValueError: substring not found`。

#### 实现

改动分三处，全部在同一 commit。

**改动 1 —— 删除 P1-S8 的注释座位**（/Volumes/data/dev/simverse-world/backend/app/agent/phases/decide/basic.py）。

定位：`grep -n '── P2 座位:_maybe_capability_errand' app/agent/phases/decide/basic.py`。删除从该行起、到包含 `# 做行为验证的死码。` 的那一行为止的**整个注释块**，以及它下面那一个空行。删完后 `grep -c '_maybe_capability_errand' app/agent/phases/decide/basic.py` 必须为 0。

**改动 2 —— 插入调用块**（同文件）。锚点：Case 2 注释首行（改动前实测在 decide/basic.py:119，删掉座位后行号上移）。`grep -c '# Case 2 (E-09/E-10): plan-priority skip.' app/agent/phases/decide/basic.py` 应为 1。

before：
```python
            return ctx

        # Case 2 (E-09/E-10): plan-priority skip. Follow the plan without an LLM
```
after：
```python
            return ctx

        # P2 #6 (DUTY_VENUE): 营生有「现场」声明的人,今天还没上工、又不在现场时,
        # 先把这一 tick 定成去现场(零 LLM)。位置是 crowd 之后、Case 2 之前:
        #   · 不能更靠下 —— 三份出厂 YAML 全设 skip_decide_when_planned: true,
        #     Case 2 一旦有计划就无条件 return,插在它之后就是死码;
        #   · 不能更靠上 —— 越过 _maybe_needs_action 就是复现 0809 生产死锁
        #     (7/11 居民饿死在自家门口);
        #   · 不能越过 crowd —— caravan cohort 是 gameplay 权威,不是装饰效果。
        duty_venue = await self._maybe_duty_venue(ctx)
        if duty_venue is not None:
            ctx.action_result = duty_venue
            ctx.plan_followed = False
            if plan:
                plan.status = "interrupted"
            return ctx

        # Case 2 (E-09/E-10): plan-priority skip. Follow the plan without an LLM
```

**改动 3 —— 插入方法本体**（同文件）。锚点：`_maybe_crowd_draw` 的 return 收尾（decide/basic.py:405-408）与 `_crowd_hint`（:410）之间。

before：
```python
        return ActionResult(
            action=ActionType.VISIT_DISTRICT, target_slug=target,
            target_tile=target_tile or get_valid_target_tile(target), reason="去凑热闹",
        )

    async def _crowd_hint(self, ctx: TickContext) -> str:
```
after：
```python
        return ActionResult(
            action=ActionType.VISIT_DISTRICT, target_slug=target,
            target_tile=target_tile or get_valid_target_tile(target), reason="去凑热闹",
        )

    async def _maybe_duty_venue(self, ctx: TickContext) -> ActionResult | None:
        """P2 #6: 营生有「现场」声明、今天还没上工、且人不在现场时,把这一 tick 的
        目的地定成那个现场(VISIT_DISTRICT,零 LLM)。

        动作必须是 VISIT_DISTRICT:memorize 只在 action ∈ {WANDER, VISIT_DISTRICT,
        GO_HOME} 时写 metadata['move'](memorize/basic.py:175),而到访验收的口径正是
        metadata_json->'move'->>'target' —— 产出别的动作,统计完全看不到。

        地点解析全部经 duty_service 的包装函数:一来「营生有没有现场」与「哪栋楼是
        那个现场」两侧不互相硬编码 slug,二来本文件被 P1-S9 的守卫读全文,不得出现
        capability_locations / nearest_capability_location 这两个名字。

        守卫集合与 _maybe_crowd_draw 逐条对齐(可用集 / status / 粘性行程 / GO_HOME);
        上面几条 early-return 已经挡掉了饿死、暴雨、在途粘性与商队 gameplay 权威,
        这里不重复写。
        """
        if not settings.duty_venue_enabled:
            return None
        if ActionType.VISIT_DISTRICT not in ctx.available_actions:
            return None
        # 不得把人从对话 / 睡眠 / 已开始的行程里拽出来。
        if ctx.resident.status in ("sleeping", "chatting", "socializing"):
            return None
        if ctx.continuation_trip is not None:
            return None
        # 回家不是上工。临界精力与 GO_HOME 行程在上面已受保护;这里再挡一次「行程还
        # 没落 Redis 的第一步」。
        if any(
            plan is not None and plan.action == ActionType.GO_HOME.value
            for plan in (ctx.current_plan, ctx.scheduled_plan)
        ):
            return None
        from app.services import duty_service
        if not duty_service.duty_venue_capability(ctx.resident):
            return None
        # 今天已经上过工就别再赶路 —— 与 on_work 用同一个 Redis 键
        # (duty_service._duty_work_cooldown_key),否则会出现「走到了现场但冷却还没过」
        # 的空跑,白花一格日行动 cap。Redis 抖动时该查询 fail-closed(视为已上工)。
        if await duty_service.duty_work_done(ctx.resident):
            return None
        if duty_service.duty_venue_location_at(ctx.resident):
            return None  # 已经在现场
        target = duty_service.nearest_duty_venue(ctx.resident)
        if not target:
            return None
        from app.agent.map_data import get_valid_target_tile
        target_tile = get_valid_target_tile(target)
        if not target_tile:
            return None
        return ActionResult(
            action=ActionType.VISIT_DISTRICT, target_slug=target,
            target_tile=target_tile, reason="去上工",
        )

    async def _crowd_hint(self, ctx: TickContext) -> str:
```

**改动 4 —— 兑现座位后同步 P1-S8 的用例**（/Volumes/data/dev/simverse-world/backend/tests/test_capability_locations.py）。P1-S8 的 `test_decide_has_a_reserved_seat_comment_for_p2` 断言 `"_maybe_capability_errand" in text`，座位被真分支取代后必然红，同 commit 改写：

before：
```python
def test_decide_has_a_reserved_seat_comment_for_p2():
    """P1 只留座位不落分支:_maybe_capability_errand 需要真实消费者才可行为验证,
    提前落地就是无法测行为的死码。"""
    src = (Path(__file__).resolve().parents[1]
           / "app" / "agent" / "phases" / "decide" / "basic.py")
    text = src.read_text(encoding="utf-8")
    assert "_maybe_capability_errand" in text
    assert "skip_decide_when_planned" in text
```
after：
```python
def test_the_p1_seat_is_now_filled_by_the_p2_duty_venue_branch():
    """P1 只留座位(占位名 _maybe_capability_errand),P2 #6 用实名 _maybe_duty_venue
    兑现它。座位与真分支不得并存 —— 并存说明分支插错了位置。"""
    src = (Path(__file__).resolve().parents[1]
           / "app" / "agent" / "phases" / "decide" / "basic.py")
    text = src.read_text(encoding="utf-8")
    assert "_maybe_capability_errand" not in text
    assert "async def _maybe_duty_venue" in text
    assert "skip_decide_when_planned" in text
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_duty_venue_decide.py tests/test_capability_locations.py tests/test_market_hall_constant.py tests/test_crowd.py tests/test_realism_needs.py tests/test_caravan_market_visitors.py tests/test_duty_venue_postman.py tests/test_lab_building.py -q
```

**验收**：1. 实现前红：多条 `AttributeError: 'BasicDecidePlugin' object has no attribute '_maybe_duty_venue'` + `test_source_order_is_crowd_then_duty_venue_then_case_two` 的 `ValueError: substring not found`。2. 实现后全绿，其中 `tests/test_crowd.py`（含 `test_critical_need_remains_ahead_of_market_pull`）、`tests/test_realism_needs.py`、`tests/test_caravan_market_visitors.py`、`tests/test_market_hall_constant.py` 零改动全绿。3. `grep -c 'capability_locations\|nearest_capability_location' app/agent/phases/decide/basic.py` 输出 0（P1-S9 守卫不被打红）。4. `grep -c '_maybe_capability_errand' app/agent/phases/decide/basic.py` 输出 0（座位已兑现）。5. 命中语义由 `test_hit_marks_the_plan_interrupted_and_unfollowed` 钉死：`ctx.plan_followed is False` 且 `plan.status == "interrupted"`。6. `len(list(ActionType)) == 16`。

**commit**：

```
feat(agent): decide 新增 _maybe_duty_venue 导流分支(crowd 后 / 计划跳过前),同闸默认关
```

### P2-S5 — DUTY_VENUE_ENABLED 同步 deploy 模板 + DUTY_VENUE_ 前缀 parity 断言（邮局侧收口）

**Flag / 批次**：`duty_venue_enabled=False`（两份 env 模板均写 false）。非迁移批次、非开闸批次——本 step 只补文档与 parity 断言。真正的开闸（改 vm212 的 .env 为 true）与 post_office.data_json 的存量回填各属独立批次，零代码 diff，与本批不同车。

**为什么**：`deploy/backend/.env.example` 是 vm212 部署实际参照的模板；07-27B 审计 H2 把「多份 env 真值互相漂移」定为事故级问题类。

既有的 parity 断言按前缀分组（`GOVERNANCE_PREFIXES = ("CIVIC_","REP_","POLIS_OFFICE_")`、`REALISM_EVENT_MEMORY_`、`REALISM_POOL_`、`REALISM_PLAN_`，加上 P1-S10 新增的 `LOCATION_`），**没有一条覆盖 `DUTY_VENUE_` 前缀**（注意 `TOWN_DUTY_FUNDING_ENABLED` 是 `TOWN_` 前缀，不沾边）。而「扫不到」的表现与「deploy 模板里根本没有这个键」一模一样：全绿，运维照 deploy 模板起的环境里这个旋钮不存在。本 step 按仓内既定套路补一条同形状的 parity 断言 + 一条默认值断言 + 一条开闸前置文本断言。

**前置文本断言必须写进 deploy 模板**（同 `TOWN_DUTY_FUNDING` 的先例）：本闸的开闸顺序里有两个容易凭记忆搞错的点——(a) 它**不**依赖 `LOCATION_CAPABILITIES_ENABLED`（critic 第 66/67 条已证伪 P1/P3 文案里那句「P1 能力闸是 P2 硬前置」的说法：`location_capabilities` 与 `capability_location_at` 都是不读闸的纯查询）；(b) 真正的硬前置是**数据侧**的 `post_office.data_json` 回填，且回填必须独立批次。这两条不写进运维照着操作的那份模板，就只活在某个人的记忆里。

本 step 是 P2 邮局侧的收口：合入后在闸全关状态下跑全量默认门，失败集必须严格等于 54 基线（49 lab + 5 postpone），零新增。

#### 先写的测试（必须跑出失败）

改文件：/Volumes/data/dev/simverse-world/backend/tests/test_env_example_consistency.py —— 在文件末尾追加（不改动既有任何一行）：

```python

#: P2 营生场所的旋钮前缀。**必须单开一条**:DUTY_VENUE_ 既不在
#: GOVERNANCE_PREFIXES(CIVIC_/REP_/POLIS_OFFICE_)里,也不在 REALISM_POOL_ /
#: REALISM_PLAN_ / REALISM_EVENT_MEMORY_ / LOCATION_ 任何一条现成 parity 的前缀内
#: —— 五条现成的 parity 全都扫不到它(TOWN_DUTY_FUNDING_ENABLED 是 TOWN_ 前缀,
#: 不沾边)。而「扫不到」的表现与「deploy 模板里根本没有这个键」一模一样:全绿,
#: 运维照 deploy 模板起的环境里这个旋钮不存在(07-27B 审计 H2 把「多份 env 真值
#: 互相漂移」定为事故级问题类)。
#:
#: 前缀取到 DUTY_VENUE_ 而不是这一个键的全名:剧院侧(#7-#9)再加场所旋钮时自动
#: 被覆盖。
DUTY_VENUE_PREFIX = "DUTY_VENUE_"

#: (Settings 字段, env 键)。默认必须都是 false = 逐字节旧行为。
DUTY_VENUE_KNOBS = [
    ("duty_venue_enabled", "DUTY_VENUE_ENABLED"),
]


def test_duty_venue_knobs_exist_in_deploy_env_example_too():
    """营生场所的旋钮必须同时出现在两份 env 参考里。"""
    backend_keys = {k for k in _raw_keys(ENV_EXAMPLE)
                    if k.startswith(DUTY_VENUE_PREFIX)}
    assert backend_keys, "backend/.env.example 里没有任何营生场所旋钮?基线认知错误"
    assert backend_keys >= {env for _, env in DUTY_VENUE_KNOBS}, (
        f"backend/.env.example 缺营生场所旋钮: "
        f"{sorted({env for _, env in DUTY_VENUE_KNOBS} - backend_keys)}")
    missing = sorted(backend_keys - _raw_keys(DEPLOY_ENV_EXAMPLE))
    assert not missing, (
        f"deploy/backend/.env.example 缺营生场所旋钮(补上并保持默认关): {missing}")


def test_duty_venue_knobs_default_to_false_everywhere():
    """false = 逐字节旧行为,所以三处默认必须都是 false。

    任何一处模板写成 true,运维照它起的环境就是默认开闸 —— 而开闸会同时改写邮差
    WORK 的记忆内容与 decide 的目的地选择。
    """
    for field, env_key in DUTY_VENUE_KNOBS:
        assert Settings.model_fields[field].default is False, \
            f"Settings 里 {field} 的默认不是 False —— 新行为必须默认关"
        for path in (ENV_EXAMPLE, DEPLOY_ENV_EXAMPLE):
            assert f"{env_key}=false" in path.read_text(encoding="utf-8"), \
                f"{path} 里 {env_key} 的默认不是 false"


def test_deploy_env_states_the_duty_venue_prerequisites_and_rollback():
    """开闸硬前置与回滚保证必须写在运维照着操作的那份模板里。

    两条最容易凭记忆搞错的:
      · 本闸**不**依赖 LOCATION_CAPABILITIES_ENABLED(location_capabilities 与
        capability_location_at 都是不读闸的纯查询);
      · 真正的硬前置是数据侧的 post_office.data_json 回填,且必须独立批次。
    以及回滚保证:胶囊的封存/投递都不要求在邮局,闸翻回去不会让胶囊积压。
    """
    text = _deploy_env_text()
    assert "DUTY_VENUE_ENABLED" in text
    assert "postal" in text and "capabilities" in text
    assert "LOCATION_CAPABILITIES_ENABLED" in text and "不依赖" in text
    assert "独立批次" in text
    assert "sealed" in text          # 回滚护栏 SQL
```

先跑一次拿红。预期失败形态：`test_duty_venue_knobs_exist_in_deploy_env_example_too` 报 `deploy/backend/.env.example 缺营生场所旋钮(补上并保持默认关): ['DUTY_VENUE_ENABLED']`。

#### 实现

改文件：/Volumes/data/dev/simverse-world/deploy/backend/.env.example

锚点：文件末尾（P1-S10 已在此追加过 `LOCATION_CAPABILITIES_ENABLED=false` 块；本 step 追加在它之后）。追加：

```

# ── P2 营生场所（DUTY_VENUE_ENABLED）──────────────────────────────────────────
# 这个键是手工同步到本文件的（DUTY_VENUE_ 既不在 GOVERNANCE_PREFIXES 里，也不在
# REALISM_POOL_ / REALISM_PLAN_ / REALISM_EVENT_MEMORY_ / LOCATION_ 任何一条现成
# parity 的前缀内，五条 parity 全都扫不到它；TOWN_DUTY_FUNDING_ENABLED 是 TOWN_
# 前缀，不沾边），由 backend/tests/test_env_example_consistency.py 的 DUTY_VENUE_
# 前缀那条守着。
#
# 关（默认）= 逐字节旧行为：邮差 WORK 时照旧投递、照旧写同一条记忆文本、metadata
# 不写、feed payload 不多键；decide 的 _maybe_duty_venue 第一行即返回，决策排序与
# 今天等价。
# 开 = 两件事：
#   1 _work_postman 解析「投递现场」（站在提供 postal 能力的地点里），给记忆写
#     metadata['duty'] = {key, at, delivered}——两个分支都写（at 为 null 表示不在
#     现场），这是 M2 口径的唯一数据源，只在现场写会让分母塌成分子；
#   2 decide 新增 _maybe_duty_venue：营生有现场声明、今天还没上工（读的就是
#     on_work 写的 sv:duty_work:{id}）、人不在现场时，把这一 tick 定成
#     VISIT_DISTRICT 去现场（零 LLM）。插在 crowd 之后、计划跳过之前。
#
# 回滚保证（写死，别凭记忆）：胶囊的封存与投递**都不要求在邮局**。
# deliver_due_capsules 的 WHERE 不带任何 location 条件，nightly_cron 的无条件兜底
# 原样保留——邮局是「投递现场」不是「准入条件」。所以任何时刻把本闸翻回 false 都
# 不会让胶囊积压。护栏 SQL（恒为 0，违反即回滚）：
#   select count(*) from time_capsules
#    where status='sealed' and deliver_on < current_date - 1;
#
# 开闸硬顺序（写死，别凭记忆）：
#   1 代码侧——P1 的 location_caps / capability_location_at /
#     nearest_capability_location 必须已合入，且 CAPABILITIES 里登记了 postal
#     （civic_grantable=true）。
#     本闸**不依赖** LOCATION_CAPABILITIES_ENABLED：location_capabilities 与
#     capability_location_at 都是不读闸的纯查询，那道闸只管 location_category 的
#     能力派生层与 RESEARCH/EAT 两个门。两闸正交，谁先谁后都行。
#   2 数据侧（真正的硬前置）——生产 dynamic_locations 里 post_office 那行的
#     data_json 必须已带上 capabilities={"postal":{}}。存量行是公投建的，没有这个
#     键；回填是纯数据变更，**必须独立批次**（迁移/数据变更与开闸不同车，07-25
#     事故红线）。正确顺序：先部署代码（闸关）→ 单独一批回填 → 再单独一次开闸。
#     没回填就开闸不会出事，只是现场分支恒不命中、M2 的 on_site 恒为 0，与今天等价。
#
# 开闸后的核验（按天看，别看容器日志——它会轮转）：
#   M1 邮差真的去了：
#     select r.slug,
#            count(*) filter (where m.metadata_json->'move'->>'arrived'='true') arrivals,
#            count(*) attempts
#       from memories m join residents r on r.id = m.resident_id
#      where m.metadata_json->'move'->>'target' = 'post_office'
#        and m.created_at >= now() - interval '14 days'
#      group by 1 order by 2 desc;
#   M2 投递真的发生在邮局（on_site / work_runs ≥ 0.5，且 sum(delivered) 不低于
#   开闸前同期）：
#     select date_trunc('day', m.created_at) d,
#            sum((m.metadata_json->'duty'->>'delivered')::int) delivered,
#            count(*) filter (where m.metadata_json->'duty'->>'at'='post_office') on_site,
#            count(*) work_runs
#       from memories m
#      where m.metadata_json->'duty'->>'key' = 'postman'
#        and m.created_at >= now() - interval '14 days'
#      group by 1 order by 1;
#   memories.metadata_json 是 sa.JSON（PG 上是 json 不是 jsonb）：-> / ->> 可用，
#   @> 与 GIN 索引不可用，所以上面两条都必须带 created_at 时间窗。
DUTY_VENUE_ENABLED=false
```

说明：`backend/.env.example` 的对应行已在 P2-S3 落地，本 step 不再改动它。

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_env_example_consistency.py tests/test_deploy_env_protection.py tests/test_deploy_exposure.py tests/test_deploy_compose.py -q && .venv/bin/python -m pytest -q 2>&1 | tail -3
```

**验收**：1. 实现前 `test_duty_venue_knobs_exist_in_deploy_env_example_too` 红，且提示 `deploy/backend/.env.example 缺营生场所旋钮: ['DUTY_VENUE_ENABLED']`。2. 实现后四个 env/deploy 相关文件全绿。3. **P2 邮局侧收口硬门**：全量默认门（第二条命令）的失败集严格等于 54 基线（49 lab + 5 postpone），零新增失败——用 `git stash && .venv/bin/python -m pytest -q 2>&1 | tail -3` 取改前基线数字逐字对比，再 `git stash pop`。4. `grep -c 'DUTY_VENUE_ENABLED=false' .env.example ../deploy/backend/.env.example` 两处各为 1。5. `.venv/bin/python -c "from app.config import Settings; assert Settings.model_fields['duty_venue_enabled'].default is False"` 退出 0。

**commit**：

```
docs(env): DUTY_VENUE_ENABLED 同步 deploy 模板 + DUTY_VENUE_ 前缀 parity 断言
```

## P2 剧院侧第一组（design_P2.md 批次表 #7 + #8 + theater capabilities 声明）—— bite-sized TDD 执行计划

<details><summary>依赖边 / 批次归属 / 与既有守卫的冲突面</summary>

## 依赖边
- **P2-S7 → P1-S1**：`CAP_STAGE` 的登记已由 plan_P2_postal notes「新增依赖边 A」写死（`civic_grantable=True, unlocks=(), category=None`），本段直接引用、不重复定义；未登记则 `normalize_capabilities` 静默丢弃，S7 第一条测试即报出。
- **P2-S8/S9 → P1-S2/S8**：用 `map_data.has_capability` 与 `capability_locations`。P1-S9 的 `capability_locations` 禁用守卫只覆盖 `decide/basic.py` 与 `tick.py`，`civic_service.py` / `debate_service.py` 不在其列（已逐字复核 test_market_hall_constant.py）。
- **P2-S8 → P2-S3**：config 与两份 env 模板的锚点是 `duty_venue_enabled` / `DUTY_VENUE_ENABLED=false` 那一行。
- **对 P3-c 的反向约束**：S7 的测试**刻意不冻结** theater 的 bounds/center/entrance 数值（只判结构），否则 068_fix_theater_bounds 同批改 `CIVIC_AGENDA` 字面量时会带一条已知红。坐标越界归 P3，本段不做也不同批。

## 批次归属
S7 纯字面量（两张建楼票已关闭、topic 未动 → 零运行时行为）；S8 引入 `STAGE_EVENT_ENABLED=false` + 两份 env 模板 + parity 断言；S9/S10 沿用同一道闸。**四步无一条迁移、无一条开闸**。

## 串并行
严格串行 S7 → S8 → S9 → S10。S9 依赖 S8 的 `_debate_venue` 与闸；S10 只依赖 S8 的闸（可与 S9 并行，收益≈0）。

## 与既有守卫的冲突面
1. `test_env_example_consistency.py::test_every_settings_field_is_documented_or_allowlisted` —— S8 同 commit 补 `backend/.env.example`，verify_cmd 含该文件。
2. `GOVERNANCE_PREFIXES` 不含 `STAGE_`，parity **扫不到** = 与「模板里没这个键」同表现，故 S8 照 `REALISM_EVENT_MEMORY_` 先例新开一条 `STAGE_EVENT_` 前缀断言并双写 deploy 模板；**#9 的 `STAGE_EVENT_CROWD_ENABLED` 会被这条断言强制双写**。
3. `test_duty_service.py::test_on_work_lecturer_schedules_event` 零改动全绿（闸关时 type 仍是 news）。
4. 零新增 ActionType，四步各带 `len(list(ActionType)) == 16`。
5. 观众收益（记忆/心情/social/关系，明确不发币）属 #10，本段一个字不碰 `settle` / `_resident_aftermath`，零经济出口。

## 两处对 design 的校正
① 事件挂在**开票那一刻**而非 `:160` 进入 live 之后：live 段任一轮 LLM 失败会走 `_auto_draw_refund` 并 return，在 `:160` 建事件会留一条指着死辩论、还拉三倍人流一小时的幽灵事件。② `_work_lecturer` 的冷却查询必须**无条件**放宽成 `type IN ('news','script')`，否则翻闸（任一方向）当天冷却失效、讲师每次 WORK 都开新课。

## 仍未闭合（交接）
`active_event_location` 首命中即返：剧院事件与非集市 festival 同时 active 时会互相抢 `festival_draw_target`。集市 cohort 在 `decide/basic.py:387` 优先于它，caravan gameplay 不受影响。

</details>

### P2-S7 — CIVIC_AGENDA 剧院 effect.data 声明 stage 能力（规范 dict 形态）+ 白名单/topic 冻结/几何不冻结三条守卫

**Flag / 批次**：无（纯字面量：两张建楼票均已关闭，seed 幂等键 topic 未变 → 零运行时行为）。非迁移批次、非开闸批次。

**为什么**：design_P2.md §②-a 的数据前提：「theater 的 data_json 写 capabilities 即可——零迁移」。走 `civic_service._add_dynamic_location:923` 的 `payload = {k: v for k, v in data.items() if k != "slug"}` 整包落库 → `map_data.load_dynamic_locations:386` 整包进 LOCATIONS。

按 critic 权威裁决写规范形态 `dict[str, dict]`（`normalize_capabilities` 的不动点），不写设计里的 `["stage"]`。

**只改 data，topic 一个字符不动**：`seed_civic_agenda` 幂等键是 `Poll.question` 精确匹配（civic_service.py:208-210），改字就重开票；同 slug 再建走整包覆盖分支，旧键全丢。

**几何刻意不冻结**：theater 的 `bounds (172,40,178,50)` 是 design §④ 认定的越界坐标，归 P3-c 迁移批同批改这里的字面量。pin 数值 = 给 P3 埋一条已知红，所以只判结构不变量（entrance/center 落在 bounds 内、键集不多不少）。

零运行时行为：两张建楼票都已关闭，改动只对「将来重投重建」生效，是产能修复不是存量修复。

#### 先写的测试（必须跑出失败）

新建文件：/Volumes/data/dev/simverse-world/backend/tests/test_civic_agenda_theater_stage.py

```python
"""P2-S7: 剧院 effect.data 的 stage 能力声明 —— 规范 dict 形态 + 三条守卫。

与邮局那条(P2-S1)同形状,但多一条**几何不冻结**的纪律:theater 的
bounds/center/entrance 是 design_P2.md §④ 认定的越界坐标(x2=178 > WALKABLE_X_RANGE
上限 173),归 P3-c 的迁移批次修,而 068_fix_theater_bounds 同批要改这里的字面量。
所以本文件只冻结**非几何**字段与结构不变量,绝不 pin 具体数值 —— pin 了,P3-c 落地
当天这条测试就是一条已知红。

第一条是 P2 → P1-S1 的依赖边守卫:stage 没登记 → normalize_capabilities 会把它
静默丢弃(只 logger.debug),全链零告警。所以这里用字符串字面量而不是 import 常量,
好让失败信息直接说清该改哪。
"""
import pytest
from sqlalchemy import select

from app.agent.actions import ActionType
from app.agent.location_caps import (
    CAPABILITIES,
    CIVIC_GRANTABLE_CAPABILITIES,
    normalize_capabilities,
)
from app.models.season import Poll
from app.services import civic_service
from app.services.civic_service import CIVIC_AGENDA

#: 生产两张建楼票的 topic 逐字快照。**任何 data 改动都不得让它变化。**
FROZEN_TOPICS = ["在南苑空地兴建一座邮局", "在东岸花园兴建一座剧院"]

#: 剧院 effect.data 的完整键集(加上本 step 的 capabilities)。用「不多不少」而不是
#: 「至少有」——多一个键就是有人顺手往公投载荷里塞了别的东西。
THEATER_KEYS = {
    "slug", "name", "type", "role", "bounds", "center", "entrance",
    "description", "boosted_actions", "capabilities",
}


def _agenda_data(slug: str) -> dict:
    for item in CIVIC_AGENDA:
        for opt in item["options"]:
            data = ((opt.get("effect") or {}).get("data") or {})
            if data.get("slug") == slug:
                return data
    raise AssertionError(f"CIVIC_AGENDA 里没有 slug={slug} 的建楼选项")


def test_stage_is_registered_by_p1_s1():
    """P2 → P1-S1 的依赖边:stage 必须先在闭集注册表里登记且可被公投授予。"""
    assert "stage" in CAPABILITIES, (
        "app/agent/location_caps.py 的 CAPABILITIES 缺 'stage' —— P1-S1 必须先登记"
        "(civic_grantable=True, unlocks=(), category=None),见 P2 计划 notes 的"
        "「新增依赖边 A」")
    spec = CAPABILITIES["stage"]
    assert spec.civic_grantable is True
    assert spec.unlocks == (), "stage 不得解锁任何动作 —— P2 零新增 ActionType"
    assert spec.category is None, "stage 不得派生 category(会污染 EAT 通路)"
    assert "stage" in CIVIC_GRANTABLE_CAPABILITIES


def test_theater_declares_stage_in_the_canonical_dict_form():
    assert _agenda_data("theater")["capabilities"] == {"stage": {}}


def test_the_declaration_is_a_fixed_point_of_normalization():
    """规范形态 = 归一化的不动点。写成 [\"stage\"] 也能用,但落库的就不是规范形态。"""
    declared = _agenda_data("theater")["capabilities"]
    assert normalize_capabilities(declared) == declared
    assert normalize_capabilities(["stage"]) == declared  # 宽松入口仍等价


def test_theater_grants_nothing_outside_the_civic_whitelist():
    """CIVIC_AGENDA 是「公投能造出什么」的源头(routers/polls.py:94-96 允许 admin
    附带任意 effect dict,_add_dynamic_location 只校验 slug 非空 + bounds 在就整包
    落库)。research 恒不在白名单里,否则一张票就能绕过实验楼的地点门。"""
    declared = normalize_capabilities(_agenda_data("theater").get("capabilities"))
    assert set(declared) <= CIVIC_GRANTABLE_CAPABILITIES
    assert "research" not in declared


def test_only_the_data_changed_topics_stay_frozen():
    assert [item["topic"] for item in CIVIC_AGENDA] == FROZEN_TOPICS


def test_the_non_geometry_half_of_the_theater_payload_is_untouched():
    data = _agenda_data("theater")
    assert set(data) == THEATER_KEYS
    assert data["name"] == "剧院"
    assert data["type"] == "public" and data["role"] == "culture"
    assert data["description"] == "小镇剧院:说书、演展、故事会的舞台"
    assert data["boosted_actions"] == ["CHAT_RESIDENT", "OBSERVE"]


def test_the_geometry_is_structurally_valid_but_deliberately_not_frozen():
    """只判结构,不 pin 数值 —— 数值归 P3-c(068_fix_theater_bounds 同批改这里的
    字面量)。pin 了就是给那一批埋一条已知红。"""
    data = _agenda_data("theater")
    x1, y1, x2, y2 = data["bounds"]
    assert x1 < x2 and y1 < y2
    for key in ("center", "entrance"):
        px, py = data[key]
        assert x1 <= px <= x2 and y1 <= py <= y2, key


def test_boosted_actions_are_real_action_types():
    """prompts.py 的 boosted 提示句直接吃这些字符串,拼错就是一句永远命不中的提示。"""
    names = {a.name for a in ActionType}
    for item in CIVIC_AGENDA:
        for opt in item["options"]:
            data = ((opt.get("effect") or {}).get("data") or {})
            for act in data.get("boosted_actions") or []:
                assert act in names, (data.get("slug"), act)


@pytest.mark.anyio
async def test_seed_is_still_idempotent_on_the_frozen_topics(db_session):
    """topic 没动 → 已有票的世界不会因为 data 改动重开票(否则同 slug 整包覆盖)。"""
    for topic in FROZEN_TOPICS:
        db_session.add(Poll(question=topic, options_json=[], status="closed"))
    await db_session.commit()

    assert await civic_service.seed_civic_agenda(db_session) == 0
    rows = (await db_session.execute(select(Poll))).scalars().all()
    assert len(rows) == len(FROZEN_TOPICS)


def test_action_type_enum_is_untouched():
    """P2 全段零新增 ActionType(design_P2.md §「为什么不新增 ActionType」)。"""
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
```

先跑一次拿红。预期失败形态（按 P1-S1 是否已改分两种，两种都必须先出现）：
- P1-S1 未登记 → `test_stage_is_registered_by_p1_s1` 失败，信息直指 `CAPABILITIES 缺 'stage'`；
- P1-S1 已登记 → `test_theater_declares_stage_in_the_canonical_dict_form` 抛 `KeyError: 'capabilities'`，且 `test_the_non_geometry_half_of_the_theater_payload_is_untouched` 报键集少了 `capabilities`。

#### 实现

改文件：/Volumes/data/dev/simverse-world/backend/app/services/civic_service.py

锚点：`CIVIC_AGENDA` 的第二条（剧院），civic_service.py:188-193。`grep -c '"slug": "theater"' app/services/civic_service.py` 应为 1，确认锚点唯一。

before（civic_service.py:188-193）：
```python
            {"label": "赞成兴建", "effect": {"type": "dynamic_location", "data": {
                "slug": "theater", "name": "剧院", "type": "public", "role": "culture",
                "bounds": [172, 40, 178, 50], "center": [175, 45], "entrance": [172, 45],
                "description": "小镇剧院:说书、演展、故事会的舞台",
                "boosted_actions": ["CHAT_RESIDENT", "OBSERVE"],
            }}},
```

after：
```python
            {"label": "赞成兴建", "effect": {"type": "dynamic_location", "data": {
                "slug": "theater", "name": "剧院", "type": "public", "role": "culture",
                "bounds": [172, 40, 178, 50], "center": [175, 45], "entrance": [172, 45],
                "description": "小镇剧院:说书、演展、故事会的舞台",
                "boosted_actions": ["CHAT_RESIDENT", "OBSERVE"],
                # P2 #7:剧院是「上演场地」。规范形态是 dict[str, dict]
                # (location_caps.normalize_capabilities 的不动点);effect.data 除
                # slug 外整包落进 dynamic_locations.data_json(:923),再整包进
                # LOCATIONS(map_data.py:386)—— 零迁移、零模型改动。
                # 只改 data:topic 一个字符都不能动,seed_civic_agenda 的幂等键是
                # Poll.question 精确匹配(:208-210),改字就重开票,而同 slug 再建走的
                # 是整包覆盖分支(existing.data_json = payload),旧键全丢。
                # 上面那三行几何坐标越界(x2=178 > WALKABLE_X_RANGE 上限 173),归
                # P3-c 的迁移批次改,本批一个数字都不动、也不与它同车。
                # 存量那行的回填是纯数据变更,属独立批次。
                "capabilities": {"stage": {}},
            }}},
```

本 step 不改任何其它文件。

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_civic_agenda_theater_stage.py tests/test_civic_agenda_capabilities.py tests/test_location_caps.py tests/test_world_governance.py tests/test_m3_civic.py tests/test_lab_building.py -q
```

**验收**：1. 实现前同一命令必须红，且失败项是上面两种形态之一（若是第一种，先按 notes 的依赖边改 P1-S1 并让 `tests/test_location_caps.py` 重新全绿，再回到本 step）。2. 实现后全部 passed、failed=0。3. `git diff --numstat app/services/civic_service.py` 的 deletions 列为 0（纯插入）。4. `git diff app/services/civic_service.py | grep -c '在南苑空地兴建一座邮局\|在东岸花园兴建一座剧院'` 输出 0（两条 topic 逐字未动）。5. `git diff app/services/civic_service.py | grep -c '172, 40, 178, 50\|175, 45\|172, 45'` 输出 0（几何坐标一个数字没动，P3-c 的地盘没被侵占）。6. `len(list(ActionType)) == 16` 由本文件与 `tests/test_lab_building.py` 双份钉死。

**commit**：

```
feat(civic): 剧院 effect.data 声明 stage 能力(规范 dict 形态)——topic 与几何均冻结,零迁移零行为
```

### P2-S8 — 引入 STAGE_EVENT_ENABLED（默认关，两份 env 模板 + STAGE_EVENT_ 前缀 parity 断言）+ create_debate(*, venue=None) 与场地的 Redis 传递

**Flag / 批次**：新增 `stage_event_enabled: bool = False`（env `STAGE_EVENT_ENABLED`，`backend/.env.example` 与 `deploy/backend/.env.example` 同 commit 各写一行 false，并新增 `STAGE_EVENT_` 前缀 parity 断言 —— #9 的 `STAGE_EVENT_CROWD_ENABLED` 会被它强制双写）。关 = 逐字节旧行为。非迁移批次、非开闸批次。

**为什么**：design_P2.md §②-a 要求 `create_debate` 收 `venue`、`run_live` 消费它，但 `run_live(db, debate)` 只拿得到 `Debate`，而 `debates` 表 13 列无 location（models/debate.py:17-31），加列 = 迁移，触犯「迁移与开闸不同车」红线。本 step 把这段缺口按**同文件既有先例**补上：`_VOTING_SINCE_KEY`（debate_service.py:44）已经在用 Redis 给 `run_live`→`settle` 传相位时刻，场地照抄同一条思路、同一个失败姿势。

**fail-closed，不臆造场地**：读不到就不建事件。announced→live 只隔 `debate_stake_window_min`（默认 30 分钟），要在这个窗口里丢 Redis 才漏得掉，代价上限是一场辩论没观众；而回落到「随便挑一个 stage 地点」会把全镇往错的楼里拉。

**写入侧与读取侧都校验 stage 声明**：Redis 里的值可能是几天前写的，而能力声明是公投可改的数据。

本 step 零生产调用方传 venue（civic 接线在 S9），闸只有「存在、默认关、两份模板都写了」这一层语义。

#### 先写的测试（必须跑出失败）

新建文件：/Volumes/data/dev/simverse-world/backend/tests/test_stage_event_venue.py

```python
"""P2-S8: STAGE_EVENT_ENABLED 闸 + create_debate 的 venue 参数与场地的 Redis 传递。

本 step 只把「场地」从 create_debate 送到 run_live 读得到的地方,不建任何
WorldEvent(那是 P2-S9)。debates 表不加列 —— 加列 = 迁移,触犯「迁移与开闸不同车」。
场地走 Redis,与同文件 _VOTING_SINCE_KEY 给 settle 传相位时刻是同一条思路。

fail-closed:读不到就没有场地,绝不臆造。announced→live 只隔 debate_stake_window_min
(默认 30 分钟),要在这个窗口里丢 Redis 才漏得掉一场的人流拉力。
"""
import inspect
from pathlib import Path

import pytest

from app.agent.actions import ActionType
from app.agent.location_caps import CAP_STAGE
from app.agent.map_data import LOCATIONS
from app.config import Settings
from app.models.debate import Debate
from app.models.resident import Resident
from app.services import debate_service as ds

BACKEND = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = BACKEND / ".env.example"
DEPLOY_ENV_EXAMPLE = BACKEND.parent / "deploy" / "backend" / ".env.example"

# 生产 dynamic_locations 里 theater 那行的 data_json(2026-08 公投建,active=t);
# capabilities 由调用方按场景决定加不加 —— 存量行今天**没有**这个键。
THEATER = {
    "name": "剧院", "type": "public", "role": "culture",
    "bounds": (172, 40, 178, 50), "center": (175, 45), "entrance": (172, 45),
    "description": "小镇剧院:说书、演展、故事会的舞台",
    "boosted_actions": ["CHAT_RESIDENT", "OBSERVE"],
}


@pytest.fixture
def overlay():
    """模拟 load_dynamic_locations 的合入:追加到 LOCATIONS 尾部,再还原。"""
    added: list[str] = []

    def _merge(slug: str, data: dict, capabilities=None) -> str:
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


async def _residents(db):
    db.add_all([
        Resident(slug="ann", name="安", creator_id="system", district="cafe",
                 resident_type="npc", status="idle", tile_x=1, tile_y=1),
        Resident(slug="bo", name="波", creator_id="system", district="cafe",
                 resident_type="npc", status="idle", tile_x=2, tile_y=2),
    ])
    await db.commit()


async def _debate(db, **kw):
    await _residents(db)
    return await ds.create_debate(db, "猫和狗谁更好", "ann", "bo", **kw)


def _redis_down():
    raise RuntimeError("redis down")


# ── 闸 ────────────────────────────────────────────────────────────────

def test_flag_defaults_to_off():
    assert Settings.model_fields["stage_event_enabled"].default is False


def test_flag_is_documented_as_false_in_both_env_templates():
    for path in (ENV_EXAMPLE, DEPLOY_ENV_EXAMPLE):
        assert "STAGE_EVENT_ENABLED=false" in path.read_text(encoding="utf-8"), path


# ── 签名与 schema ─────────────────────────────────────────────────────

def test_venue_is_keyword_only_and_defaults_to_none():
    """默认 None = 今天所有调用方逐字节不变。"""
    sig = inspect.signature(ds.create_debate)
    assert list(sig.parameters) == ["db", "topic", "a_slug", "b_slug", "venue"]
    p = sig.parameters["venue"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY
    assert p.default is None


def test_debates_table_gained_no_location_column():
    """零迁移:场地不进 schema。"""
    assert set(Debate.__table__.columns.keys()) == {
        "id", "topic", "resident_a_slug", "resident_b_slug", "status",
        "transcript_json", "winner", "pool_a", "pool_b", "votes_a", "votes_b",
        "starts_at", "settled_at"}


# ── 传递 ──────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_no_venue_means_nothing_is_remembered(db_session):
    d = await _debate(db_session)
    assert await ds._debate_venue(d.id) is None


@pytest.mark.anyio
async def test_a_stage_venue_round_trips(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    d = await _debate(db_session, venue="theater")
    assert await ds._debate_venue(d.id) == "theater"


@pytest.mark.anyio
async def test_legacy_row_without_the_declaration_is_not_a_venue(db_session, overlay):
    """存量 dynamic_locations 行没有 capabilities 键 —— 未回填时静默降级,不抛。"""
    overlay("theater", THEATER)
    d = await _debate(db_session, venue="theater")
    assert await ds._debate_venue(d.id) is None


@pytest.mark.anyio
async def test_an_unknown_slug_is_not_a_venue(db_session):
    d = await _debate(db_session, venue="nowhere")
    assert await ds._debate_venue(d.id) is None


@pytest.mark.anyio
async def test_the_venue_is_revalidated_on_read(db_session, overlay):
    """Redis 里的值可能是几天前写的,而能力声明是公投随时能改的数据。"""
    slug = overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    d = await _debate(db_session, venue=slug)
    LOCATIONS[slug].pop("capabilities")
    assert await ds._debate_venue(d.id) is None


@pytest.mark.anyio
async def test_redis_loss_degrades_to_no_venue(db_session, overlay, monkeypatch):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    d = await _debate(db_session, venue="theater")
    monkeypatch.setattr("app.redis_client.get_redis", _redis_down)
    assert await ds._debate_venue(d.id) is None


@pytest.mark.anyio
async def test_a_redis_write_failure_never_breaks_debate_creation(
        db_session, overlay, monkeypatch):
    """场地是叙事装饰;辩论本体(玩家能押注的那个对象)不能因它建不出来。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    monkeypatch.setattr("app.redis_client.get_redis", _redis_down)
    d = await _debate(db_session, venue="theater")
    assert d.id and d.status == "announced"


def test_action_type_enum_is_untouched():
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
```

再改文件：/Volumes/data/dev/simverse-world/backend/tests/test_env_example_consistency.py —— 在文件末尾（`test_pool_reserved_slot_defaults_to_zero_everywhere` 之后）追加：

```python

#: 剧院/舞台事件的旋钮前缀。和 ``EVENT_MEMORY_TIER_PREFIX`` / ``POOL_RESERVE_PREFIX``
#: 一个道理:``STAGE_`` **不在** ``GOVERNANCE_PREFIXES``(政治层三条线)里,上面那条
#: parity 扫不到本批的键 —— 而「扫不到」的表现和「deploy 模板里根本没这个键」一模
#: 一样,全绿(07-27B 审计 H2 把「多份 env 真值互相漂移」定为事故级问题类)。
#:
#: 前缀取到 ``STAGE_EVENT_`` 而不是钉死 ``STAGE_EVENT_ENABLED``:P2 #9 还要落一个
#: ``STAGE_EVENT_CROWD_ENABLED``,钉死单键的话第二个旋钮落地时这条 parity 会静默
#: 扫不到它。
STAGE_EVENT_PREFIX = "STAGE_EVENT_"


def test_stage_event_knobs_exist_in_deploy_env_example_too():
    """剧院/舞台事件的旋钮必须同时出现在两份 env 参考里。"""
    backend_keys = {k for k in _raw_keys(ENV_EXAMPLE)
                    if k.startswith(STAGE_EVENT_PREFIX)}
    assert backend_keys, "backend/.env.example 里没有任何舞台事件旋钮?基线认知错误"
    missing = sorted(backend_keys - _raw_keys(DEPLOY_ENV_EXAMPLE))
    assert not missing, (
        f"deploy/backend/.env.example 缺舞台事件旋钮(补上并保持默认关): {missing}")


def test_stage_event_knobs_default_to_off_everywhere():
    """三处默认必须一致地关:Settings 一处、两份模板各一处。"""
    backend_keys = {k for k in _raw_keys(ENV_EXAMPLE)
                    if k.startswith(STAGE_EVENT_PREFIX)}
    for env_key in sorted(backend_keys):
        field = env_key.lower()
        assert Settings.model_fields[field].default is False, \
            f"Settings 里 {field} 的默认不是 False —— 新闸必须默认关"
        for path in (ENV_EXAMPLE, DEPLOY_ENV_EXAMPLE):
            assert f"{env_key}=false" in path.read_text(encoding="utf-8"), \
                f"{path} 里 {env_key} 的默认不是 false"
```

先跑一次拿红。预期失败形态：`test_flag_defaults_to_off` 抛 `KeyError: 'stage_event_enabled'`；`test_venue_is_keyword_only_and_defaults_to_none` 报参数列表是 `['db','topic','a_slug','b_slug']`；`test_no_venue_means_nothing_is_remembered` 抛 `AttributeError: module 'app.services.debate_service' has no attribute '_debate_venue'`；`test_stage_event_knobs_exist_in_deploy_env_example_too` 报「没有任何舞台事件旋钮」。

#### 实现

**改动 1** —— /Volumes/data/dev/simverse-world/backend/app/config.py

锚点：P2-S3 插入的 `duty_venue_enabled: bool = False` 那一行与其后的空行 + `# P2 Task 1 —` 注释之间（`grep -c 'duty_venue_enabled' app/config.py` 应为 1）。

before：
```python
    duty_venue_enabled: bool = False

    # P2 Task 1 — relation write deltas (reused, zero new LLM calls) + decay.
```
after：
```python
    duty_venue_enabled: bool = False

    # --- P2 剧院/舞台事件 (STAGE_EVENT_*) ---
    # 「上演场地」语义:辩论开票时在声明了 stage 能力的地点挂一条 type="script" 的
    # 世界事件,讲师的公开课也从 "news" 改成 "script"。关键在 type ——
    # crowd_service._EVENT_TYPES_WITH_CROWD 是 ("festival","script"),"news" 不在
    # 里面,所以公开课的 ×realism_festival_weight 人流拉力**从来没生效过**。
    # 关 = 逐字节旧行为:不建任何舞台事件、公开课仍是 "news"、辩论生命周期一步不变。
    # 零新增 ActionType、零新增经济出口(观众收益只走记忆/心情/social/关系,
    # debate settle 的 5% burn 是唯一真金出口,不得双花)。
    stage_event_enabled: bool = False

    # P2 Task 1 — relation write deltas (reused, zero new LLM calls) + decay.
```

**改动 2** —— /Volumes/data/dev/simverse-world/backend/app/services/debate_service.py，锚点：`_VOTING_SINCE_TTL = 7 * 86400`（debate_service.py:45）之后、`DEBATE_SYSTEM = (`（:47）之前。

before：
```python
_VOTING_SINCE_KEY = "sv:debate_voting_since:{id}"
_VOTING_SINCE_TTL = 7 * 86400

DEBATE_SYSTEM = (
```
after：
```python
_VOTING_SINCE_KEY = "sv:debate_voting_since:{id}"
_VOTING_SINCE_TTL = 7 * 86400

#: 辩论的上演场地。debates 表 13 列无 location,而本批次不动 schema(红线:迁移与
#: 行为变更不得同一次变更),所以 create_debate 收到的 venue 走 Redis 传给 run_live
#: —— 与上面 _VOTING_SINCE_KEY 同一条思路、同一个失败姿势。
#: 读不到 = **不建事件**(fail-closed),绝不臆造场地:announced → live 只隔
#: debate_stake_window_min(默认 30 分钟),要在这个窗口里丢 Redis 才会漏掉一场的
#: 人流拉力,代价上限是一场辩论没观众;而回落到「随便挑一个 stage 地点」会把全镇
#: 往错的楼里拉。
_VENUE_KEY = "sv:debate_venue:{id}"
_VENUE_TTL = 7 * 86400

DEBATE_SYSTEM = (
```

**改动 3** —— 同文件 `create_debate`（debate_service.py:60-77），整体替换为：

```python
async def create_debate(db, topic: str, a_slug: str, b_slug: str,
                        *, venue: str | None = None) -> Debate:
    d = Debate(topic=topic, resident_a_slug=a_slug, resident_b_slug=b_slug, status="announced")
    db.add(d)
    await db.commit()
    await db.refresh(d)
    # P2 #7:上演场地。venue 为 None(今天所有调用方的默认)时整段跳过,与改前逐字节
    # 相同。场地不进 schema —— 见 _VENUE_KEY 的说明。
    if venue:
        await _remember_venue(d.id, venue)
    # S1-3: seed opposing issue stances for the two debaters. `announced` is
    # the reliable first-hand signal — the debate lifecycle stops here today
    # (no live/settle driver in app code). Best-effort + gated: an opinion
    # failure must never break debate creation.
    try:
        from app.config import settings
        if settings.polis_opinion_enabled:
            from app.services.opinion_service import OpinionService
            await OpinionService(db).update_from_debate(d, seed_only=True)
    except Exception:
        logger.warning("opinion seed from create_debate failed", exc_info=True)
    return d


async def _remember_venue(debate_id: str, venue: str) -> None:
    """记下这场辩论的上演场地。只有声明了 stage 能力的地点才算数。

    校验放在写入侧:写入侧知道调用方是谁(civic_service 的公开课链),读取侧只拿得到
    一个字符串。地点没声明 stage 就当没给场地 —— 存量 dynamic_locations 行没有
    capabilities 键时天然走这条路,缺省安全。
    """
    from app.agent.location_caps import CAP_STAGE
    from app.agent.map_data import has_capability
    if not has_capability(venue, CAP_STAGE):
        logger.debug("debate venue %r does not declare stage; ignored", venue)
        return
    try:
        from app.redis_client import get_redis
        await get_redis().set(_VENUE_KEY.format(id=debate_id), venue, ex=_VENUE_TTL)
    except Exception:
        # 场地是叙事装饰;辩论本体(玩家能押注的那个对象)不能因它建不出来。
        logger.warning("debate venue mark failed for %s", debate_id, exc_info=True)


async def _debate_venue(debate_id: str) -> str | None:
    """读回上演场地;没记过 / 读不出来 / 地点已不再声明 stage → None。

    读取侧**再校验一次**:Redis 里的值可能是几天前写的,而能力声明是公投随时能改的
    数据。两侧都判,任一侧不成立就退化成「没有场地」= 今天的行为。
    """
    try:
        from app.redis_client import get_redis
        raw = await get_redis().get(_VENUE_KEY.format(id=debate_id))
    except Exception:
        logger.warning("debate venue read failed for %s", debate_id, exc_info=True)
        return None
    if not raw:
        return None
    from app.agent.location_caps import CAP_STAGE
    from app.agent.map_data import has_capability
    return raw if has_capability(raw, CAP_STAGE) else None
```

**改动 4** —— /Volumes/data/dev/simverse-world/backend/.env.example

锚点：P2-S3 插入的 `DUTY_VENUE_ENABLED=false` 那一行之后（`grep -c '^DUTY_VENUE_ENABLED=false' .env.example` 应为 1）。在其后追加：

```

# ── P2 剧院/舞台事件（STAGE_EVENT_ENABLED）─────────────────────────────────────
# 关（默认）= 逐字节旧行为：辩论生命周期一步不变、不建任何舞台世界事件、讲师的
# 公开课仍然是 type="news"。
# 开 = 两件事：
#   1 辩论开票的同一刻，在声明了 stage 能力的地点挂一条 type="script" 的 WorldEvent
#     （ends_at = 投票窗口 DEBATE_VOTE_WINDOW_MIN）；
#   2 讲师公开课的事件 type 从 "news" 改成 "script"。
#
# 为什么是 "script"：crowd_service._EVENT_TYPES_WITH_CROWD = ("festival", "script")，
# "news" 不在里面 —— 所以 active_event_location 永远看不到公开课，
# festival_draw_target 的 ×REALISM_FESTIVAL_WEIGHT 人流拉力对公开课**一次都没生效
# 过**。改 type 是零改动接上那条已经过生产验证的拉力（集市日走的就是它）。
# 不把 "news" 加进那个元组：那会让 NEWS_POOL 的四条随机新闻也长出人流语义。
#
# 不改变的事：零新增 ActionType；零新增经济出口——观众收益只写记忆/心情/social/
# 关系，debate settle 的 5% burn 分账是唯一真金出口，不得双花。
#
# 开闸硬前置（写死，别凭记忆）：
#   1 代码侧——P1 的 location_caps / has_capability / capability_locations 必须已
#     合入，且 CAPABILITIES 里登记了 stage（civic_grantable=true）。
#     本闸**不**依赖 LOCATION_CAPABILITIES_ENABLED：location_capabilities 与
#     has_capability 都是不读闸的纯查询。
#   2 依赖 REALISM_CROWD_ENABLED=true —— ×3 人流拉力由它把关
#     （decide/basic.py 的 _maybe_crowd_draw）。它关着的话本闸只剩 prompt 里多一条
#     事件描述，人流一个不动。
#   3 数据侧——生产 dynamic_locations 里 theater 那行的 data_json 必须已带上
#     capabilities={"stage":{}}。存量行是公投建的，没有这个键；回填是纯数据变更，
#     **必须独立批次**。没回填就开闸不会出事，只是永远解析不到场地、不建事件，
#     与今天等价。
# 护栏 SQL（恒为 0，违反即回滚）：
#   select count(*) from debates
#    where status <> 'settled' and starts_at < now() - interval '2 days';
STAGE_EVENT_ENABLED=false
```

**改动 5** —— /Volumes/data/dev/simverse-world/deploy/backend/.env.example

锚点：P2-S5 插入的 `DUTY_VENUE_ENABLED=false` 那一行之后。追加同一块文本（逐字相同，含 `STAGE_EVENT_ENABLED=false`）。

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_stage_event_venue.py tests/test_env_example_consistency.py tests/test_deploy_env_protection.py tests/test_debates.py tests/test_debate_driver.py tests/test_opinion_service.py tests/test_m3_civic.py tests/test_lab_building.py -q
```

**验收**：1. 实现前红：`test_flag_defaults_to_off` 抛 `KeyError: 'stage_event_enabled'`，`test_no_venue_means_nothing_is_remembered` 抛 `AttributeError: ... has no attribute '_debate_venue'`，`test_stage_event_knobs_exist_in_deploy_env_example_too` 报「没有任何舞台事件旋钮」。2. 实现后全绿，其中 `tests/test_debates.py`、`tests/test_debate_driver.py`、`tests/test_opinion_service.py`、`tests/test_m3_civic.py` 零改动全绿（venue 默认 None → 逐字节旧行为）。3. `tests/test_env_example_consistency.py::test_every_settings_field_is_documented_or_allowlisted` 绿。4. `grep -c '^STAGE_EVENT_ENABLED=false' .env.example ../deploy/backend/.env.example` 两处各为 1。5. `.venv/bin/python -c "from app.config import Settings; assert Settings.model_fields['stage_event_enabled'].default is False"` 退出 0。6. `grep -rn 'stage_event_enabled' app/` 只命中 `app/config.py` 一行（本 step 零消费方，闸读在 S9/S10 的调用点）。7. `.venv/bin/python -m alembic heads` 输出与改前逐字相同（零迁移）。

**commit**：

```
feat(debate): create_debate 收 venue 并经 Redis 传给 run_live,挂 STAGE_EVENT_ENABLED 默认关
```

### P2-S9 — run_live 在开票那一刻建 type="script" 舞台事件 + civic 接线按能力反查 venue（挂 STAGE_EVENT_ENABLED）

**Flag / 批次**：沿用 `stage_event_enabled=False`（S8 引入，两份 env 模板均 false）。关 = 零世界事件、辩论生命周期一步不变。非迁移批次、非开闸批次。

**为什么**：design_P2.md §②-a 的本体：用 `type="script"` 零改动接上 `_EVENT_TYPES_WITH_CROWD`（crowd_service.py:28）已有的 ×`realism_festival_weight` 人流拉力，并自动进入所有居民的 decide prompt。

**对 design 的一处校正——事件挂在开票那一刻，不是 `:160` 进入 live 之后**：live 段任一轮 LLM 失败会走 `_auto_draw_refund` 并 return（debate_service.py:204-208 上方），那条路上辩论当场 settled。在 `:160` 建事件会留下一条指着死辩论、还要拉三倍人流一小时的幽灵事件，还得再写一段补偿清理。挂在开票这一刻在同一条成功路径上，墙钟只差六轮 LLM（数十秒），而 `ends_at` 取 `debate_vote_window_min` 本来就该从「投票开始」量起——这正是 `drive_due_debates` 打 `_mark_voting_since` 的同一刻（:337-341）。

**civic 侧不硬编码 `"theater"`**：剧院是公投建的动态行，slug 是数据不是代码常量；走 `capability_locations(CAP_STAGE)` 反查。`civic_service.py` 不在 P1-S9 的能力反查禁用清单里（那条只覆盖 `decide/basic.py` 与 `tick.py`）。

#### 先写的测试（必须跑出失败）

新建文件：/Volumes/data/dev/simverse-world/backend/tests/test_stage_event_debate.py

```python
"""P2-S9: run_live 在开票那一刻挂一条 type="script" 的舞台事件 + civic 接线。

最要紧的那条断言是 test_the_event_is_visible_to_the_crowd_puller:
crowd_service._EVENT_TYPES_WITH_CROWD 是 ("festival","script"),用 "news" 建的事件
active_event_location 一辈子看不见 —— 学院公开课十五天零到访就是这么来的。

第二要紧的是 test_an_aborted_debate_leaves_no_ghost_event:LLM 失败会走
_auto_draw_refund 当场 settled,若照 design 在进入 live 时建事件,就会留下一条指着
死辩论、还要拉三倍人流一小时的幽灵事件。
"""
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.agent.actions import ActionType
from app.agent.location_caps import CAP_STAGE
from app.agent.map_data import LOCATIONS
from app.config import settings
from app.models.resident import Resident
from app.models.world_event import WorldEvent
from app.services import civic_service, crowd_service
from app.services import debate_service as ds
from app.services.world_event_service import _to_dict

DEBATE_SRC = (Path(__file__).resolve().parents[1]
              / "app" / "services" / "debate_service.py")

THEATER = {
    "name": "剧院", "type": "public", "role": "culture",
    "bounds": (172, 40, 178, 50), "center": (175, 45), "entrance": (172, 45),
    "description": "小镇剧院:说书、演展、故事会的舞台",
    "boosted_actions": ["CHAT_RESIDENT", "OBSERVE"],
}


@pytest.fixture
def overlay():
    added: list[str] = []

    def _merge(slug: str, data: dict, capabilities=None) -> str:
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


def _mock_client(text="我方观点更站得住脚。"):
    client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    client.messages.create = AsyncMock(return_value=resp)
    return client


async def _residents(db):
    db.add_all([
        Resident(slug="ann", name="安", creator_id="system", district="cafe",
                 resident_type="npc", status="idle", tile_x=1, tile_y=1),
        Resident(slug="bo", name="波", creator_id="system", district="cafe",
                 resident_type="npc", status="idle", tile_x=2, tile_y=2),
    ])
    await db.commit()


async def _run(db, *, topic="猫和狗谁更好", venue=None, text="我方观点更站得住脚。"):
    await _residents(db)
    d = await ds.create_debate(db, topic, "ann", "bo", venue=venue)
    with patch("app.llm.client.get_client", return_value=_mock_client(text)), \
         patch("app.llm.metering.record_usage", new_callable=AsyncMock):
        await ds.run_live(db, d)
    return d


async def _events(db) -> list[WorldEvent]:
    return (await db.execute(select(WorldEvent))).scalars().all()


# ── 闸关 = 逐字节旧行为 ───────────────────────────────────────────────

@pytest.mark.anyio
async def test_gate_off_creates_no_world_event(db_session, overlay, monkeypatch):
    monkeypatch.setattr(settings, "stage_event_enabled", False)
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    d = await _run(db_session, venue="theater")
    assert d.status == "voting"
    assert await _events(db_session) == []


# ── 闸开 ──────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_gate_on_opens_one_script_event_at_the_venue(
        db_session, overlay, monkeypatch):
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    before = datetime.now(UTC)
    d = await _run(db_session, venue="theater")

    (ev,) = await _events(db_session)
    assert ev.type == "script"          # 不是 "news" —— 这是全段的关键
    assert ev.payload_json["location_id"] == "theater"
    assert ev.payload_json["debate_id"] == d.id
    assert "剧院" in ev.title and "猫和狗谁更好" in ev.title
    assert "安" in ev.description and "波" in ev.description
    window = (ev.ends_at.replace(tzinfo=UTC) if ev.ends_at.tzinfo is None
              else ev.ends_at) - before
    assert 0 < window.total_seconds() <= settings.debate_vote_window_min * 60 + 5


@pytest.mark.anyio
async def test_the_event_is_visible_to_the_crowd_puller(
        db_session, overlay, monkeypatch):
    """这条就是 design §② 那个「公开课的人流拉力从未生效过」缺陷的反面证明。"""
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _run(db_session, venue="theater")

    (ev,) = await _events(db_session)
    assert ev.type in crowd_service._EVENT_TYPES_WITH_CROWD
    assert crowd_service.active_event_location([_to_dict(ev)]) == "theater"


@pytest.mark.anyio
async def test_gate_on_without_a_venue_creates_nothing(db_session, monkeypatch):
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    await _run(db_session)
    assert await _events(db_session) == []


@pytest.mark.anyio
async def test_gate_on_legacy_row_without_the_declaration_creates_nothing(
        db_session, overlay, monkeypatch):
    """存量 theater 行没有 capabilities 键 —— 未回填就开闸不出事,只是不建事件。"""
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    overlay("theater", THEATER)
    await _run(db_session, venue="theater")
    assert await _events(db_session) == []


@pytest.mark.anyio
async def test_an_aborted_debate_leaves_no_ghost_event(
        db_session, overlay, monkeypatch):
    """LLM 空辩词 → _auto_draw_refund 当场 settled。事件必须一条都没有,否则就是
    一条指着死辩论、还要拉三倍人流一小时的幽灵。"""
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    d = await _run(db_session, venue="theater", text="")
    assert d.status == "settled" and d.winner == "draw"
    assert await _events(db_session) == []


@pytest.mark.anyio
async def test_a_long_topic_does_not_overflow_the_title_column(
        db_session, overlay, monkeypatch):
    """WorldEvent.title 是 String(200),Debate.topic 是 String(300) —— 真 PG 上不
    截断就是一条 StringDataRightTruncation。"""
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _run(db_session, topic="长" * 300, venue="theater")
    (ev,) = await _events(db_session)
    assert len(ev.title) <= 200


@pytest.mark.anyio
async def test_a_broken_event_write_never_drags_the_debate_into_a_refund(
        db_session, overlay, monkeypatch):
    """世界事件是叙事装饰;一场已经跑完六轮的辩论不能因为它退款。"""
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})

    async def _boom(_debate_id):
        raise RuntimeError("venue lookup exploded")

    monkeypatch.setattr(ds, "_debate_venue", _boom)
    d = await _run(db_session, venue="theater")
    assert d.status == "voting" and d.winner is None
    assert await _events(db_session) == []


# ── civic 接线 ────────────────────────────────────────────────────────

def _lecture_pool(db):
    def _res(slug, name, sbti, **kw):
        d = dict(slug=slug, name=name, district="town_hall", status="idle",
                 resident_type="npc", creator_id="sys", tile_x=119, tile_y=53,
                 is_autonomous=True,
                 meta_json={"sbti": {"dimensions": sbti}})
        d.update(kw)
        return Resident(**d)

    db.add_all([
        _res("opt", "乐观者", {"So1": "H", "A1": "H"}),
        _res("skept", "怀疑者", {"So1": "H", "A1": "L"}),
    ])


@pytest.mark.anyio
async def test_lecture_debate_gets_the_stage_venue_when_the_gate_is_on(
        db_session, overlay, monkeypatch):
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    slug = overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    _lecture_pool(db_session)
    await db_session.commit()

    event = {"title": "小镇的来路的公开课", "payload_json": {"duty": "lecturer"}}
    assert await civic_service.maybe_spawn_lecture_debate(db_session, event) is True

    from app.models.debate import Debate
    d = (await db_session.execute(select(Debate))).scalars().one()
    assert await ds._debate_venue(d.id) == slug


@pytest.mark.anyio
async def test_lecture_debate_gets_no_venue_when_the_gate_is_off(
        db_session, overlay, monkeypatch):
    monkeypatch.setattr(settings, "stage_event_enabled", False)
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    _lecture_pool(db_session)
    await db_session.commit()

    event = {"title": "小镇的来路的公开课", "payload_json": {"duty": "lecturer"}}
    assert await civic_service.maybe_spawn_lecture_debate(db_session, event) is True

    from app.models.debate import Debate
    d = (await db_session.execute(select(Debate))).scalars().one()
    assert await ds._debate_venue(d.id) is None


def test_civic_does_not_hardcode_the_theater_slug():
    """剧院是公投建的动态行,slug 是数据不是代码常量 —— 走能力反查。"""
    src = (Path(__file__).resolve().parents[1]
           / "app" / "services" / "civic_service.py").read_text(encoding="utf-8")
    body = src.split("async def maybe_spawn_lecture_debate", 1)[1].split(
        "\n# ── helper", 1)[0]
    assert '"theater"' not in body and "'theater'" not in body


def test_debate_service_still_has_no_location_column_semantics():
    """零迁移:场地只走 Redis 与 payload,不进 debates 表。"""
    text = DEBATE_SRC.read_text(encoding="utf-8")
    assert "mapped_column" not in text and "add_column" not in text


def test_action_type_enum_is_untouched():
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
```

先跑一次拿红。预期失败形态：`test_gate_on_opens_one_script_event_at_the_venue` 抛 `ValueError: not enough values to unpack (expected 1, got 0)`（事件一条都没建）；`test_lecture_debate_gets_the_stage_venue_when_the_gate_is_on` 断言 `None == "theater"` 失败。

#### 实现

**改动 1** —— /Volumes/data/dev/simverse-world/backend/app/services/debate_service.py

锚点：`run_live` 结尾（debate_service.py:205-208）。`grep -n 'debate_voting_open' app/services/debate_service.py` 应只有一处。

before：
```python
    debate.status = "voting"
    await db.commit()
    await manager.broadcast({"type": "debate_voting_open", "debate_id": debate.id})
    return debate
```
after：
```python
    debate.status = "voting"
    await db.commit()
    # P2 #7:开票的同一刻在场地挂一条 type="script" 的世界事件(见下)。
    await _maybe_open_stage_event(db, debate, res_a, res_b)
    await manager.broadcast({"type": "debate_voting_open", "debate_id": debate.id})
    return debate
```

**改动 2** —— 同文件，紧接 `run_live` 之后、`_auto_draw_refund`（:211）之前插入整块：

```python
async def _maybe_open_stage_event(db, debate: Debate, res_a, res_b) -> None:
    """把这场辩论挂成场地上的一条 ``type="script"`` 世界事件(STAGE_EVENT_ENABLED)。

    **为什么是 "script" 而不是 "news"**:``crowd_service._EVENT_TYPES_WITH_CROWD``
    是 ``("festival", "script")``(crowd_service.py:28),"news" 不在里面 —— 学院
    公开课十五天零到访正是栽在这上面。用 "script" 零改动即获得 ``festival_draw_target``
    的 ×``realism_festival_weight`` 人流拉力(crowd_service.py:207-219),并自动进入
    所有居民的 decide prompt(``get_active_events_cached`` → ``ctx.world_events``)。
    ``WorldEvent.type`` 是 ``String(20)`` 自由文本(models/world_event.py:26),不是
    闭集;``lab/protocol.py:73`` 的那个闭集是 lab 事件总线,与 world_events 表无关。

    **为什么挂在开票这一刻,而不是设计写的「进入 live 之后」**:live 段任何一轮 LLM
    失败都会走 ``_auto_draw_refund`` 并 return(见上),那条路上这场辩论当场 settled
    —— 在进入 live 时建事件就会留下一条指着死辩论、还要拉三倍人流一小时的幽灵事件,
    还得再写一段补偿清理。挂在开票这一刻在同一条成功路径上,墙钟只差六轮 LLM(数十
    秒),而 ``ends_at`` 取 ``debate_vote_window_min`` 本来就该从「投票开始」量起 ——
    这也正是 ``drive_due_debates`` 打 ``_mark_voting_since`` 的同一刻(:337-341)。

    全程 best-effort:世界事件是叙事装饰,建不出来绝不能把一场已经跑完六轮的辩论
    拖进 auto-draw 退款。
    """
    from app.config import settings
    if not settings.stage_event_enabled:
        return
    try:
        venue = await _debate_venue(debate.id)
        if not venue:
            return
        from app.agent.map_data import get_location_by_id
        from app.models.world_event import WorldEvent
        place = (get_location_by_id(venue) or {}).get("name") or "剧院"
        a_name = res_a.name if res_a else "正方"
        b_name = res_b.name if res_b else "反方"
        now = datetime.now(UTC)
        db.add(WorldEvent(
            type="script",
            # WorldEvent.title 是 String(200),Debate.topic 是 String(300)——
            # 真 PG 上不截断就是一条 StringDataRightTruncation。
            title=f"{place}辩论 · {debate.topic}"[:200],
            description=(f"{a_name}与{b_name}正在{place}辩论「{debate.topic}」，"
                         f"居民们可以去{place}旁听、议论。"),
            payload_json={"location_id": venue, "debate_id": debate.id},
            starts_at=now,
            ends_at=now + timedelta(minutes=settings.debate_vote_window_min),
            # 与 script_service.fire_due_scripts(:79-86)同姿势:realism 开时留给
            # event_cron 的 flip 去激活,好让 start 相位的广播与集体记忆照常发生。
            is_active=(False if settings.realism_enabled else True),
        ))
        await db.commit()
    except Exception:
        logger.warning("stage event for debate %s failed", debate.id, exc_info=True)
        # 半截写入不回滚的话,PendingRollbackError 会顺着这条共享 session 传染给
        # drive_due_debates 的下一场辩论(同 event_cron.py:69-77 踩过的坑)。
        try:
            await db.rollback()
        except Exception:
            logger.warning("stage event rollback itself failed", exc_info=True)
```

**改动 3** —— /Volumes/data/dev/simverse-world/backend/app/services/civic_service.py

锚点：`maybe_spawn_lecture_debate` 结尾（civic_service.py:979-981）。

before：
```python
        from app.services.debate_service import create_debate
        await create_debate(db, f"关于「{topic}」的争论", a.slug, b.slug)
        return True
```
after：
```python
        from app.services.debate_service import create_debate
        # P2 #7:把辩论安排到声明了 stage 能力的地点(今天全镇唯一的是剧院)。
        # 闸关 = 不传场地 = 逐字节旧行为。地点由能力反查得出,这里**不硬编码**
        # "theater" —— 剧院是公投建的动态行,slug 是数据不是代码常量。
        venue = None
        if settings.stage_event_enabled:
            from app.agent.location_caps import CAP_STAGE
            from app.agent.map_data import capability_locations
            venue = next(iter(capability_locations(CAP_STAGE)), None)
        await create_debate(db, f"关于「{topic}」的争论", a.slug, b.slug, venue=venue)
        return True
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_stage_event_debate.py tests/test_stage_event_venue.py tests/test_debates.py tests/test_debate_driver.py tests/test_m3_civic.py tests/test_crowd.py tests/test_world_events.py tests/test_market_hall_constant.py tests/test_env_example_consistency.py tests/test_lab_building.py -q
```

**验收**：1. 实现前红：`test_gate_on_opens_one_script_event_at_the_venue` 报事件一条都没建（unpack 0 值），`test_lecture_debate_gets_the_stage_venue_when_the_gate_is_on` 断言 `None == 'theater'` 失败。2. 实现后全绿，其中 `tests/test_debates.py` / `tests/test_debate_driver.py` / `tests/test_m3_civic.py` / `tests/test_crowd.py` 零改动全绿。3. 关键实证成对出现：`test_the_event_is_visible_to_the_crowd_puller` 证明 `active_event_location` 看得见（`"script" ∈ _EVENT_TYPES_WITH_CROWD`），`test_an_aborted_debate_leaves_no_ghost_event` 证明失败路径零幽灵事件。4. `grep -n 'theater' app/services/civic_service.py` 只命中 `CIVIC_AGENDA` 的字面量（S7 那处），`maybe_spawn_lecture_debate` 函数体零命中。5. `grep -c 'capability_locations\|nearest_capability_location' app/agent/phases/decide/basic.py app/agent/tick.py` 两处各为 0（P1-S9 守卫未被触碰）。6. `len(list(ActionType)) == 16`。7. `.venv/bin/python -m alembic heads` 与改前逐字相同。

**commit**：

```
feat(debate): 开票同刻挂 type="script" 舞台事件接上 ×3 人流拉力,civic 侧按 stage 能力反查场地
```

### P2-S10 — _work_lecturer 的事件 type 从 "news" 改 "script"（挂闸）+ 冷却查询无条件覆盖两种 type

**Flag / 批次**：沿用 `stage_event_enabled=False`（S8 引入，两份 env 模板均 false）。关 = 事件仍是 `"news"`、逐字节旧行为。冷却查询的放宽是**无条件**的（跨闸正确性修复，两个方向都必须成立）。非迁移批次、非开闸批次。

**为什么**：design_P2.md §②-b：`_work_lecturer`（duty_service.py:405-431）建的是 `type="news"`，payload 里明明写了 `{"location_id": "academy"}`（:426），但 `_EVENT_TYPES_WITH_CROWD`（crowd_service.py:28）不含 `"news"`，于是 `active_event_location` 永远看不到公开课，×3 拉力**一次都没生效过**。改 type 即接上。

**不把 `"news"` 加进那个元组**：那会让 `NEWS_POOL`（event_templates.py:31-36）那 4 条随机新闻也长出人流语义。

**冷却查询必须无条件放宽成 `type IN ('news','script')`**（设计没提，但不改就是硬缺陷）：闸翻开后新课是 `"script"`，窗口里的旧课还是 `"news"`；只查一种，翻闸当天（或翻回去当天）7 天冷却直接失效，讲师每次 WORK 都开一场新课。标题过滤 `%{name}的公开课%` 已把范围收得极窄，不会误伤 `script_service` 的剧本事件。

#### 先写的测试（必须跑出失败）

新建文件：/Volumes/data/dev/simverse-world/backend/tests/test_stage_event_lecture.py

```python
"""P2-S10: 公开课事件的 type 从 "news" 改 "script" —— 修「人流拉力从未生效过」。

本文件的核心是一对对照:同一段代码,闸关时建出来的事件
active_event_location 看不见,闸开时看得见。这就是 design_P2.md §② 那个新发现
缺陷的可执行形态。

第二条是冷却:闸是可以来回翻的,而冷却窗口是 7 天。查询只认一种 type 的话,翻闸
当天(任一方向)冷却当场失效,讲师每次 WORK 都开一场新课。
"""
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agent.actions import ActionType
from app.agent.map_data import LOCATIONS
from app.config import settings
from app.models.resident import Resident
from app.models.world_event import WorldEvent
from app.services import crowd_service, duty_service
from app.services.world_event_service import _to_dict

DUTY_SRC = (Path(__file__).resolve().parents[1]
            / "app" / "services" / "duty_service.py")


def _lecturer() -> Resident:
    return Resident(
        slug="gu", name="顾明远", creator_id="sys", resident_type="npc",
        district="academy", status="idle", tile_x=70, tile_y=56,
        meta_json={"duty": {"key": "lecturer",
                            "perks": {"lecture_cooldown_days": 7}}},
    )


async def _lecture(db) -> Resident:
    r = _lecturer()
    db.add(r)
    await db.commit()
    return r


async def _events(db) -> list[WorldEvent]:
    return (await db.execute(select(WorldEvent))).scalars().all()


# ── 闸关 = 逐字节旧行为 ───────────────────────────────────────────────

@pytest.mark.anyio
async def test_gate_off_still_writes_a_news_event(db_session, monkeypatch):
    monkeypatch.setattr(settings, "stage_event_enabled", False)
    r = await _lecture(db_session)

    line = await duty_service._work_lecturer(db_session, r)

    assert line == "顾明远在学院挂出了公开课的讲题"
    (ev,) = await _events(db_session)
    assert ev.type == "news"
    assert ev.title == "顾明远的公开课"
    assert ev.payload_json == {"location_id": "academy", "duty": "lecturer"}


@pytest.mark.anyio
async def test_gate_off_event_is_invisible_to_the_crowd_puller(
        db_session, monkeypatch):
    """这就是缺陷本身:payload 里写着 academy,拉力却一次都没生效过。"""
    monkeypatch.setattr(settings, "stage_event_enabled", False)
    r = await _lecture(db_session)
    await duty_service._work_lecturer(db_session, r)

    (ev,) = await _events(db_session)
    assert ev.type not in crowd_service._EVENT_TYPES_WITH_CROWD
    assert crowd_service.active_event_location([_to_dict(ev)]) is None


# ── 闸开 ──────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_gate_on_writes_a_script_event_with_the_same_payload(
        db_session, monkeypatch):
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    r = await _lecture(db_session)

    line = await duty_service._work_lecturer(db_session, r)

    assert line == "顾明远在学院挂出了公开课的讲题"   # 叙事一个字不变
    (ev,) = await _events(db_session)
    assert ev.type == "script"
    assert ev.title == "顾明远的公开课"
    assert ev.payload_json == {"location_id": "academy", "duty": "lecturer"}


@pytest.mark.anyio
async def test_gate_on_event_finally_pulls_a_crowd(db_session, monkeypatch):
    """修好之后的正面证明。academy 是静态地点,恒在 LOCATIONS 里。"""
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    assert "academy" in LOCATIONS
    r = await _lecture(db_session)
    await duty_service._work_lecturer(db_session, r)

    (ev,) = await _events(db_session)
    assert ev.type in crowd_service._EVENT_TYPES_WITH_CROWD
    assert crowd_service.active_event_location([_to_dict(ev)]) == "academy"


# ── 冷却跨闸 ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_cooldown_survives_flipping_the_gate_on(db_session, monkeypatch):
    """旧课是 "news",翻闸后查询只认 "script" 的话冷却当场失效。"""
    monkeypatch.setattr(settings, "stage_event_enabled", False)
    r = await _lecture(db_session)
    assert await duty_service._work_lecturer(db_session, r) is not None

    monkeypatch.setattr(settings, "stage_event_enabled", True)
    assert await duty_service._work_lecturer(db_session, r) is None
    assert len(await _events(db_session)) == 1


@pytest.mark.anyio
async def test_cooldown_survives_flipping_the_gate_back_off(
        db_session, monkeypatch):
    """回滚方向同样成立 —— 闸是可以随时翻回去的。"""
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    r = await _lecture(db_session)
    assert await duty_service._work_lecturer(db_session, r) is not None

    monkeypatch.setattr(settings, "stage_event_enabled", False)
    assert await duty_service._work_lecturer(db_session, r) is None
    assert len(await _events(db_session)) == 1


def test_the_cooldown_type_set_is_declared_once_and_covers_both():
    assert duty_service._LECTURE_EVENT_TYPES == ("news", "script")


# ── 边界守卫 ──────────────────────────────────────────────────────────

def test_news_never_gains_crowd_semantics():
    """不许图省事把 "news" 塞进那个元组:NEWS_POOL 的四条随机新闻会跟着长出人流
    语义,全镇往一条「今天风很大」的新闻里跑。"""
    assert crowd_service._EVENT_TYPES_WITH_CROWD == ("festival", "script")


def test_no_bare_news_literal_left_in_the_lecturer_handler():
    """type 与冷却判据必须来自同一处声明,不得各自手写字面量。"""
    text = DUTY_SRC.read_text(encoding="utf-8")
    body = text.split("async def _work_lecturer", 1)[1].split(
        "async def _work_researcher", 1)[0]
    offenders = [line.strip() for line in body.splitlines()
                 if not line.lstrip().startswith("#") and '"news"' in line
                 and "_LECTURE_EVENT_TYPES" not in line
                 and "stage_event_enabled" not in line]
    assert not offenders, offenders


def test_action_type_enum_is_untouched():
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
```

先跑一次拿红。预期失败形态：`test_the_cooldown_type_set_is_declared_once_and_covers_both` 抛 `AttributeError: module 'app.services.duty_service' has no attribute '_LECTURE_EVENT_TYPES'`；`test_gate_on_writes_a_script_event_with_the_same_payload` 断言 `"news" == "script"` 失败；`test_cooldown_survives_flipping_the_gate_back_off` 报事件数是 2。

#### 实现

改文件：/Volumes/data/dev/simverse-world/backend/app/services/duty_service.py（两处）

**改动 1 —— 模块常量**。锚点：`DUTY_WORK_COOLDOWN_HOURS = 20`（duty_service.py:59）那一行之后。

before：
```python
DUTY_WORK_COOLDOWN_HOURS = 20  # ≈ once per game day, tolerant of schedule drift
```
after：
```python
DUTY_WORK_COOLDOWN_HOURS = 20  # ≈ once per game day, tolerant of schedule drift

#: 公开课世界事件的历史 type 与现 type。冷却判据必须**两种都查**:STAGE_EVENT_ENABLED
#: 翻开之后新课是 "script",而 7 天窗口里的旧课还是 "news";只认一种的话,翻闸当天
#: (任一方向)冷却直接失效,讲师每次 WORK 都开一场新课。
_LECTURE_EVENT_TYPES = ("news", "script")
```

**改动 2 —— `_work_lecturer` 整体替换**（duty_service.py:405-431，从 `async def _work_lecturer` 到 `return f"{resident.name}在学院挂出了公开课的讲题"`）：

```python
async def _work_lecturer(db, resident) -> str | None:
    """顾明远:每周在学院开一场公开课(WorldEvent,event_cron 按窗口激活,
    活动期间注入所有居民的决策与对话 prompt,吸引大家去学院)。

    P2 #8(STAGE_EVENT_ENABLED):事件 type 从 "news" 改成 "script"。
    ``crowd_service._EVENT_TYPES_WITH_CROWD`` 是 ``("festival", "script")``
    (crowd_service.py:28),"news" 不在里面 —— 于是 ``active_event_location``
    (:65-76)永远看不到公开课,``festival_draw_target`` 的
    ×``realism_festival_weight`` 拉力(:207-219)对公开课**一次都没生效过**。
    payload 里那句 ``location_id: academy`` 写了三年,一直是死信息。

    不把 "news" 加进那个元组:那会让 ``event_templates.NEWS_POOL`` 的 4 条随机新闻
    也长出人流语义,全镇往一条「今天风很大」里跑。

    闸关 = 逐字节旧行为:type 仍是 "news"、标题/描述/payload/返回文案一字不变。
    """
    from app.models.world_event import WorldEvent
    from app.config import settings

    cooldown_days = int(perk(resident, "lecture_cooldown_days", 7))
    since = datetime.now(UTC) - timedelta(days=cooldown_days)
    recent = (await db.execute(
        select(WorldEvent.id).where(
            # 两种 type 都查(见 _LECTURE_EVENT_TYPES 的说明)。标题过滤已经把范围
            # 收得极窄("%{name}的公开课%"),不会误伤 script_service 的剧本事件。
            WorldEvent.type.in_(_LECTURE_EVENT_TYPES),
            WorldEvent.title.like(f"%{resident.name}的公开课%"),
            WorldEvent.created_at >= since,
        ).limit(1)
    )).scalar_one_or_none()
    if recent is not None:
        return None
    now = datetime.now(UTC)
    db.add(WorldEvent(
        type=("script" if settings.stage_event_enabled else "news"),
        title=f"{resident.name}的公开课",
        description=f"{resident.name}今天在学院开公开课,讲小镇的历史与来路。居民们可以去学院旁听。",
        payload_json={"location_id": "academy", "duty": "lecturer"},
        starts_at=now, ends_at=now + timedelta(hours=6), is_active=False,
    ))
    await db.commit()
    await _feed(resident.slug, "duty_output", {"duty": "lecturer"})
    return f"{resident.name}在学院挂出了公开课的讲题"
```

本 step 不改任何其它文件：`civic_service.maybe_spawn_lecture_debate` 的判据是 `payload.get("duty") == "lecturer"`（civic_service.py:947-949），与 type 无关，公开课→辩论那条链一个字不用动。

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_stage_event_lecture.py tests/test_duty_service.py tests/test_m3_civic.py tests/test_crowd.py tests/test_world_events.py tests/test_event_memory_tier.py tests/test_event_tier_marker.py tests/test_pool_world_event_lane.py tests/test_office_duty_boundary.py tests/test_env_example_consistency.py tests/test_lab_building.py -q
```

**验收**：1. 实现前红：`test_the_cooldown_type_set_is_declared_once_and_covers_both` 抛 `AttributeError: ... '_LECTURE_EVENT_TYPES'`，`test_gate_on_writes_a_script_event_with_the_same_payload` 断言 `'news' == 'script'` 失败，`test_cooldown_survives_flipping_the_gate_back_off` 报事件数 2。2. 实现后全绿，其中 `tests/test_duty_service.py::test_on_work_lecturer_schedules_event`、`tests/test_m3_civic.py::test_lecture_end_spawns_debate`、`tests/test_event_memory_tier.py`、`tests/test_pool_world_event_lane.py` 零改动全绿。3. 缺陷与修复成对留证：`test_gate_off_event_is_invisible_to_the_crowd_puller`（闸关时 `active_event_location` 返 None）与 `test_gate_on_event_finally_pulls_a_crowd`（闸开时返 `"academy"`）。4. `grep -n '_EVENT_TYPES_WITH_CROWD = ' app/services/crowd_service.py` 输出仍是 `("festival", "script")` —— 没有图省事把 `"news"` 塞进去。5. `git diff app/services/duty_service.py | grep -c 'location_id\|academy\|公开课的讲题'` 输出 0（payload 与叙事文案一字未改）。6. `len(list(ActionType)) == 16`。7. `.venv/bin/python -m alembic heads` 与改前逐字相同（零迁移）。8. **本组收口**：全量默认门 `.venv/bin/python -m pytest -q 2>&1 | tail -3` 的失败集严格等于 54 基线（49 lab + 5 postpone），零新增——用 `git stash` 取改前基线数字逐字对比后 `git stash pop`。

**commit**：

```
fix(duty): 公开课事件 type news→script 接上从未生效过的 ×3 人流拉力,冷却判据跨 type 覆盖
```

## P2 剧院侧第二组：人流拉力（design_P2.md 批次表 #9 + §③ 路 B）—— bite-sized TDD 执行计划

<details><summary>依赖边 / 批次归属 / 与既有守卫的冲突面</summary>

依赖边(硬前置)：P1-S1 登记 CAP_STAGE(civic_grantable=True/unlocks=()/category=None，由 P2-S1 notes 写明，本段只引用)、P1-S2 location_capabilities、P1-S5 capability_location_at。#7 负责 theater 声明 capabilities={"stage":{}} 与 type="script" 事件；未落地时本段全链 inert(stage_event_venue 恒 None)，不抛。#8(_work_lecturer news→script)缺陷已回源码核实属实(duty_service.py:423 写 type="news"，crowd_service.py:28 的 _EVENT_TYPES_WITH_CROWD 不含 news)，但它是批次表独立一行，本段不做、不同批，只在 P2-S11 用一条测试把现状钉死。P3-c 不构成依赖：本机实测 get_valid_target_tile("theater")=entrance(172,45) reachable=True、center(175,45) reachable=False，S11 的可达门今天不拦剧院。
批次：S11/S12 纯代码、零生产调用方、无闸；S13 新增 stage_event_crowd_enabled=False 并双写两份 .env.example。三步无迁移、无开闸、无数据回填。
串并行：严格 S11→S12→S13（S12 用 S11 的 _active_stage_event_key/_stable_rank；S13 用两者）。
守卫冲突面：①P1-S9 对 decide/basic.py 全文断言禁 capability_locations/nearest_capability_location/裸 "market_hall"——分支一律经 crowd_service 包装函数，acceptance 用 grep 钉死；②tests/test_lab_building.py 的 len==16，三步各带一条复述，零新增 ActionType；③STAGE_EVENT_CROWD_ENABLED 不在 GOVERNANCE_PREFIXES("CIVIC_"/"REP_"/"POLIS_OFFICE_")内，却落在 #7 可能注册的 STAGE_EVENT_ 前缀 parity 内→必须双写，本段自带 per-key 兜底断言；④_stable_market_rank 改成同串委托，用 sha256 逐字节对拍证零行为差，stage 用独立 cache+lock，不碰 market 单飞锁；⑤0809 死锁排序由源码文本断言钉死。
未闭合(交接)：本分支刻意不落粘性行程（不设 market_trip_event_id——tick.py:155-162 把 kind/location 写死成 market_day/market_hall），故每 tick 重算目的地且每 tick 吃一格日行动 cap，与既有 festival 抽签同形状，开闸后 burn-in 必盯。

</details>

### P2-S11 — crowd_service 加 stage 事件场地解析（能力门 + 可达性自保）+ 稳定排序 helper 收敛

**Flag / 批次**：无（纯函数 + 同串重构，零生产调用方；闸只加在调用点，见 P2-S13）。非迁移批次。

**为什么**：#9 的前半：把「哪场事件算演出、演在哪」落成纯函数。场地判据用 CAP_STAGE 而不是 slug 字面量——这正是 §③ 路 B「地点吸引力与在场人数解耦」的机器表述：拉力来自事件+能力声明，与在场人数无关，actions.py 一个字不改。可达性门是对 P3-c 的自保：剧院 center(175,45) 实测 walkable=True/reachable=False，若目的地落到孤岛，名单里的人会每 tick 走 find_path 恒返 None 的路线并烧光日行动 cap。_stable_rank 收敛是同串重构，避免哈希拼串出现第二份。零生产调用方、不挂闸。

#### 先写的测试（必须跑出失败）

新建文件：/Volumes/data/dev/simverse-world/backend/tests/test_stage_event_venue.py

```python
"""P2-S11: stage 事件场地解析 —— 能力门 + 可达性自保 + 稳定排序 helper 收敛。

三条判据各防一类事故:
  · 能力门:场地资格来自地点自己的 capabilities 声明(CAP_STAGE),不是 slug 字面量。
    这是 §③ 路 B 的机器表述 —— 拉力与在场人数解耦,actions.py:80-86 一个字不改。
  · 可达性门:get_valid_target_tile 返回的 tile 必须在 get_reachable_tiles() 里。
    pathfinder._get_forced_walkable(:60-68) 无边界检查地把每个地点的 entrance/center
    强标 walkable,所以 walkable 会自证成功(实测 theater center(175,45)
    walkable=True / reachable=False)。
  · 同串收敛:_stable_market_rank 委托给 _stable_rank 后必须逐字节等价。
"""
import hashlib

import pytest

from app.agent.actions import ActionType
from app.agent.location_caps import CAP_STAGE
from app.agent.map_data import LOCATIONS, get_location_id_at
from app.agent import pathfinder
from app.services import crowd_service

# 生产 dynamic_locations 里 theater 那行的 data_json(2026-08 公投建,active=t)。
# capabilities 由调用方按场景决定加不加 —— 存量行今天**没有**这个键。
THEATER = {
    "name": "剧院", "type": "public", "role": "culture",
    "bounds": (172, 40, 178, 50), "center": (175, 45), "entrance": (172, 45),
    "description": "小镇剧院:辩论、公开课、戏与人群",
    "boosted_actions": ["OBSERVE", "CHAT_RESIDENT"],
}
#: entrance 落在孤岛上的假场地(x=175 在 theater bounds 内但不与镇区连通)。
ISLAND_STAGE = {**THEATER, "name": "孤岛戏台", "entrance": (175, 45)}


def _script(location_id, *, etype="script", eid="stage-1"):
    return {
        "id": eid, "type": etype, "title": "一场辩论",
        "starts_at": "2026-08-17T10:00:00+00:00",
        "ends_at": "2026-08-17T11:00:00+00:00",
        "payload_json": {"location_id": location_id, "debate_id": "d1"},
    }


SEASON_SCRIPT = {
    "id": "s1", "type": "script", "title": "剧本 · 第1幕",
    "starts_at": "", "ends_at": "",
    "payload_json": {"season_id": "se1", "act": 1},
}
MARKET_DAY = {
    "id": "market-1", "type": "festival", "title": "集市日",
    "starts_at": "2026-08-13T00:00:00+00:00",
    "ends_at": "2026-08-14T00:00:00+00:00",
    "payload_json": {"market_day": True, "location_id": "market_hall"},
}
LECTURE_NEWS = {
    "id": "n1", "type": "news", "title": "顾明远的公开课",
    "starts_at": "", "ends_at": "",
    "payload_json": {"location_id": "academy", "duty": "lecturer"},
}


@pytest.fixture
def overlay():
    """模拟 load_dynamic_locations 的合入:追加到 LOCATIONS 尾部,再还原。

    合入/摘除都要重置 pathfinder 缓存 —— get_walkable_tiles 会 force-add 每个地点的
    entrance/center(pathfinder.py:94-96),LOCATIONS 变了缓存就过期。
    """
    added: list[str] = []

    def _merge(slug: str, data: dict, capabilities=None) -> str:
        assert slug not in LOCATIONS, slug
        row = dict(data)
        if capabilities is not None:
            row["capabilities"] = capabilities
        LOCATIONS[slug] = row
        added.append(slug)
        pathfinder.reset_walkable_cache()
        return slug

    yield _merge
    for slug in added:
        LOCATIONS.pop(slug, None)
    pathfinder.reset_walkable_cache()


# ── 稳定排序 helper 的同串收敛 ────────────────────────────────────────

def test_stable_rank_is_plain_sha256_over_a_unit_separated_material():
    assert crowd_service._stable_rank(("a", "b", "c", "d"), "r1") == \
        hashlib.sha256("a\x1fb\x1fc\x1fd\x1fr1".encode("utf-8")).digest()


def test_market_rank_is_byte_identical_after_delegating():
    """集市 cohort 的选人结果不得因为本次收敛发生一位比特的变化。"""
    key = ("market-1", "2026-08-13T00:00:00+00:00", "2026-08-14T00:00:00+00:00")
    for rid in ("r1", "r2", "骆小舟"):
        assert crowd_service._stable_market_rank(key, rid) == \
            hashlib.sha256("\x1f".join((*key, rid)).encode("utf-8")).digest()


# ── 能力门 ────────────────────────────────────────────────────────────

def test_no_events_no_venue():
    assert crowd_service.stage_event_venue([]) is None
    assert crowd_service.stage_event_venue(None) is None
    assert crowd_service.active_stage_event_id([]) is None


def test_a_script_event_at_a_stage_capable_venue_is_recognized(overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    events = [_script("theater")]
    assert crowd_service.stage_event_venue(events) == "theater"
    assert crowd_service.active_stage_event_id(events) == "stage-1"


def test_a_venue_without_the_stage_declaration_is_inert(overlay):
    """存量 dynamic_locations 行没有 capabilities 键 —— #7 的声明未落地时本段整段
    不生效,而不是乱拉人。缺省安全。"""
    overlay("theater", THEATER)
    assert crowd_service.stage_event_venue([_script("theater")]) is None


def test_an_unknown_location_id_is_ignored(overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    assert crowd_service.stage_event_venue([_script("nowhere_at_all")]) is None
    assert crowd_service.stage_event_venue([_script(None)]) is None
    assert crowd_service.stage_event_venue([_script(123)]) is None


def test_market_day_is_not_a_stage_event(overlay):
    """market_hall 不声明 stage —— 集市 cohort 与观众 cohort 不得互相抢人。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    assert crowd_service.stage_event_venue([MARKET_DAY]) is None


def test_season_script_events_carry_no_location_and_stay_inert(overlay):
    """script_service.py:79-87 建的季节剧本 payload 只有 season_id/act。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    assert crowd_service.stage_event_venue([SEASON_SCRIPT]) is None


def test_the_lecture_news_event_is_not_picked_up(overlay):
    """公开课今天是 type=\"news\"(duty_service.py:423),而 _EVENT_TYPES_WITH_CROWD
    只有 (festival, script) —— 这就是「公开课的人流拉力从未生效过」。本段**不修**它
    (归 design_P2.md 批次表 #8),这条只是把现状钉死,防止有人以为 #9 顺带治好了。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    assert crowd_service.stage_event_venue([LECTURE_NEWS]) is None
    assert "news" not in crowd_service._EVENT_TYPES_WITH_CROWD


def test_a_festival_at_a_stage_venue_also_counts(overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    assert crowd_service.stage_event_venue(
        [_script("theater", etype="festival")]) == "theater"


# ── 可达性自保 ────────────────────────────────────────────────────────

def test_an_island_venue_is_refused(overlay):
    """entrance 不与镇区连通 → 不认这个场地。否则名单里的人每 tick 走一条
    find_path 恒返 None 的路线,arrivals 永远 0 而日行动 cap 被烧光。"""
    overlay("island_stage", ISLAND_STAGE, capabilities={CAP_STAGE: {}})
    assert crowd_service.stage_event_venue([_script("island_stage")]) is None


def test_the_production_theater_entrance_is_reachable_today(overlay):
    """本段**不依赖** P3-c 的 bounds 迁移:生产 theater 的 entrance(172,45) 今天就是
    连通的,只有 center(175,45) 是孤岛,而 get_valid_target_tile 有 entrance 就永不取
    center(map_data.py:453)。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    reachable = pathfinder.get_reachable_tiles()
    assert (172, 45) in reachable
    assert (175, 45) not in reachable
    assert crowd_service.stage_event_venue([_script("theater")]) == "theater"


def test_a_venue_without_a_target_tile_is_refused(overlay):
    """data_json 写了 \"entrance\": null 时 get_valid_target_tile 返 None,不回退
    center(map_data.py:453 是 .get 的默认值形式)。"""
    overlay("no_door", {**THEATER, "entrance": None, "center": None},
            capabilities={CAP_STAGE: {}})
    assert crowd_service.stage_event_venue([_script("no_door")]) is None


# ── 站位反查穿透遮蔽 ─────────────────────────────────────────────────

def test_stage_venue_at_pierces_the_outdoor_mask(overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    # 遮蔽是真的:首命中返回的是 outdoor 街区,不是剧院。
    assert get_location_id_at(174, 45) == "east_gardens"
    assert get_location_id_at(172, 45) == "east_gardens"
    # 能力反查穿透遮蔽。
    assert crowd_service.stage_venue_at(174, 45) == "theater"
    assert crowd_service.stage_venue_at(172, 45) == "theater"
    # 镇中心不在任何 stage 地点里。
    assert crowd_service.stage_venue_at(75, 56) is None


def test_stage_venue_at_is_none_without_the_declaration(overlay):
    overlay("theater", THEATER)
    assert crowd_service.stage_venue_at(174, 45) is None


def test_action_type_enum_is_untouched():
    """P2 全段零新增 ActionType(design_P2.md §「为什么不新增 ActionType」)。"""
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
```

先跑一次拿红。预期失败形态：收集期通过，用例期多条 `AttributeError: module 'app.services.crowd_service' has no attribute '_stable_rank'` / `'stage_event_venue'` / `'stage_venue_at'` / `'active_stage_event_id'`。

#### 实现

改文件：/Volumes/data/dev/simverse-world/backend/app/services/crowd_service.py（两处）

**改动 1 —— 顶部 import**。锚点：crowd_service.py:24-26（`grep -c 'from app.services.event_location import resolve_event_location_id' app/services/crowd_service.py` 应为 1）。

before：
```python
from app.config import settings
from app.agent.map_data import LOCATIONS, get_location_id_at
from app.services.event_location import resolve_event_location_id
```
after：
```python
from app.config import settings
from app.agent.location_caps import CAP_STAGE
from app.agent.map_data import (
    LOCATIONS, get_location_id_at, location_capabilities,
)
from app.services.event_location import resolve_event_location_id
```
import 安全性核对：`app/agent/location_caps.py` 不 import 任何 app 模块（P1-S1 的 `test_module_imports_nothing_from_app` 守着）；`location_capabilities` 是 P1-S2 加在 map_data 上的纯查询；`app/agent/pathfinder.py` 只 import `map_data` 与 `world_geometry`，不反向 import services —— 下面对它用惰性 import，与本模块既有风格一致。

**改动 2 —— _stable_market_rank 收敛 + stage 解析块**。锚点：crowd_service.py:102-104。

before：
```python
def _stable_market_rank(event_key: tuple[str, str, str], resident_id: str) -> bytes:
    material = "\x1f".join((*event_key, resident_id)).encode("utf-8")
    return hashlib.sha256(material).digest()
```
after：
```python
def _stable_rank(parts: tuple[str, ...], resident_id: str) -> bytes:
    """每个 (事件, 居民) 对的确定性排序材料。

    集市与舞台两条 cohort 共用同一条哈希规则:同一场事件在任何进程、任何重启后都选
    出同一批人(名单不能随 PYTHONHASHSEED 漂),这是 cohort 能被缓存与复算的前提。
    """
    material = "\x1f".join((*parts, resident_id)).encode("utf-8")
    return hashlib.sha256(material).digest()


def _stable_market_rank(event_key: tuple[str, str, str], resident_id: str) -> bytes:
    return _stable_rank(event_key, resident_id)


# ── P2 #9 舞台事件 (stage event) ──────────────────────────────────────
# 「哪场事件算演出」= 事件类型 ∈ _EVENT_TYPES_WITH_CROWD;「演在哪」= 事件 payload
# 指的那栋楼自己声明了 stage 能力。场地资格**不看在场人数、也不看 slug 字面量** ——
# 这正是 design_P2.md §③ 路 B「地点吸引力与在场人数解耦」的机器表述:拉力全部来自
# 事件 + 能力声明,actions.py:80-86 的 CHAT_RESIDENT 判据一个字不改。


def _stage_venue_is_reachable(venue: str) -> bool:
    """该场地的目的地 tile 是否与镇区连通。

    必须用 get_reachable_tiles 而不是 get_walkable_tiles:后者被
    pathfinder._get_forced_walkable(:60-68)无边界检查地塞进每个地点的 entrance 与
    center,会自证成功(实测 theater center(175,45) walkable=True / reachable=False)。
    孤岛场地一旦被认下,名单里的人会每 tick 走一条 find_path 恒返 None 的路线 ——
    arrivals 永远 0,而每 tick 照吃一格日行动 cap(tick.py:108-117)。

    fail-**closed**:探测异常时返回 False(不认场地、不导流)。宁可少一场戏,也不能把
    人往走不到的地方赶。剧院 bounds 越界的修复归 P3-c(独立迁移批次),本函数是它落地
    前的自保,不是它的替代品。
    """
    from app.agent.map_data import get_valid_target_tile
    from app.agent.pathfinder import get_reachable_tiles
    try:
        tile = get_valid_target_tile(venue)
        if not tile:
            return False
        return (int(tile[0]), int(tile[1])) in get_reachable_tiles()
    except Exception:
        logger.warning("stage venue reachability probe failed: %s", venue,
                       exc_info=True)
        return False


def _active_stage_event_key(world_events) -> tuple[str, str, str, str] | None:
    """活跃舞台事件的稳定缓存/选人键 (marker, starts, ends, venue)。

    venue 进键:同一栋楼换一场戏要重开名单,同一场戏挪了地方也要重开。
    """
    for event in world_events or []:
        if event.get("type") not in _EVENT_TYPES_WITH_CROWD:
            continue
        venue = resolve_event_location_id(event.get("payload_json"))
        if not isinstance(venue, str) or venue not in LOCATIONS:
            continue
        if CAP_STAGE not in location_capabilities(venue):
            continue
        if not _stage_venue_is_reachable(venue):
            logger.debug("stage event venue is not reachable, ignored: %s", venue)
            continue
        # id 有就以 id 为准;starts/ends 区分畸形或合成夹具,并让改期的同一行成为新名单。
        marker = event.get("id") or event.get("title") or "stage_event"
        return (str(marker), str(event.get("starts_at") or ""),
                str(event.get("ends_at") or ""), venue)
    return None


def stage_event_venue(world_events) -> str | None:
    """正在演出的场地 id;没有合格演出则 None。纯函数、零查询。"""
    key = _active_stage_event_key(world_events)
    return key[3] if key is not None else None


def active_stage_event_id(world_events) -> str | None:
    """该场演出的公开身份(用于日志与归因)。"""
    key = _active_stage_event_key(world_events)
    return key[0] if key is not None else None


def stage_venue_at(x: int, y: int) -> str | None:
    """居民此刻脚下、且声明了 stage 能力的地点 id;不在任何场地里则 None。

    用 capability_location_at 而**不是** get_location_id_at:后者首命中即返
    (map_data.py:243-249),命中序 = dict 插入序 = 静态在前、动态追加在尾(:386),而
    theater(172,40,178,50) 完全落在 outdoor 街区 east_gardens(140,35,179,58) 内部 ——
    实测 get_location_id_at(174,45) 返 "east_gardens"。照它写,人站在剧院正中也判不出
    「已在场」,于是每 tick 都会被再拉一次。
    """
    from app.agent.map_data import capability_location_at
    return capability_location_at(x or 0, y or 0, CAP_STAGE)
```

本 step 不改任何其它文件；`_EVENT_TYPES_WITH_CROWD`（:28）一个字不动 —— 把 `"news"` 加进去是 design_P2.md 批次表 #8 的事，且设计已否决（会让 NEWS_POOL 那 4 条随机新闻也产生人流语义）。

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_stage_event_venue.py tests/test_crowd.py tests/test_caravan_market_visitors.py tests/test_caravan_lifecycle.py tests/test_capability_location_at.py tests/test_lab_building.py -q
```

**验收**：1. 实现前红：多条 `AttributeError: module 'app.services.crowd_service' has no attribute '_stable_rank'/'stage_event_venue'/'stage_venue_at'/'active_stage_event_id'`。2. 实现后全绿，其中 `tests/test_crowd.py` 与 `tests/test_caravan_market_visitors.py`（集市 cohort 选人）零改动全绿 —— `_stable_market_rank` 的收敛由 `test_market_rank_is_byte_identical_after_delegating` 逐字节对拍。3. 本 step 零生产调用方：`grep -rn 'stage_event_venue\|stage_venue_at\|active_stage_event_id\|_stable_rank' app/ | grep -v 'app/services/crowd_service.py'` 输出为空。4. `test_the_production_theater_entrance_is_reachable_today` 给出「本段不依赖 P3-c」的实证：(172,45) reachable、(175,45) 不 reachable。5. `test_the_lecture_news_event_is_not_picked_up` 绿 —— 公开课缺陷现状被钉死，本段确未顺带修（归 #8）。6. `len(list(ActionType)) == 16` 由本文件与 `tests/test_lab_building.py` 双份钉死。

**commit**：

```
feat(crowd): stage 事件场地解析(能力门+可达性自保)+ 稳定排序 helper 收敛
```

### P2-S12 — stage_event_cohort：照 market_day_crowd_cohort 形状的确定性观众名单（独立缓存 + 独立单飞锁）

**Flag / 批次**：无（纯查询 + 进程内缓存，零生产调用方；闸只加在调用点，见 P2-S13）。非迁移批次。

**为什么**：#9 的本体。照 `market_day_crowd_cohort`（crowd_service.py:122-204）逐条同构：进程内 TTL 缓存 + asyncio 单飞锁 + fail-open + sha256 稳定排序 —— 该函数注释自陈这是 perf 红线（否则每 tick 每居民一次居民表查询）。与集市 cohort 的唯一实质差异：**不按站位排除**候选。集市靠持久化 visitor 分配定名单，而按站位排除会让到场者每 20s 掉出名单、后来者被逐批补进，把全镇轮着拉空；不排除则名单在同一场演出内稳定，到场者继续占位。零生产调用方、不挂闸。

#### 先写的测试（必须跑出失败）

新建文件：/Volumes/data/dev/simverse-world/backend/tests/test_stage_event_cohort.py

```python
"""P2-S12: stage_event_cohort —— 照 market_day_crowd_cohort 形状的确定性观众名单。

照抄的四件事(crowd_service.py:122-204 的注释自陈 perf 红线):进程内 TTL 缓存、
asyncio 单飞锁、异常 fail-open、sha256 稳定排序。
刻意不照抄的一件事:**不按站位排除候选**。按站位排除会让到场者每 20s 掉出名单、
后来者被逐批补进,把全镇轮着拉空;不排除则同一场演出内名单稳定,到场者继续占位。
"""
import asyncio

import pytest

from app.agent.actions import ActionType
from app.agent.location_caps import CAP_STAGE
from app.agent.map_data import LOCATIONS
from app.agent import pathfinder
from app.models.resident import Resident
from app.services import crowd_service

THEATER = {
    "name": "剧院", "type": "public", "role": "culture",
    "bounds": (172, 40, 178, 50), "center": (175, 45), "entrance": (172, 45),
    "description": "小镇剧院:辩论、公开课、戏与人群",
    "boosted_actions": ["OBSERVE", "CHAT_RESIDENT"],
}
STAGE_EVENT = {
    "id": "stage-1", "type": "script", "title": "一场辩论",
    "starts_at": "2026-08-17T10:00:00+00:00",
    "ends_at": "2026-08-17T11:00:00+00:00",
    "payload_json": {"location_id": "theater", "debate_id": "d1"},
}


@pytest.fixture(autouse=True)
def _fresh_cohort_cache():
    crowd_service._reset_for_tests()
    yield
    crowd_service._reset_for_tests()


@pytest.fixture
def overlay():
    added: list[str] = []

    def _merge(slug: str, data: dict, capabilities=None) -> str:
        assert slug not in LOCATIONS, slug
        row = dict(data)
        if capabilities is not None:
            row["capabilities"] = capabilities
        LOCATIONS[slug] = row
        added.append(slug)
        pathfinder.reset_walkable_cache()
        return slug

    yield _merge
    for slug in added:
        LOCATIONS.pop(slug, None)
    pathfinder.reset_walkable_cache()


async def _seed(db, n=12, *, status="idle", tile=(75, 56), rtype="npc"):
    made = []
    for i in range(n):
        r = Resident(id=f"aud-{i}", slug=f"aud-{i}", name=f"观众{i}",
                     creator_id="system", resident_type=rtype,
                     district="east_gardens", status=status,
                     tile_x=tile[0], tile_y=tile[1], meta_json={})
        db.add(r)
        made.append(r)
    await db.commit()
    return made


# ── 名单本体 ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_no_stage_event_no_cohort(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _seed(db_session)
    assert await crowd_service.stage_event_cohort(db_session, []) == frozenset()
    assert await crowd_service.stage_event_cohort(db_session, None) == frozenset()


@pytest.mark.anyio
async def test_cohort_is_bounded_and_deterministic(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _seed(db_session, 12)

    first = await crowd_service.stage_event_cohort(db_session, [STAGE_EVENT], ttl=0)
    second = await crowd_service.stage_event_cohort(db_session, [STAGE_EVENT], ttl=0)

    assert first == second
    assert len(first) == crowd_service.STAGE_EVENT_CROWD_LIMIT == 6
    assert first <= {f"aud-{i}" for i in range(12)}


@pytest.mark.anyio
async def test_a_smaller_town_yields_a_smaller_cohort(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _seed(db_session, 3)
    assert len(await crowd_service.stage_event_cohort(
        db_session, [STAGE_EVENT], ttl=0)) == 3


@pytest.mark.anyio
async def test_a_different_event_reshuffles_the_cohort(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _seed(db_session, 12)
    other = {**STAGE_EVENT, "id": "stage-2"}

    a = await crowd_service.stage_event_cohort(db_session, [STAGE_EVENT], ttl=0)
    b = await crowd_service.stage_event_cohort(db_session, [other], ttl=0)
    assert a != b


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["sleeping", "chatting", "socializing"])
async def test_protected_status_is_never_invited(db_session, overlay, status):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _seed(db_session, 6, status=status)
    assert await crowd_service.stage_event_cohort(
        db_session, [STAGE_EVENT], ttl=0) == frozenset()


@pytest.mark.anyio
async def test_non_sim_residents_are_never_invited(db_session, overlay):
    """UGC character / 玩家角色不是自治居民,不参与人流。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _seed(db_session, 6, rtype="character")
    assert await crowd_service.stage_event_cohort(
        db_session, [STAGE_EVENT], ttl=0) == frozenset()


@pytest.mark.anyio
async def test_residents_already_at_the_venue_keep_their_seat(db_session, overlay):
    """刻意不按站位排除:否则到场者每 20s 掉出名单、后来者被逐批补进,整镇被轮着拉空。
    名单在同一场演出内必须稳定。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _seed(db_session, 12)
    before = await crowd_service.stage_event_cohort(db_session, [STAGE_EVENT], ttl=0)

    for rid in before:                       # 所有人都已到场
        r = await db_session.get(Resident, rid)
        r.tile_x, r.tile_y = 174, 45
    await db_session.commit()

    after = await crowd_service.stage_event_cohort(db_session, [STAGE_EVENT], ttl=0)
    assert after == before


@pytest.mark.anyio
async def test_a_venue_without_the_declaration_yields_no_cohort(db_session, overlay):
    overlay("theater", THEATER)
    await _seed(db_session, 12)
    assert await crowd_service.stage_event_cohort(
        db_session, [STAGE_EVENT], ttl=0) == frozenset()


# ── perf 红线:缓存 + 单飞 ────────────────────────────────────────────

@pytest.mark.anyio
async def test_the_cohort_is_cached_within_the_ttl(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _seed(db_session, 12)
    first = await crowd_service.stage_event_cohort(db_session, [STAGE_EVENT])

    for i in range(12, 18):                  # 缓存期内新增居民不得改变名单
        db_session.add(Resident(id=f"aud-{i}", slug=f"aud-{i}", name=f"观众{i}",
                                creator_id="system", resident_type="npc",
                                district="east_gardens", status="idle",
                                tile_x=75, tile_y=56, meta_json={}))
    await db_session.commit()

    assert await crowd_service.stage_event_cohort(db_session, [STAGE_EVENT]) == first


@pytest.mark.anyio
async def test_concurrent_ticks_share_a_single_query(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _seed(db_session, 12)
    calls = {"n": 0}
    real_execute = db_session.execute

    async def counting_execute(*args, **kwargs):
        calls["n"] += 1
        return await real_execute(*args, **kwargs)

    db_session.execute = counting_execute
    try:
        results = await asyncio.gather(*[
            crowd_service.stage_event_cohort(db_session, [STAGE_EVENT])
            for _ in range(8)
        ])
    finally:
        db_session.execute = real_execute

    assert len(set(results)) == 1
    assert calls["n"] == 1, "单飞锁没生效 —— 每 tick 一次居民表查询是 perf 红线"


@pytest.mark.anyio
async def test_a_database_failure_fails_open_to_an_empty_cohort(overlay):
    """查询炸了就不拉人,而不是把异常抛进 decide 相位(tick.py:102-104 会 break 整
    条相位链)。"""
    from unittest.mock import AsyncMock
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    db = AsyncMock()
    db.execute.side_effect = RuntimeError("db down")
    assert await crowd_service.stage_event_cohort(db, [STAGE_EVENT]) == frozenset()


def test_stage_and_market_cohorts_do_not_share_a_lock_or_a_cache():
    """共用单飞锁会让两条人流互相排队;共用缓存会让键互相驱逐。"""
    assert crowd_service._stage_cohort_lock is not crowd_service._market_cohort_lock
    assert crowd_service._stage_cohort_cache is not crowd_service._market_cohort_cache


def test_reset_for_tests_clears_both_cohort_caches():
    crowd_service._stage_cohort_cache[("x", "", "", "theater")] = (0.0, frozenset())
    crowd_service._market_cohort_cache[("x", "", "", False)] = (0.0, frozenset())
    crowd_service._reset_for_tests()
    assert not crowd_service._stage_cohort_cache
    assert not crowd_service._market_cohort_cache


def test_action_type_enum_is_untouched():
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
```

先跑一次拿红。预期失败形态：收集期通过，用例期多条 `AttributeError: module 'app.services.crowd_service' has no attribute 'stage_event_cohort'` / `'STAGE_EVENT_CROWD_LIMIT'` / `'_stage_cohort_lock'`。

#### 实现

改文件：/Volumes/data/dev/simverse-world/backend/app/services/crowd_service.py（三处）

**改动 1 —— 常量与缓存**。锚点：crowd_service.py:40-43（`grep -c '^_market_cohort_lock = asyncio.Lock()' app/services/crowd_service.py` 应为 1）。

before：
```python
_market_cohort_cache: dict[
    tuple[str, str, str, bool], tuple[float, frozenset[str]]
] = {}
_market_cohort_lock = asyncio.Lock()
```
after：
```python
_market_cohort_cache: dict[
    tuple[str, str, str, bool], tuple[float, frozenset[str]]
] = {}
_market_cohort_lock = asyncio.Lock()

# 舞台观众名单:与集市名单**分开**的缓存与单飞锁。共用锁会让两条人流互相排队,
# 共用缓存会让两种键互相驱逐(下面那个 >8 的裁剪是按时间戳挑最老的)。
STAGE_EVENT_CROWD_LIMIT = 6
STAGE_EVENT_COHORT_TTL_SECONDS = 20.0
_stage_cohort_cache: dict[
    tuple[str, str, str, str], tuple[float, frozenset[str]]
] = {}
_stage_cohort_lock = asyncio.Lock()
```

**改动 2 —— _reset_for_tests**。锚点：crowd_service.py:55-62，整体替换。

before：
```python
def _reset_for_tests() -> None:  # pragma: no cover - test hook
    global _market_cohort_lock
    _counts_cache["ts"] = -1e9
    _counts_cache["data"] = {}
    _market_cohort_cache.clear()
    # Tests may use a fresh event loop per case. Production keeps one loop, but
    # replacing this test-only lock avoids retaining a lock from an old loop.
    _market_cohort_lock = asyncio.Lock()
```
after：
```python
def _reset_for_tests() -> None:  # pragma: no cover - test hook
    global _market_cohort_lock, _stage_cohort_lock
    _counts_cache["ts"] = -1e9
    _counts_cache["data"] = {}
    _market_cohort_cache.clear()
    _stage_cohort_cache.clear()
    # Tests may use a fresh event loop per case. Production keeps one loop, but
    # replacing this test-only lock avoids retaining a lock from an old loop.
    _market_cohort_lock = asyncio.Lock()
    _stage_cohort_lock = asyncio.Lock()
```

**改动 3 —— cohort 本体**。锚点：P2-S11 插入的 `stage_venue_at` 结尾两行（`grep -c 'def stage_venue_at' app/services/crowd_service.py` 应为 1）。

before：
```python
    from app.agent.map_data import capability_location_at
    return capability_location_at(x or 0, y or 0, CAP_STAGE)
```
after：
```python
    from app.agent.map_data import capability_location_at
    return capability_location_at(x or 0, y or 0, CAP_STAGE)


async def stage_event_cohort(
    db,
    world_events,
    *,
    ttl: float = STAGE_EVENT_COHORT_TTL_SECONDS,
) -> frozenset[str]:
    """演出期间被确定性邀请到场的观众(至多 STAGE_EVENT_CROWD_LIMIT 人)。

    形状照 market_day_crowd_cohort(:122-204):进程内 TTL 缓存 + 单飞锁,避免每个并发
    tick 各查一次居民表(该函数的注释自陈这是 perf 红线);挑的是**真实**的清醒自治
    居民,移动仍走正常的 VISIT_DISTRICT 通路;查询异常 fail-open 成空集合并短暂缓存,
    以免一次库故障被放大 N 倍。

    与集市名单的唯一实质差异:**不按站位排除候选**。集市的名单由持久化的 visitor
    分配定死,舞台没有那层持久化 —— 一旦按站位排除,到场者会在下一个 TTL 窗掉出名单、
    后来者被逐批补进,一场戏能把全镇轮着拉空。不排除则同一场演出内名单稳定,已到场的
    人继续占着位置(「到没到」由调用方按站位判,见 stage_venue_at)。

    这就是 design_P2.md §③ 路 B 的外力:名单把 N 个人送到场后,actions.py:80-86 的
    idle_nearby 自然非空,CHAT_RESIDENT 自动解锁 —— 鸡生蛋被打破一次即可自持,而
    actions.py 一个字都不用改。
    """
    event_key = _active_stage_event_key(world_events)
    if event_key is None:
        return frozenset()

    now = time.monotonic()
    cached = _stage_cohort_cache.get(event_key)
    if cached is not None and ttl > 0 and now - cached[0] < ttl:
        return cached[1]

    async with _stage_cohort_lock:
        now = time.monotonic()
        cached = _stage_cohort_cache.get(event_key)
        if cached is not None and ttl > 0 and now - cached[0] < ttl:
            return cached[1]

        try:
            from sqlalchemy import select
            from app.models.resident import Resident

            rows = (await db.execute(
                select(Resident.id).where(
                    Resident.is_autonomous,
                    Resident.resident_type.in_(["npc", "resident"]),
                    Resident.status.not_in(["sleeping", "chatting", "socializing"]),
                )
            )).all()
            eligible = [str(row[0]) for row in rows]
            chosen = frozenset(sorted(
                eligible,
                key=lambda resident_id: _stable_rank(event_key, resident_id),
            )[:STAGE_EVENT_CROWD_LIMIT])
        except Exception:
            # 不拉人好过把异常抛回 decide 相位(tick.py:102-104 会 break 整条相位链)。
            logger.warning("stage event crowd cohort query failed", exc_info=True)
            chosen = frozenset()

        _stage_cohort_cache[event_key] = (now, chosen)
        # 只有活跃键有用;合成事件快速轮换 id 时把这个小缓存钉在有界大小。
        if len(_stage_cohort_cache) > 8:
            oldest = min(_stage_cohort_cache,
                         key=lambda key: _stage_cohort_cache[key][0])
            if oldest != event_key:
                _stage_cohort_cache.pop(oldest, None)
        return chosen
```

本 step 不改任何其它文件；`market_day_crowd_cohort`（:122-204）一个字不动。

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_stage_event_cohort.py tests/test_stage_event_venue.py tests/test_crowd.py tests/test_caravan_market_visitors.py tests/test_caravan_lifecycle.py tests/test_lab_building.py -q
```

**验收**：1. 实现前红：多条 `AttributeError: module 'app.services.crowd_service' has no attribute 'stage_event_cohort'/'STAGE_EVENT_CROWD_LIMIT'/'_stage_cohort_lock'`。2. 实现后全绿，其中 `tests/test_crowd.py`、`tests/test_caravan_market_visitors.py`、`tests/test_caravan_lifecycle.py` 零改动全绿（集市 cohort 未被波及）。3. perf 红线由 `test_concurrent_ticks_share_a_single_query` 钉死：8 个并发 tick 只落 1 次查询。4. 名单稳定性由 `test_residents_already_at_the_venue_keep_their_seat` 钉死（到场者不掉名单）。5. 本 step 零生产调用方：`grep -rn 'stage_event_cohort' app/ | grep -v 'app/services/crowd_service.py'` 输出为空。6. `git diff app/services/crowd_service.py | grep -c 'market_day_crowd_cohort'` 输出 0（集市路径未被改动）。7. `len(list(ActionType)) == 16`。

**commit**：

```
feat(crowd): stage_event_cohort——确定性观众名单(独立缓存+单飞锁),零生产调用方
```

### P2-S13 — 引入 STAGE_EVENT_CROWD_ENABLED（默认关，双写两份 env 模板）+ decide 接 _maybe_stage_crowd + 路 B 不变式守卫

**Flag / 批次**：新增 `stage_event_crowd_enabled: bool = False`（env `STAGE_EVENT_CROWD_ENABLED`，`backend/.env.example` 与 `deploy/backend/.env.example` 同 commit 双写 false）。关 = decide 排序与今天逐字节等价、零额外查询。非迁移批次（纯代码 + 两份模板文档，零 DB 改动）；theater 的 `capabilities={"stage":{}}` 存量回填与真正的开闸分属另外两个批次。

**为什么**：#9 的接线，也是 §③ 路 B 的收口。分支插在 `_maybe_duty_venue` 之后、Case 2 之前：生计优先于看戏；再往上是 caravan cohort（gameplay 权威）与临界需求（0809 死锁守卫），都不得被盖掉；再往下就是死码（三份 YAML 全开 skip_decide_when_planned）。动作必须是 VISIT_DISTRICT —— memorize 只对三个移动动作写 metadata['move']（memorize/basic.py:175），M3/M4 的验收口径正吃它。路 B 的关键论证「不破坏其它地点的 CHAT_RESIDENT」在测试里是可执行断言：actions.py 零改动 + 授权集逐条对拍。

#### 先写的测试（必须跑出失败）

新建文件：/Volumes/data/dev/simverse-world/backend/tests/test_stage_event_crowd_decide.py

```python
"""P2-S13: decide 的 _maybe_stage_crowd 分支 + §③ 路 B 的不变式守卫。

四类断言:
  1 闸与守卫(闸/可用集/status/粘性行程/GO_HOME/不在名单/已在场/无演出);
  2 命中后的上下文副作用(plan_followed=False + plan.status=interrupted),漏置会让
    tick.py:127-131 把这次自由移动误判成 planned_move 写进粘性行程;
  3 排序不变式:临界需求 > caravan cohort > 营生导流 > 看戏 > 计划跳过,源码顺序
    由文本断言钉死;
  4 **路 B 的核心论证**:actions.py 一个字不改,所以其它地点的 CHAT_RESIDENT 行为
    逐条不变;鸡生蛋只被「人真的到场」这一个外力打破。
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.actions import ActionType, get_available_actions
from app.agent.location_caps import CAP_STAGE
from app.agent.map_data import LOCATIONS
from app.agent import pathfinder
from app.agent.plan_target import resolve_location_id
from app.agent.schemas import HourlyPlan, TickContext
from app.config import Settings, settings
from app.services import crowd_service

BACKEND = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = BACKEND / ".env.example"
DEPLOY_ENV_EXAMPLE = BACKEND.parent / "deploy" / "backend" / ".env.example"
DECIDE_SRC = BACKEND / "app" / "agent" / "phases" / "decide" / "basic.py"
ACTIONS_SRC = BACKEND / "app" / "agent" / "actions.py"

THEATER = {
    "name": "剧院", "type": "public", "role": "culture",
    "bounds": (172, 40, 178, 50), "center": (175, 45), "entrance": (172, 45),
    "description": "小镇剧院:辩论、公开课、戏与人群",
    "boosted_actions": ["OBSERVE", "CHAT_RESIDENT"],
}
STAGE_EVENT = {
    "id": "stage-1", "type": "script", "title": "一场辩论",
    "starts_at": "2026-08-17T10:00:00+00:00",
    "ends_at": "2026-08-17T11:00:00+00:00",
    "payload_json": {"location_id": "theater", "debate_id": "d1"},
}


@pytest.fixture(autouse=True)
def _quiet_world(monkeypatch):
    """默认关掉会抢在本分支之前的三条通路,单测只留一个变量。"""
    crowd_service._reset_for_tests()
    monkeypatch.setattr(settings, "stage_event_crowd_enabled", True)
    monkeypatch.setattr(settings, "realism_crowd_enabled", False)
    monkeypatch.setattr(settings, "caravan_lifecycle_enabled", False)
    monkeypatch.setattr(settings, "duty_venue_enabled", False)
    yield
    crowd_service._reset_for_tests()


@pytest.fixture
def overlay():
    added: list[str] = []

    def _merge(slug: str, data: dict, capabilities=None) -> str:
        assert slug not in LOCATIONS, slug
        row = dict(data)
        if capabilities is not None:
            row["capabilities"] = capabilities
        LOCATIONS[slug] = row
        added.append(slug)
        pathfinder.reset_walkable_cache()
        return slug

    yield _merge
    for slug in added:
        LOCATIONS.pop(slug, None)
    pathfinder.reset_walkable_cache()


def _res(rid="aud-0", tile=(75, 56), *, status="idle", rtype="npc"):
    return SimpleNamespace(
        id=rid, slug=rid, name="观众", resident_type=rtype, status=status,
        tile_x=tile[0], tile_y=tile[1], meta_json={},
        home_location_id=None, home_tile_x=5, home_tile_y=5,
    )


def _ctx(resident, world_events=None, plan=None):
    ctx = TickContext(db=AsyncMock(), resident=resident, world_time="20:00",
                      hour=20, schedule_phase="傍晚",
                      current_plan=plan, scheduled_plan=plan)
    ctx.world_events = world_events if world_events is not None else [STAGE_EVENT]
    ctx.available_actions = [ActionType.VISIT_DISTRICT, ActionType.OBSERVE,
                             ActionType.IDLE]
    return ctx


def _plugin(**params):
    from app.agent.phases.decide.basic import BasicDecidePlugin
    plug = BasicDecidePlugin(params=params or None)
    plug._load_memories = AsyncMock()
    return plug


def _cohort(*ids):
    return patch.object(crowd_service, "stage_event_cohort",
                        AsyncMock(return_value=frozenset(ids)))


# ── 闸本身 ────────────────────────────────────────────────────────────

def test_flag_defaults_to_off():
    assert Settings.model_fields["stage_event_crowd_enabled"].default is False


def test_flag_is_documented_as_false_in_both_env_templates():
    """STAGE_EVENT_CROWD_ENABLED 不在 GOVERNANCE_PREFIXES(CIVIC_/REP_/POLIS_OFFICE_)
    里,现成的那条 parity 扫不到它 —— 而「扫不到」的表现和「模板里没有这个键」一模
    一样。这里按键名兜底,不依赖任何前缀。"""
    for path in (ENV_EXAMPLE, DEPLOY_ENV_EXAMPLE):
        assert "STAGE_EVENT_CROWD_ENABLED=false" in path.read_text(encoding="utf-8"), path


@pytest.mark.anyio
async def test_gated_off_is_inert(overlay, monkeypatch):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    monkeypatch.setattr(settings, "stage_event_crowd_enabled", False)
    with _cohort("aud-0"):
        assert await _plugin()._maybe_stage_crowd(_ctx(_res())) is None


# ── 命中 ──────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_pulls_a_listed_resident_to_the_venue(overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    with _cohort("aud-0", "aud-1"):
        res = await _plugin()._maybe_stage_crowd(_ctx(_res()))
    assert res is not None
    assert res.action == ActionType.VISIT_DISTRICT
    assert res.target_slug == "theater"
    assert res.target_tile == (172, 45)      # entrance,不是孤岛 center(175,45)


@pytest.mark.anyio
async def test_target_slug_is_resolvable_so_the_visit_metric_can_see_it(overlay):
    """memorize 的 move.target 经 resolve_location_id 解析(memorize/basic.py:62-63);
    解析不出就写成 null,M3/M4 的到访统计完全看不到这次导流。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    with _cohort("aud-0"):
        res = await _plugin()._maybe_stage_crowd(_ctx(_res()))
    assert resolve_location_id(res.target_slug, res.target_slug) == "theater"


@pytest.mark.anyio
async def test_hit_marks_the_plan_interrupted_and_unfollowed(overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    plan = HourlyPlan(2, (18, 22), "STUDY", None, "图书馆", 3, "看书")
    ctx = _ctx(_res(), plan=plan)
    with _cohort("aud-0"):
        out = await _plugin(skip_decide_when_planned=True).execute(ctx)
    assert out.action_result.target_slug == "theater"
    assert out.plan_followed is False
    assert plan.status == "interrupted"


@pytest.mark.anyio
async def test_the_branch_never_claims_a_market_trip(overlay):
    """market_trip_event_id 是集市专用:tick.py:155-162 会把行程的 kind/location 写死
    成 market_day/market_hall,借用它等于把观众登记成买家。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    ctx = _ctx(_res())
    with _cohort("aud-0"):
        await _plugin()._maybe_stage_crowd(ctx)
    assert ctx.market_trip_event_id is None


# ── 守卫 ──────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_not_on_the_list_is_not_pulled(overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    with _cohort("someone-else"):
        assert await _plugin()._maybe_stage_crowd(_ctx(_res())) is None


@pytest.mark.anyio
async def test_already_at_the_venue_is_not_pulled_again(overlay):
    """站位判断必须穿透 outdoor 遮蔽(get_location_id_at(174,45)==\"east_gardens\"),
    否则人站在剧院里还会被每 tick 再拉一次。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    with _cohort("aud-0"):
        assert await _plugin()._maybe_stage_crowd(
            _ctx(_res(tile=(174, 45)))) is None


@pytest.mark.anyio
async def test_no_stage_event_does_not_query_the_cohort_at_all(overlay):
    """没戏可看时连查询都不该发生 —— 解析在前、查库在后。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    probe = AsyncMock(return_value=frozenset({"aud-0"}))
    with patch.object(crowd_service, "stage_event_cohort", probe):
        assert await _plugin()._maybe_stage_crowd(_ctx(_res(), [])) is None
    probe.assert_not_awaited()


@pytest.mark.anyio
async def test_legacy_row_without_declaration_does_not_pull(overlay):
    overlay("theater", THEATER)
    with _cohort("aud-0"):
        assert await _plugin()._maybe_stage_crowd(_ctx(_res())) is None


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["sleeping", "chatting", "socializing"])
async def test_protected_status_is_never_interrupted(overlay, status):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    with _cohort("aud-0"):
        assert await _plugin()._maybe_stage_crowd(
            _ctx(_res(status=status))) is None


@pytest.mark.anyio
async def test_active_trip_wins(overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    ctx = _ctx(_res())
    ctx.continuation_trip = {"action": "VISIT_DISTRICT"}
    with _cohort("aud-0"):
        assert await _plugin()._maybe_stage_crowd(ctx) is None


@pytest.mark.anyio
async def test_going_home_is_not_entertainment(overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    plan = HourlyPlan(0, (18, 22), "GO_HOME", None, "home", 3, "回家")
    with _cohort("aud-0"):
        assert await _plugin()._maybe_stage_crowd(
            _ctx(_res(), plan=plan)) is None


@pytest.mark.anyio
async def test_requires_visit_district_to_be_available(overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    ctx = _ctx(_res())
    ctx.available_actions = [ActionType.IDLE]
    with _cohort("aud-0"):
        assert await _plugin()._maybe_stage_crowd(ctx) is None


@pytest.mark.anyio
async def test_critical_need_still_outranks_the_show(overlay, monkeypatch):
    """0809「饿死在自家门口」的守卫:临界需求必须排在人流拉力之前。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    monkeypatch.setattr(settings, "realism_enabled", True)
    res = _res()
    res.meta_json = {"needs": {"energy": 0.05, "satiety": 0.8, "social": 0.8}}
    ctx = _ctx(res)
    ctx.available_actions.append(ActionType.GO_HOME)
    with _cohort("aud-0"):
        out = await _plugin(skip_decide_when_planned=True).execute(ctx)
    assert out.action_result.action == ActionType.GO_HOME


# ── 排序:源码顺序 ────────────────────────────────────────────────────

def test_source_order_is_crowd_then_duty_then_stage_then_case_two():
    text = DECIDE_SRC.read_text(encoding="utf-8")
    i_crowd = text.index("crowd = await self._maybe_crowd_draw(ctx)")
    i_duty = text.index("duty_venue = await self._maybe_duty_venue(ctx)")
    i_stage = text.index("stage_crowd = await self._maybe_stage_crowd(ctx)")
    i_case2 = text.index("# Case 2 (E-09/E-10): plan-priority skip.")
    assert i_crowd < i_duty < i_stage < i_case2


def test_decide_never_names_the_p1_reverse_lookup_helpers():
    """P1-S9 的 test_market_capability_is_not_used_for_venue_resolution 读本文件全文;
    地点解析必须全部经 crowd_service 的包装函数。"""
    text = DECIDE_SRC.read_text(encoding="utf-8")
    assert "capability_locations" not in text
    assert "nearest_capability_location" not in text
    assert "capability_location_at" not in text


def test_decide_adds_no_bare_theater_or_market_hall_literal():
    """场地来自事件 + 能力声明,不是 decide 里的字面量(P1-S9 另有一条同款守卫)。"""
    import re
    for i, line in enumerate(DECIDE_SRC.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        assert not re.search(r"[\"'](theater|market_hall)[\"']", line), f"{i}: {line}"


# ── §③ 路 B 的核心论证:actions.py 零改动 ─────────────────────────────

def test_the_chat_gate_source_is_untouched_verbatim():
    """路 B 的全部要点:CHAT_RESIDENT 的判据一个字都不改。"""
    text = ACTIONS_SRC.read_text(encoding="utf-8")
    assert ('    idle_nearby = [r for r in nearby_residents\n'
            '                   if _targetable(r) and r.status in ("idle", "walking")]'
            ) in text
    assert ('    if idle_nearby:\n'
            '        available.extend(_SOCIAL_NEEDS_IDLE_TARGET)') in text
    for token in ("stage", "cohort", "crowd_service", "world_events"):
        assert token not in text, token


@pytest.mark.parametrize("gate", [True, False])
@pytest.mark.parametrize("tile", [(75, 56), (174, 45)])
def test_other_locations_keep_their_exact_authorization_set(
        overlay, monkeypatch, gate, tile):
    """授权集是 (居民, 附近的人) 的纯函数 —— 演出、闸态、站位都不改变它。

    这是「不破坏其它地点的 CHAT_RESIDENT 行为」的机器证明:get_available_actions
    根本不接收 world_events,本段也没有给它加任何参数或分支。
    """
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    monkeypatch.setattr(settings, "stage_event_crowd_enabled", gate)
    me = _res("me", tile)
    lonely = get_available_actions(me, nearby_residents=[])
    assert ActionType.CHAT_RESIDENT not in lonely
    assert ActionType.GOSSIP not in lonely and ActionType.EAVESDROP not in lonely

    peer = _res("peer", tile)
    with_peer = get_available_actions(me, nearby_residents=[peer])
    assert ActionType.CHAT_RESIDENT in with_peer


def test_the_lock_opens_by_itself_once_the_cohort_arrives(overlay):
    """§③ 的结论:名单把人送到场后,idle_nearby 自然非空,CHAT_RESIDENT 自动解锁 ——
    鸡生蛋被外力打破一次即可自持,不需要碰 actions.py 一个字。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    me = _res("me", (174, 45))
    assert ActionType.CHAT_RESIDENT not in get_available_actions(me, [])  # 到场前:空场
    arrived = [_res(f"aud-{i}", (174, 45)) for i in range(3)]
    assert ActionType.CHAT_RESIDENT in get_available_actions(me, arrived)


def test_action_type_enum_is_untouched():
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
```

先跑一次拿红。预期失败形态：`test_flag_defaults_to_off` 抛 `KeyError: 'stage_event_crowd_enabled'`；多条 `AttributeError: 'BasicDecidePlugin' object has no attribute '_maybe_stage_crowd'`；`test_source_order_is_crowd_then_duty_then_stage_then_case_two` 抛 `ValueError: substring not found`。

#### 实现

四处改动，全部同一 commit。

**改动 1** —— /Volumes/data/dev/simverse-world/backend/app/config.py

锚点：`    # P2 Task 1 — relation write deltas (reused, zero new LLM calls) + decay.`（config.py:593，`grep -c` 输出 1；P2-S3 与 #7 也在此行之前追加过，按 step 顺序执行不冲突）。

before：
```python
    # P2 Task 1 — relation write deltas (reused, zero new LLM calls) + decay.
```
after：
```python
    # --- P2 剧院人流 (STAGE_EVENT_CROWD_ENABLED) ---
    # 演出期间把一支确定性的观众名单(≤6 人,sha256 稳定排序)拉到声明了 stage 能力的
    # 那栋楼。关 = decide 的 _maybe_stage_crowd 第一行即返回,决策排序与今天逐字节
    # 等价、零额外查询。
    # 这是 design_P2.md §③ 路 B:actions.py:80-86 的 CHAT_RESIDENT 判据一个字不改,
    # 靠「人真的到场」把鸡生蛋打破一次即可自持 —— 副作用半径只在有 active stage
    # 事件时,其它地点的授权集逐条不变。
    # 与 REALISM_CROWD_ENABLED 正交:确定性名单是 gameplay 拉力,不是装饰性抽签
    # (照 caravan lifecycle 的先例,decide/basic.py:361-365)。
    stage_event_crowd_enabled: bool = False

    # P2 Task 1 — relation write deltas (reused, zero new LLM calls) + decay.
```

**改动 2** —— /Volumes/data/dev/simverse-world/backend/.env.example

锚点：P2-S3 写入的 `DUTY_VENUE_ENABLED=false` 那一行（`grep -c '^DUTY_VENUE_ENABLED=false' .env.example` 应为 1）。在其后追加：

```

# ── P2 剧院人流（STAGE_EVENT_CROWD_ENABLED）─────────────────────────────────
# 关（默认）= decide 的 _maybe_stage_crowd 第一行即返回：决策排序与今天逐字节等价、
# 零额外查询、零 LLM。
# 开 = 演出期间（world_events 里有 festival/script 事件，且它 payload 指的那栋楼自己
# 声明了 stage 能力、目的地 tile 与镇区连通），按 sha256 稳定排序挑至多 6 名清醒的
# 自治居民，把这一 tick 定成 VISIT_DISTRICT 去那栋楼（零 LLM）。名单在同一场演出内
# 稳定，已到场的人继续占位、不会被再拉一次。
#
# 为什么这样治「剧院 15 天 0 到访」（design_P2.md §③ 路 B）：不改
# actions.py:80-86 的 CHAT_RESIDENT 判据（附近有 idle/walking 的人才解锁）。改判据
# 会让 LLM 在空场瞎编 target_slug —— 白花钱、吃掉一格日行动 cap、什么都不发生。
# 人真的到场后 idle_nearby 自然非空，锁自己就开了，而其它地点的授权集一个字不变。
#
# 与 REALISM_CROWD_ENABLED 正交：那道闸管的是 ×3 加权抽签（装饰性），本闸是确定性
# 名单（gameplay 拉力），照 caravan lifecycle 的先例解耦。两闸互不为前置。
#
# 开闸硬顺序（写死，别凭记忆）：
#   1 代码侧——P1 的 location_caps 必须已登记 stage（civic_grantable=true），且
#     capability_location_at / location_capabilities 已合入。本闸**不**依赖
#     LOCATION_CAPABILITIES_ENABLED（那两个是不读闸的纯查询）。
#   2 内容侧——必须真的有演出：STAGE_EVENT_ENABLED 开了、辩论/公开课会建出
#     type="script" 且 payload_json.location_id 指向剧院的 world_event。没有演出时
#     本闸开着也完全 inert。
#   3 数据侧（真正的硬前置）——生产 dynamic_locations 里 theater 那行的 data_json
#     必须已带上 capabilities={"stage":{}}。存量行是公投建的，没有这个键；回填是纯
#     数据变更，**必须独立批次**（迁移/数据变更与开闸不同车，07-25 事故红线）。
#     没回填就开闸不会出事，只是名单恒为空、与今天等价。
#
# 开闸后的核验（按天看，别看容器日志——它会轮转；memories.metadata_json 是 json 不是
# jsonb，所有查询必须带 created_at 时间窗）：
#   M3 到访从 0 起飞（基线 0 次 0 人 / 15 天；验收线 visits≥20 且 people≥6）：
#     select count(*) visits, count(distinct m.resident_id) people
#       from memories m
#      where m.metadata_json->'move'->>'target' = 'theater'
#        and m.metadata_json->'move'->>'arrived' = 'true'
#        and m.created_at >= now() - interval '14 days';
#   M4 到访是被事件拉去的（during/(during+off) ≥ 0.6）：
#     with ev as (select id, starts_at, ends_at from world_events
#                  where type='script' and payload_json->>'location_id'='theater')
#     select count(*) filter (where ev.id is not null) during_event,
#            count(*) filter (where ev.id is null)     off_event
#       from memories m
#       left join ev on m.created_at between ev.starts_at and ev.ends_at
#      where m.metadata_json->'move'->>'target' = 'theater'
#        and m.metadata_json->'move'->>'arrived' = 'true'
#        and m.created_at >= now() - interval '14 days';
#
# 开闸后必盯（已知未闭合项）：本分支刻意不落粘性行程（不设 market_trip_event_id——
# tick.py:155-162 会把行程的 kind/location 写死成 market_day/market_hall），所以名单里
# 的人每 tick 重算目的地且每 tick 吃一格 AGENT_MAX_DAILY_ACTIONS（默认 20）。剧院入口
# (172,45) 离镇中心 (75,56) 曼哈顿 108 格 ÷ REALISM_MOVE_SPEED=8 ≈ 14 tick。若当天配额
# 被走路烧光，表现是 M3 的 visits 上不去而 attempts 很高——先查这里，不是查代码。
STAGE_EVENT_CROWD_ENABLED=false
```

**改动 3** —— /Volumes/data/dev/simverse-world/deploy/backend/.env.example

锚点：P2-S5 写入的 `DUTY_VENUE_ENABLED=false` 那一行（`grep -c '^DUTY_VENUE_ENABLED=false' deploy/backend/.env.example` 应为 1）。在其后追加：

```

# ── P2 剧院人流（STAGE_EVENT_CROWD_ENABLED）─────────────────────────────────
# 这个键**必须**手工同步到本文件：STAGE_ 既不在 GOVERNANCE_PREFIXES
# （CIVIC_/REP_/POLIS_OFFICE_，tests/test_env_example_consistency.py:183）里，也不在
# REALISM_POOL_ / REALISM_PLAN_ / REALISM_EVENT_MEMORY_ / LOCATION_ / DUTY_VENUE_
# 任何一条现成 parity 的前缀内——漏写时那些 parity 全都扫不到它，而「扫不到」的表现
# 和「模板里根本没有这个键」一模一样。由 backend/tests/test_stage_event_crowd_decide.py
# 的 test_flag_is_documented_as_false_in_both_env_templates 按键名兜底守着。
#
# 关（默认）= decide 零新分支、零额外查询，与今天逐字节等价。
# 开 = 演出期间按 sha256 稳定排序挑至多 6 名清醒自治居民去那栋楼（VISIT_DISTRICT，
# 零 LLM）。治的是「剧院 15 天 0 到访 0 人」，做法是把人送到场，而不是放宽
# actions.py 的 CHAT_RESIDENT 判据（放宽会让 LLM 在空场瞎编对话对象，白花钱且吃掉
# 日行动 cap）。
#
# 开闸硬顺序：①P1 的 location_caps 登记了 stage；②真的有演出（STAGE_EVENT_ENABLED
# 开、辩论/公开课建出 type="script" 且 location_id=theater 的 world_event）；
# ③生产 dynamic_locations 里 theater 那行的 data_json 已回填 capabilities={"stage":{}}
# ——回填是纯数据变更，必须独立批次（迁移/数据变更与开闸不同车）。三条缺任一，本闸
# 开着也只是 inert，与今天等价。
# 与 REALISM_CROWD_ENABLED 正交，互不为前置。
STAGE_EVENT_CROWD_ENABLED=false
```

**改动 4** —— /Volumes/data/dev/simverse-world/backend/app/agent/phases/decide/basic.py（两处）

4a 调用块。锚点：Case 2 注释首行（`grep -c '# Case 2 (E-09/E-10): plan-priority skip. Follow the plan without an LLM' app/agent/phases/decide/basic.py` 应为 1）。

before：
```python
        # Case 2 (E-09/E-10): plan-priority skip. Follow the plan without an LLM
```
after：
```python
        # P2 #9 (STAGE_EVENT_CROWD): 有戏在演时,把确定性的观众名单拉到那栋楼。
        # 排在 duty 之后 —— 生计优先于看戏;排在 crowd 之后 —— caravan cohort 是
        # gameplay 权威;排在 Case 2 之前 —— 三份出厂 YAML 全开
        # skip_decide_when_planned,插在它之后就是死码。
        stage_crowd = await self._maybe_stage_crowd(ctx)
        if stage_crowd is not None:
            ctx.action_result = stage_crowd
            ctx.plan_followed = False
            if plan:
                plan.status = "interrupted"
            return ctx

        # Case 2 (E-09/E-10): plan-priority skip. Follow the plan without an LLM
```

4b 方法本体。锚点：`    async def _crowd_hint(self, ctx: TickContext) -> str:`（`grep -c 'async def _crowd_hint' app/agent/phases/decide/basic.py` 应为 1）。

before：
```python
    async def _crowd_hint(self, ctx: TickContext) -> str:
```
after：
```python
    async def _maybe_stage_crowd(self, ctx: TickContext) -> ActionResult | None:
        """P2 #9: 演出期间把确定性的观众名单送到场(VISIT_DISTRICT,零 LLM)。

        这是 design_P2.md §③ 路 B「地点吸引力与在场人数解耦」的落点:
        actions.py:80-86 的 CHAT_RESIDENT 判据一个字不改 —— 改判据(路 A)会让 LLM 在
        空场瞎编 target_slug,找不到人、静默无事发生,却已花掉 LLM 钱和一格日行动 cap。
        这里换成把人真的送到场:idle_nearby 自然非空,锁自己就开了,且这条路径对没有
        stage 事件的其它地点零影响。

        动作必须是 VISIT_DISTRICT:memorize 只在 action ∈ {WANDER, VISIT_DISTRICT,
        GO_HOME} 时写 metadata['move'](memorize/basic.py:175),而到访验收的口径正是
        metadata_json->'move'->>'target' —— 产出别的动作,统计完全看不到。

        场地解析全部经 crowd_service 的包装函数:一来「哪场事件算演出」与「哪栋楼能
        当舞台」两侧不互相硬编码 slug,二来本文件被 P1-S9 的守卫读全文,不得出现
        capability_locations / nearest_capability_location,也不得留裸的地点字面量。

        守卫集合与 _maybe_crowd_draw 逐条对齐(可用集 / status / 粘性行程 / GO_HOME);
        上面几条 early-return 已经挡掉了饿死、暴雨、在途粘性、商队 gameplay 权威与
        营生导流,这里不重复写。
        """
        if not settings.stage_event_crowd_enabled:
            return None
        if ActionType.VISIT_DISTRICT not in ctx.available_actions:
            return None
        # 缓存的名单可能差几秒。绝不用它把人从对话 / 睡眠 / 已开始的行程里拽出来。
        if ctx.resident.status in ("sleeping", "chatting", "socializing"):
            return None
        if ctx.continuation_trip is not None:
            return None
        # 回家不是看戏。临界精力与 GO_HOME 行程在上面已受保护;这里再挡一次「行程还
        # 没落 Redis 的第一步」。
        if any(
            plan is not None and plan.action == ActionType.GO_HOME.value
            for plan in (ctx.current_plan, ctx.scheduled_plan)
        ):
            return None
        from app.services import crowd_service
        world_events = getattr(ctx, "world_events", None)
        # 先解析场地(纯函数、零查询),没戏可看就别去查名单。
        venue = crowd_service.stage_event_venue(world_events)
        if not venue:
            return None
        if crowd_service.stage_venue_at(
                ctx.resident.tile_x, ctx.resident.tile_y) == venue:
            return None  # 已经在场
        cohort = await crowd_service.stage_event_cohort(ctx.db, world_events)
        if ctx.resident.id not in cohort:
            return None
        from app.agent.map_data import get_valid_target_tile
        target_tile = get_valid_target_tile(venue)
        if not target_tile:
            return None
        # 刻意**不**设 ctx.market_trip_event_id:那是集市专用的粘性通道,
        # tick.py:155-162 会把行程的 kind/location 写死成 market_day/market_hall,
        # 借用它等于把观众登记成买家。代价是本行程不落粘性、每 tick 重算(与既有
        # festival 抽签同形状),给舞台开一条自己的粘性通道是独立 step。
        return ActionResult(
            action=ActionType.VISIT_DISTRICT, target_slug=venue,
            target_tile=target_tile, reason="去看戏",
        )

    async def _crowd_hint(self, ctx: TickContext) -> str:
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_stage_event_crowd_decide.py tests/test_env_example_consistency.py tests/test_stage_event_cohort.py tests/test_stage_event_venue.py tests/test_agent_actions.py tests/test_crowd.py tests/test_realism_needs.py tests/test_caravan_market_visitors.py tests/test_market_hall_constant.py tests/test_duty_venue_decide.py tests/test_capability_locations.py tests/test_deploy_env_protection.py tests/test_lab_building.py -q
```

**验收**：1. 实现前红：`test_flag_defaults_to_off` 抛 `KeyError: 'stage_event_crowd_enabled'`；多条 `AttributeError: ... has no attribute '_maybe_stage_crowd'`；`test_source_order_is_crowd_then_duty_then_stage_then_case_two` 抛 `ValueError: substring not found`。2. 实现后全绿，其中 `tests/test_agent_actions.py`、`tests/test_crowd.py`、`tests/test_realism_needs.py`、`tests/test_caravan_market_visitors.py`、`tests/test_market_hall_constant.py`、`tests/test_duty_venue_decide.py` 零改动全绿。3. `tests/test_env_example_consistency.py::test_every_settings_field_is_documented_or_allowlisted` 绿 —— 新字段已进 `backend/.env.example`；双模板由 `test_flag_is_documented_as_false_in_both_env_templates` 按键名兜底。4. §③ 路 B 的机器证明：`git diff --stat app/agent/actions.py` 输出为空（actions.py 零改动），且 `test_the_chat_gate_source_is_untouched_verbatim` 与 `test_other_locations_keep_their_exact_authorization_set`（4 个参数化组合）全绿。5. `grep -c 'capability_locations\|nearest_capability_location\|capability_location_at' app/agent/phases/decide/basic.py` 输出 0；`grep -c '"market_hall"\|"theater"' app/agent/phases/decide/basic.py` 输出 0（P1-S9 两条守卫不被打红）。6. `.venv/bin/python -c "from app.config import Settings; assert Settings.model_fields['stage_event_crowd_enabled'].default is False"` 退出 0。7. `len(list(ActionType)) == 16`。

**commit**：

```
feat(agent): decide 接 _maybe_stage_crowd 观众导流,挂 STAGE_EVENT_CROWD_ENABLED 默认关
```

## P2 剧院侧第三组（design_P2.md 批次表 #10 + #11）—— 收益与观测的 bite-sized TDD 执行计划

<details><summary>依赖边 / 批次归属 / 与既有守卫的冲突面</summary>

## 依赖边

**边 C（硬阻塞，#10/#11 → #7）**：三个 step 都读 `settings.stage_event_enabled`（env `STAGE_EVENT_ENABLED`，默认 false），由批次表 #7 引入并同 commit 写 `backend/.env.example`。本段**零新增 Settings 字段**，故不动两份 env 模板；acceptance 用 `git diff app/config.py` 为空 + `tests/test_env_example_consistency.py` 双向钉死「没有偷加字段」。S16/S17 各带一条 flag 注册守卫，#7 没落地会当场红并指名。

**边 D（硬阻塞，→ #7 的 WorldEvent 契约）**：`stage_venue_of` 反查的形状是 `type="script"` + `payload_json={"location_id": venue, "debate_id": debate.id}`（design §②-a 原文）。#7 改字段名，S15 的 `test_venue_is_read_from_the_script_event` 当场红。

**边 E（P1）**：`location_caps.CAP_STAGE`（登记已由 P2-S1 notes 写明：civic_grantable=True / unlocks=() / category=None，本段只引用）、`map_data.capability_location_at`（P1-S5）。两者都不读闸。

**边 F（软，只影响验收数值不影响正确性）**：#9 的 cohort 不开 → 剧院恒零人 → 观众名单恒空 → S16 逐字节等于今天，M3/M5 全 0。

剧院坐标越界（bounds x2=178 > WALKABLE_X_RANGE 上限 173、center(175,45) 孤岛）归 P3-c 迁移批次，本段不做也不同批。

## 批次归属

三个 step 全是纯代码：零迁移、零开闸、零新 Settings 字段、零新 ActionType（`len(list(ActionType))==16` 三处复述）。S15 无闸（纯查询、零生产调用方），S16/S17 沿用 #7 的 `STAGE_EVENT_ENABLED`。

## 串并行

严格串行 S15 → S16 → S17。S16 依赖 S15 的两个查询；S17 与 S16 无文件交集（memorize/execute vs debate_service）理论可并行，但共用同一道闸的守卫，顺序跑更省事。

## 与既有守卫的冲突面（已逐条避开）

1. P1-S9 的 `"capability_locations" / "nearest_capability_location" not in text` 只扫 `decide/basic.py` 与 `tick.py` —— 本段三个文件都不在扫描集，且用的是 `capability_location_at`（不含那两个子串）。
2. `tests/test_lab_building.py:85-88` 全程不碰，三个 step 各带一条同款断言。
3. **经济守恒（本组硬门，已逐条核实）**：stake 时钱已从玩家钱包扣走（`coin_service.charge`，:99）；settle 只重分配 `distributable = int(loser_pool*0.95)`、`burn = loser_pool - distributable`、赢方拿 `amount + int(distributable*amount/winner_pool)`，出账恒 ≤ 入账，净销毁 = burn + 取整余数 —— settle 是净 sink，不是铸币口。S16 的观众收益一枚 SC 不动：`inspect.getsource` 源码扫描 + `users.soul_coin_balance` / `resident_treasuries.balance_sc` 快照双证。`treasury_debit` 是纯销毁、`treasury_transfer` 才守恒 —— 本段两个都不用。
4. `_finish_draw` / `_auto_draw_refund` 不走 `_resident_aftermath`，平局无观众收益（有意，与今天一致）。
5. `_audience_aftermath` 跑在 settle 的 `await db.commit()` 之后，异常被自身 try/except 吞并 rollback（防止污染其后的 opinion_service），故不回染 M7。
6. OBSERVE 的记忆由 memorize 既有分支（`memorize/basic.py:135-136`）写，execute 分支**只改 status 不写记忆** —— 否则同一 tick 双份记忆，污染检索与 importance 分位。

## 交接给后续批次（别当已完成）

`metadata["act"]["loc"]` 的具体性靠 stage 能力反查穿透遮蔽，是剧院专用；「任意地点的最具体 id」要等 P3 的 `LOCATION_SPECIFIC_FIRST_ENABLED`，开了之后回落项自己就返 theater，两项等价。关系 bump 是 O(n²)（8 人 = 28 次带 commit 的 UPDATE），只在 settle 跑一次、不在 tick 热路径。

</details>

### P2-S15 — debate_service 加场地反查与在场观众名单两个纯查询（零生产调用方、不挂闸）

**Flag / 批次**：无（纯查询、零生产调用方、零新 Settings 字段；闸只加在调用点，见 P2-S16）。非迁移批次、非开闸批次。

**为什么**：把 #10 需要的「这场辩论在哪演、谁在场」落成两个纯查询先行一步，理由有三：

1. **Debate 表零 location 列，本批次不动 schema**（红线：迁移与行为变更不同车）。地点走 #7 已经建好的 WorldEvent 通道反查，零迁移。
2. **必须用 `capability_location_at` 而不是 `get_location_id_at`**：后者首命中即返（map_data.py:243-249），而 theater(172,40,178,50) 完全落在 outdoor 街区 east_gardens(140,35,179,58) 内部 —— 生产实测 `get_location_id_at(175,45) == "east_gardens"`。照粗查写，观众名单恒为空、#10 静默失效且零告警。这与邮局侧 P2-S2 是同一处校正。
3. **payload 过滤放 Python 侧**：world_events.payload_json 是 `sa.JSON()`（PG 上是 json 非 jsonb），测试库是 sqlite（JSON 运算符不可用）。`drive_due_debates` 的时间判据同样是 Python 侧过滤，口径一致。

本 step 零生产调用方、不挂闸（闸只加在调用点，与 P2-S2 同一条纪律）。名单上限 8 人：关系 bump 是两两 O(n²)，但只在 settle 跑一次、不在 tick 热路径。

#### 先写的测试（必须跑出失败）

新建文件：/Volumes/data/dev/simverse-world/backend/tests/test_debate_stage_audience.py

```python
"""P2-S15: 辩论场地反查 + 在场观众名单(纯查询,零生产调用方,不挂闸)。

核心是一条与邮局侧同构的校正:在场判定必须走 capability_location_at,不能走
get_location_id_at —— theater(172,40,178,50) 完全落在 outdoor 街区
east_gardens(140,35,179,58) 内部。test_masking_is_real_and_the_audience_sees
_through_it 同时钉死「遮蔽是真的」与「能力反查穿透」两件事。
"""
import pytest

from app.agent.actions import ActionType
from app.agent.location_caps import CAP_STAGE
from app.agent.map_data import LOCATIONS, get_location_id_at
from app.models.resident import Resident
from app.models.world_event import WorldEvent
from app.services import debate_service as ds

# 生产 dynamic_locations 里 theater 那行的 data_json(civic_service.py:188-193 原文)。
# capabilities 由调用方按场景决定加不加 —— 存量行今天**没有**这个键。
THEATER = {
    "name": "剧院", "type": "public", "role": "culture",
    "bounds": (172, 40, 178, 50), "center": (175, 45), "entrance": (172, 45),
    "description": "小镇剧院:说书、演展、故事会的舞台",
    "boosted_actions": ["CHAT_RESIDENT", "OBSERVE"],
}
INSIDE = (175, 45)
OUTSIDE = (75, 56)


@pytest.fixture
def overlay():
    """模拟 load_dynamic_locations 的合入:追加到 LOCATIONS 尾部,再还原。"""
    added: list[str] = []

    def _merge(slug: str, data: dict, capabilities=None) -> str:
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


async def _resident(db, slug, tile=INSIDE, *, status="idle", rtype="npc"):
    r = Resident(slug=slug, name=slug, creator_id="system", district="cafe",
                 status=status, resident_type=rtype,
                 tile_x=tile[0], tile_y=tile[1])
    db.add(r)
    await db.commit()
    return r


async def _debate(db):
    await _resident(db, "ann", OUTSIDE)
    await _resident(db, "bo", OUTSIDE)
    return await ds.create_debate(db, "猫和狗谁更好", "ann", "bo")


async def _script_event(db, debate_id, venue="theater"):
    ev = WorldEvent(type="script", title="辩论", description="",
                    payload_json={"location_id": venue, "debate_id": debate_id})
    db.add(ev)
    await db.commit()
    return ev


# ── stage_venue_of ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_venue_is_read_from_the_script_event(db_session):
    """payload 契约由批次表 #7 定:type=\"script\" + {location_id, debate_id}。"""
    d = await _debate(db_session)
    await _script_event(db_session, d.id)
    assert await ds.stage_venue_of(db_session, d.id) == "theater"


@pytest.mark.anyio
async def test_venue_is_none_without_the_event(db_session):
    """今天世界里每一场辩论都是这个形态 —— 降级即今天的行为。"""
    d = await _debate(db_session)
    assert await ds.stage_venue_of(db_session, d.id) is None


@pytest.mark.anyio
async def test_venue_ignores_other_debates_and_other_event_types(db_session):
    d = await _debate(db_session)
    await _script_event(db_session, "some-other-debate")
    db_session.add(WorldEvent(type="news", title="公开课", description="",
                              payload_json={"location_id": "academy",
                                            "debate_id": d.id}))
    await db_session.commit()
    assert await ds.stage_venue_of(db_session, d.id) is None


@pytest.mark.anyio
async def test_venue_tolerates_a_malformed_payload(db_session):
    d = await _debate(db_session)
    db_session.add(WorldEvent(type="script", title="坏行", description="",
                              payload_json={"debate_id": d.id}))
    await db_session.commit()
    assert await ds.stage_venue_of(db_session, d.id) is None


# ── stage_audience ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_masking_is_real_and_the_audience_sees_through_it(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    # 遮蔽是真的:首命中返回的是 outdoor 街区,不是剧院。
    assert get_location_id_at(*INSIDE) == "east_gardens"
    await _resident(db_session, "watcher", INSIDE)
    seats = await ds.stage_audience(db_session, "theater", seed="d1")
    assert [r.slug for r in seats] == ["watcher"]


@pytest.mark.anyio
async def test_people_outside_the_venue_are_not_audience(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _resident(db_session, "passerby", OUTSIDE)
    assert await ds.stage_audience(db_session, "theater", seed="d1") == []


@pytest.mark.anyio
async def test_legacy_row_without_the_declaration_is_inert(db_session, overlay):
    """存量 dynamic_locations 行没有 capabilities 键 —— 未回填时名单必须为空,
    绝不能抛,也绝不能瞎认。"""
    overlay("theater", THEATER)
    await _resident(db_session, "watcher", INSIDE)
    assert await ds.stage_audience(db_session, "theater", seed="d1") == []


@pytest.mark.anyio
async def test_sleepers_and_non_sim_residents_are_excluded(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _resident(db_session, "sleeper", INSIDE, status="sleeping")
    await _resident(db_session, "ugc", INSIDE, rtype="character")
    assert await ds.stage_audience(db_session, "theater", seed="d1") == []


@pytest.mark.anyio
async def test_debaters_are_not_their_own_audience(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _resident(db_session, "ann", INSIDE)
    await _resident(db_session, "watcher", INSIDE)
    seats = await ds.stage_audience(db_session, "theater", seed="d1",
                                    exclude_slugs=("ann", "bo"))
    assert [r.slug for r in seats] == ["watcher"]


@pytest.mark.anyio
async def test_audience_is_capped_and_deterministic(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    for i in range(12):
        await _resident(db_session, f"w{i}", INSIDE)
    first = [r.slug for r in await ds.stage_audience(db_session, "theater", seed="d1")]
    again = [r.slug for r in await ds.stage_audience(db_session, "theater", seed="d1")]
    assert len(first) == ds.AUDIENCE_LIMIT == 8
    assert first == again           # 同 seed 同名单
    other = [r.slug for r in await ds.stage_audience(db_session, "theater", seed="d2")]
    assert set(other) <= {f"w{i}" for i in range(12)}


def test_action_type_enum_is_untouched():
    """P2 全段零新增 ActionType(design_P2.md §「为什么不新增 ActionType」)。"""
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
```

先跑一次拿红。预期失败形态：收集期即报 `AttributeError: module 'app.services.debate_service' has no attribute 'stage_venue_of'`（全文件红）。

#### 实现

改文件：/Volumes/data/dev/simverse-world/backend/app/services/debate_service.py（两处，纯插入）

**改动 1 —— 顶部 import + 常量**。锚点：`debate_service.py:20-23` 的 import 段与 `:30-34` 的常量段。

before：
```python
import logging
from datetime import datetime, timedelta, UTC
```
after：
```python
import hashlib
import logging
from datetime import datetime, timedelta, UTC
```

before（`debate_service.py:31-34`）：
```python
STAKE_MIN = 10
STAKE_MAX = 200
BURN_RATE = 0.05  # 5% of the losing pool is burned on payout
ROUNDS = 6
```
after：
```python
STAKE_MIN = 10
STAKE_MAX = 200
BURN_RATE = 0.05  # 5% of the losing pool is burned on payout
ROUNDS = 6

#: 观众收益的人数上限。关系 bump 是两两 O(n²)(8 人 = 28 次带 commit 的 UPDATE),
#: 但这条路径每场辩论只在 settle 时跑一次、不在 tick 热路径上,所以不需要
#: market_day_crowd_cohort 那套 TTL 缓存 + 单飞锁。
AUDIENCE_LIMIT = 8

#: 反查场地时扫描的 script 事件行数上限。script 事件只有两个产地(#7 的 live 辩论、
#: #8 的公开课),而 settle 发生在 run_live 之后 debate_vote_window_min 内,目标行
#: 必在最新的一批里。上限存在只是为了让这条查询恒定代价。
_VENUE_SCAN_LIMIT = 200
```

**改动 2 —— 两个纯查询**。锚点：`# Helpers` 分隔线（`debate_service.py:464-466`）与 `_resident`（`:467`）之间，追加在 `_resident` 之后（文件末尾）。

after（追加到 `debate_service.py:468` 之后）：
```python


def _stable_audience_rank(seed: str, resident_id: str) -> bytes:
    """稳定排序键(照 crowd_service._stable_market_rank:102-104 的形状)。

    按 id 直接排序会让同几个人永远占满 8 个名额;按 seed(= debate id)加盐后,
    每场辩论的截断名单不同,但同一场重跑恒等 —— settle 幂等要求它是纯函数。
    """
    return hashlib.sha256(f"{seed}\x1f{resident_id}".encode("utf-8")).digest()


async def stage_venue_of(db, debate_id: str) -> str | None:
    """这场辩论的剧院地点 id;没有场地信息则 None(= 今天每一场辩论的形态)。

    Debate 模型没有 location 列,本批次不动 schema(红线:迁移与行为变更不得同一次
    变更)。地点走已有的 WorldEvent 通道,payload 契约由 design_P2.md §②-a 定:
    ``type="script"`` + ``payload_json={"location_id": venue, "debate_id": id}``。

    payload 的过滤放在 Python 侧而不是 SQL:world_events.payload_json 是
    ``sa.JSON()``(PG 上是 json 不是 jsonb),而测试库是 sqlite(JSON 运算符不可用)。
    drive_due_debates 的时间判据同样是 Python 侧过滤(:311-317),口径一致。

    location_id 的读取经 resolve_event_location_id,与 crowd_service.
    active_event_location(:65-76)同一个解析器 —— 否则「人流拉去哪」与「观众算在
    哪」会分叉。
    """
    from app.models.world_event import WorldEvent
    from app.services.event_location import resolve_event_location_id

    rows = (await db.execute(
        select(WorldEvent)
        .where(WorldEvent.type == "script")
        .order_by(WorldEvent.created_at.desc())
        .limit(_VENUE_SCAN_LIMIT)
    )).scalars().all()
    for ev in rows:
        payload = ev.payload_json or {}
        if payload.get("debate_id") != debate_id:
            continue
        loc = resolve_event_location_id(payload)
        return loc if isinstance(loc, str) and loc else None
    return None


async def stage_audience(
    db, venue: str, *, seed: str, exclude_slugs: tuple[str, ...] = (),
) -> list[Resident]:
    """此刻站在 venue 里的清醒自治 sim 居民,至多 AUDIENCE_LIMIT 人。

    「在不在场」用 map_data.capability_location_at 而**不是** get_location_id_at:
    后者首命中即返(map_data.py:243-249),命中序 = dict 插入序 = 静态在前、动态追加
    在尾(:386),而 theater(172,40,178,50) 完全落在 outdoor 街区
    east_gardens(140,35,179,58) 内部 —— 生产实测 get_location_id_at(175,45) 返
    "east_gardens"。照粗查写,观众名单恒为空、#10 静默失效且零告警。

    存量 dynamic_locations 行没有 capabilities 键 → 归一成空 dict → 恒返空名单 →
    调用方走老行为。缺省安全,回填前后都不炸。

    候选集的过滤条件与 crowd_service.market_day_crowd_cohort(:174-180)逐字同款:
    autonomous + resident_type ∈ {npc, resident} + 非 sleeping。UGC 角色
    (resident_type="character")不进名单 —— 观众收益写的是 needs/关系,不该落到
    非 sim 身份上。
    """
    from app.agent.location_caps import CAP_STAGE
    from app.agent.map_data import capability_location_at

    rows = (await db.execute(
        select(Resident).where(
            Resident.is_autonomous,
            Resident.resident_type.in_(["npc", "resident"]),
            Resident.status.not_in(["sleeping"]),
        )
    )).scalars().all()
    present = [
        r for r in rows
        if r.slug not in exclude_slugs
        and capability_location_at(r.tile_x or 0, r.tile_y or 0, CAP_STAGE) == venue
    ]
    present.sort(key=lambda r: _stable_audience_rank(seed, str(r.id)))
    return present[:AUDIENCE_LIMIT]
```

本 step 不改任何其它文件，不引入任何 Settings 字段。

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_debate_stage_audience.py tests/test_debates.py tests/test_debate_driver.py tests/test_capability_location_at.py tests/test_crowd.py tests/test_lab_building.py -q
```

**验收**：1. 实现前红（收集期 `AttributeError: module 'app.services.debate_service' has no attribute 'stage_venue_of'`）。2. 实现后全部 passed、failed=0；`tests/test_debates.py` 与 `tests/test_debate_driver.py` 零改动全绿。3. `test_masking_is_real_and_the_audience_sees_through_it` 同时给出两条实证：`get_location_id_at(175,45) == "east_gardens"`（遮蔽真存在）与 `stage_audience(...) == [watcher]`（能力反查穿透）。4. 零生产调用方：`grep -rn 'stage_venue_of\|stage_audience' app/ | grep -v 'app/services/debate_service.py'` 输出为空。5. 零新 Settings 字段：`git diff app/config.py backend/.env.example deploy/backend/.env.example` 为空。6. `git diff --numstat app/services/debate_service.py` 的 deletions 列为 0（纯插入）。7. `len(list(ActionType)) == 16` 由本文件与 `tests/test_lab_building.py` 双份钉死。

**commit**：

```
feat(debate): 加 stage_venue_of / stage_audience 两个纯查询——能力反查穿透 outdoor 遮蔽
```

### P2-S16 — _resident_aftermath 追加观众收益（记忆/心情/social/关系，零 SC 流动）—— 沿用 STAGE_EVENT_ENABLED

**Flag / 批次**：沿用批次表 #7 的 `STAGE_EVENT_ENABLED`（默认 false，本 step 不引入、不翻）。非迁移批次、非开闸批次；needs 写入额外受既有 `REALISM_ENABLED` 约束。

**为什么**：design_P2.md §②-c：`_resident_aftermath`（`debate_service.py:435-461`）是现成的结算钩子，观众收益全部复用既有系统，四层全部非货币。

**为什么必须明确不发币（本组硬门）**：settle 已经有一条真金链路 —— stake 时 `coin_service.charge` 已从玩家钱包扣走（`:99`），settle 只做重分配：`distributable = int(loser_pool*0.95)`、`burn = loser_pool - distributable`、赢方拿 `amount + int(distributable*amount/winner_pool)`（`:396-414`）。出账恒 ≤ 入账，净销毁 = burn + 取整余数 —— settle 是净 sink 不是铸币口。给观众发 SC 就是开第二条铸币口且无对应 sink，与 5% BURN_RATE 的通缩设计直接对冲，也构成与 settle 分账的双花。`treasury_debit` 是纯销毁、`treasury_transfer` 才是守恒转移 —— 本 step 两个都不用。

**social 最该给**：`needs.social` 恢复会改变 `most_critical`（needs.py:65）与 `_crowd_hint`（decide/basic.py:410），直接治动机侧。needs 写入额外挂 `realism_enabled`：needs 体系本来就归 realism，闸关的世界不该凭空多出 `meta_json["needs"]`。

#### 先写的测试（必须跑出失败）

新建文件：/Volumes/data/dev/simverse-world/backend/tests/test_debate_audience_payoff.py

```python
"""P2-S16: 观众收益 —— 记忆/心情/社交需求/关系四层,零 SC 流动。

经济守恒是本组的硬门,两条独立证据:
  · 源码扫描:_audience_aftermath 的函数体不得出现任何货币符号;
  · 余额快照:走完整条 settle,users.soul_coin_balance 与
    resident_treasuries.balance_sc 的总变化恰等于 -burn(5% BURN_RATE),
    一枚都不多不少 —— 观众路径没有开新出口,也没有与 settle 分账双花。
"""
import inspect

import pytest
from sqlalchemy import select

from app.agent.actions import ActionType
from app.agent.location_caps import CAP_STAGE
from app.agent.map_data import LOCATIONS
from app.agent.needs import get_needs
from app.config import settings
from app.memory.service import MemoryService
from app.models.memory import Memory
from app.models.resident import Resident
from app.models.resident_treasury import ResidentTreasury
from app.models.user import User
from app.models.world_event import WorldEvent
from app.services import debate_service as ds
from app.services import relation_service

THEATER = {
    "name": "剧院", "type": "public", "role": "culture",
    "bounds": (172, 40, 178, 50), "center": (175, 45), "entrance": (172, 45),
    "description": "小镇剧院:说书、演展、故事会的舞台",
    "boosted_actions": ["CHAT_RESIDENT", "OBSERVE"],
}
INSIDE = (175, 45)
OUTSIDE = (75, 56)


@pytest.fixture
def overlay():
    added: list[str] = []

    def _merge(slug: str, data: dict, capabilities=None) -> str:
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


async def _resident(db, slug, tile=INSIDE):
    r = Resident(slug=slug, name=slug, creator_id="system", district="cafe",
                 status="idle", resident_type="npc",
                 tile_x=tile[0], tile_y=tile[1])
    db.add(r)
    await db.commit()
    return r


async def _staged_debate(db, *, with_event=True, watchers=2):
    await _resident(db, "ann", OUTSIDE)
    await _resident(db, "bo", OUTSIDE)
    seats = [await _resident(db, f"w{i}", INSIDE) for i in range(watchers)]
    d = await ds.create_debate(db, "猫和狗谁更好", "ann", "bo")
    if with_event:
        db.add(WorldEvent(type="script", title="辩论", description="",
                          payload_json={"location_id": "theater",
                                        "debate_id": d.id}))
        await db.commit()
    return d, seats


async def _audience_memories(db, resident_id):
    rows = (await db.execute(
        select(Memory).where(Memory.resident_id == resident_id,
                             Memory.source == "debate")
    )).scalars().all()
    return rows


# ── 依赖边守卫 ────────────────────────────────────────────────────────

def test_stage_event_flag_is_registered_by_the_previous_batch():
    from app.config import Settings
    field = Settings.model_fields.get("stage_event_enabled")
    assert field is not None, (
        "app/config.py 缺 stage_event_enabled —— design_P2.md 批次表 #7 必须先引入 "
        "STAGE_EVENT_ENABLED(默认 false)并同 commit 写进 backend/.env.example;"
        "#10/#11 沿用同一道闸,见本计划 notes 的「依赖边 C」")
    assert field.default is False, "新闸必须默认关"


# ── 闸关 = 今天 ───────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_gate_off_changes_nothing(db_session, overlay, monkeypatch):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    monkeypatch.setattr(settings, "stage_event_enabled", False)
    d, seats = await _staged_debate(db_session)
    before = {r.id: dict(r.mood_json or {}) for r in seats}

    await ds._resident_aftermath(db_session, d, "a")

    for r in seats:
        await db_session.refresh(r)
        assert await _audience_memories(db_session, r.id) == []
        assert dict(r.mood_json or {}) == before[r.id]
        assert await relation_service.get_pair(
            db_session, seats[0].id, seats[1].id) is None


# ── 闸开 = 四层非货币收益 ─────────────────────────────────────────────

@pytest.mark.anyio
async def test_audience_gets_memory_mood_social_and_relations(
        db_session, overlay, monkeypatch):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    monkeypatch.setattr(settings, "realism_enabled", True)
    d, seats = await _staged_debate(db_session)
    social_before = {r.id: get_needs(r)["social"] for r in seats}

    await ds._resident_aftermath(db_session, d, "a")

    for r in seats:
        await db_session.refresh(r)
        mems = await _audience_memories(db_session, r.id)
        assert len(mems) == 1 and "剧院" in mems[0].content
        assert float((r.mood_json or {}).get("valence", 0.0)) > 0.0
        assert get_needs(r)["social"] > social_before[r.id]
    pair = await relation_service.get_pair(db_session, seats[0].id, seats[1].id)
    assert pair is not None and pair.familiarity > 0.0


@pytest.mark.anyio
async def test_no_event_no_audience_path(db_session, overlay, monkeypatch):
    """#7 没建 script 事件(= 今天每一场辩论)→ 降级到今天的行为。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    d, seats = await _staged_debate(db_session, with_event=False)

    await ds._resident_aftermath(db_session, d, "a")

    for r in seats:
        assert await _audience_memories(db_session, r.id) == []


@pytest.mark.anyio
async def test_debaters_keep_exactly_their_own_aftermath(
        db_session, overlay, monkeypatch):
    """辩手不是自己的观众:赢家仍然只有一条记忆 + 原来的 +0.3/+0.1 心情。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    d, _ = await _staged_debate(db_session)
    ann = (await db_session.execute(
        select(Resident).where(Resident.slug == "ann"))).scalar_one()
    ann.tile_x, ann.tile_y = INSIDE       # 辩手就站在台上
    await db_session.commit()

    await ds._resident_aftermath(db_session, d, "a")

    mems = await _audience_memories(db_session, ann.id)
    assert len(mems) == 1 and "中赢了" in mems[0].content


# ── 零 SC:两条独立证据 ───────────────────────────────────────────────

def test_audience_path_never_mentions_money():
    src = inspect.getsource(ds._audience_aftermath)
    for token in ("coin_service", "reward", "treasury", "balance_sc",
                  "soul_coin", "charge("):
        assert token not in src, (
            f"_audience_aftermath 出现 {token!r} —— 观众收益必须留在"
            "「记忆/心情/需求/关系」四个非货币层(design_P2.md §②-c)")


@pytest.mark.anyio
async def test_settle_only_burns_and_audience_adds_zero(
        db_session, overlay, monkeypatch):
    """走完整条 settle:总币量变化恰为 -burn,观众一枚都没多拿。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    d, seats = await _staged_debate(db_session)
    db_session.add(ResidentTreasury(resident_slug="w0", balance_sc=42))
    await db_session.commit()

    u1 = User(name="u1", email="u1@d.com", soul_coin_balance=1000)
    u2 = User(name="u2", email="u2@d.com", soul_coin_balance=1000)
    db_session.add_all([u1, u2])
    await db_session.commit()
    await ds.stake(db_session, d.id, u1.id, "a", 100)
    await ds.stake(db_session, d.id, u2.id, "b", 100)
    d.status, d.votes_a, d.votes_b = "voting", 3, 1
    await db_session.commit()

    res = await ds.settle(db_session, d.id)

    assert res["winner"] == "a"
    assert res["loser_pool"] == 100 and res["distributable"] == 95
    assert res["burn"] == 5
    total = sum((await db_session.execute(
        select(User.soul_coin_balance))).scalars().all())
    assert total == 2000 - res["burn"], "settle 只销毁 5%,不铸币"
    treasury = (await db_session.execute(
        select(ResidentTreasury.balance_sc))).scalars().all()
    assert treasury == [42], "观众收益不得动任何居民金库"
    for r in seats:
        assert len(await _audience_memories(db_session, r.id)) == 1


def test_action_type_enum_is_untouched():
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
```

先跑一次拿红。预期失败形态（按 #7 是否已落地分两种，两种都必须先出现）：
- #7 未引入闸 → `test_stage_event_flag_is_registered_by_the_previous_batch` 失败，信息直指 `app/config.py 缺 stage_event_enabled`；
- #7 已引入 → `test_audience_path_never_mentions_money` 抛 `AttributeError: module 'app.services.debate_service' has no attribute '_audience_aftermath'`。

#### 实现

改文件：/Volumes/data/dev/simverse-world/backend/app/services/debate_service.py（两处，纯插入）

**改动 1 —— `_resident_aftermath` 追加第三个 try 块**。锚点：`debate_service.py:455-461`（opinion 那段）之后、`# Helpers` 分隔线（`:464`）之前。前两个 try 块一个字不改。

before：
```python
    except Exception:
        logger.warning("opinion update from settle failed", exc_info=True)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
```

after：
```python
    except Exception:
        logger.warning("opinion update from settle failed", exc_info=True)
    # P2 #10:在场观众的收益。全部落在「记忆/心情/社交需求/关系」四个**非货币**
    # 层 —— settle 已经有一条真金链路(stake 时 charge 已扣走玩家的币 :99,settle
    # 只做重分配:distributable=int(loser_pool*0.95)、burn=loser_pool-distributable
    # :396-414),出账恒 ≤ 入账、净销毁 burn+取整余数,是净 sink 不是铸币口。给观众
    # 发 SC 就是开第二条铸币口且无对应 sink,并与 settle 分账双花。这里一枚不动。
    #
    # 跑在 settle 的 await db.commit()(:405)之后,辩论早已 settled;异常自吞并
    # rollback,既不回染 M7 的生命周期护栏,也不把中断的事务留给上面的
    # opinion_service(照 execute/basic.py:99-103 _charge_meal 的形状)。
    try:
        from app.config import settings
        if settings.stage_event_enabled:
            await _audience_aftermath(db, d, winner)
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        logger.warning("debate audience aftermath failed", exc_info=True)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
```

**改动 2 —— 收益常量 + `_audience_aftermath`**。锚点：`AUDIENCE_LIMIT`（P2-S15 落的常量）之后追加四个常量；`_audience_aftermath` 追加到文件末尾（P2-S15 的 `stage_audience` 之后）。

常量（接在 `AUDIENCE_LIMIT = 8` 之后）：
```python
#: 观众的四层收益系数。全部是既有系统的既有量纲,没有一项是货币:
#: importance 照 _resident_aftermath 里辩手的 0.7/0.6 降一档(design §②-c ≈0.5);
#: 心情照赢家的 +0.3/+0.1 降一档;social 是 needs 的 [0,1] 标度;
#: familiarity 是 relation_service 的 [0,1] 标度。
AUDIENCE_MEMORY_IMPORTANCE = 0.5
AUDIENCE_MOOD_VALENCE = 0.1
AUDIENCE_MOOD_AROUSAL = 0.05
AUDIENCE_SOCIAL_RESTORE = 0.15
AUDIENCE_FAMILIARITY = 0.02
```

函数（追加到 `stage_audience` 之后，文件末尾）：
```python


async def _audience_aftermath(db, d: Debate, winner: str) -> None:
    """在场观众的非货币收益:记忆 / 心情 / 社交需求 / 关系。**零 SC 流动**。

    social 是这四层里最该给的一项:needs.social 恢复会改变 most_critical
    (needs.py:65)与 _crowd_hint(decide/basic.py:410),直接治动机侧 —— 看了一场热闹
    的辩论就该不那么孤独。needs 写入额外挂 realism_enabled:needs 体系本来就归
    realism,闸关的世界不该凭空多出 meta_json["needs"]。

    write_needs 不 commit(needs.py:29-34)且必须整体重赋 meta_json 才触发
    SQLAlchemy 脏检测 —— 所以整批写完统一 commit 一次。
    """
    venue = await stage_venue_of(db, d.id)
    if not venue:
        return
    audience = await stage_audience(
        db, venue, seed=d.id,
        exclude_slugs=(d.resident_a_slug, d.resident_b_slug),
    )
    if not audience:
        return

    from app.agent.map_data import get_location_by_id
    from app.config import settings
    from app.memory.service import MemoryService
    from app.services.mood_service import apply_mood_event
    from app.services.relation_service import bump

    venue_name = (get_location_by_id(venue) or {}).get("name") or venue
    win_slug = d.resident_a_slug if winner == "a" else d.resident_b_slug
    win_res = await _resident(db, win_slug)
    win_name = win_res.name if win_res else ("正方" if winner == "a" else "反方")

    mem = MemoryService(db)
    for r in audience:
        await mem.add_memory(
            r.id, "event",
            f"我在{venue_name}看完了辩论「{d.topic}」,{win_name}赢了。",
            importance=AUDIENCE_MEMORY_IMPORTANCE, source="debate")
        # 已经拿到 ORM 对象,用 apply_mood_event 而不是 ..._by_id,省一次 db.get。
        await apply_mood_event(db, r, AUDIENCE_MOOD_VALENCE, AUDIENCE_MOOD_AROUSAL)

    if settings.realism_enabled:
        from app.agent.needs import get_needs, write_needs
        for r in audience:
            needs = get_needs(r)
            needs["social"] = min(1.0, needs["social"] + AUDIENCE_SOCIAL_RESTORE)
            write_needs(r, needs)
        await db.commit()

    # 同场观众两两加熟。O(n²) 但 n ≤ AUDIENCE_LIMIT(8 → 28 次),且每场辩论只在
    # settle 时跑一次,不在 tick 热路径上。
    for i, r1 in enumerate(audience):
        for r2 in audience[i + 1:]:
            await bump(db, r1.id, r2.id, AUDIENCE_FAMILIARITY, 0.0)
```

本 step 不改任何其它文件，不引入任何 Settings 字段（`stage_event_enabled` 由批次表 #7 引入）。

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_debate_audience_payoff.py tests/test_debate_stage_audience.py tests/test_debates.py tests/test_debate_driver.py tests/test_env_example_consistency.py tests/test_lab_building.py -q
```

**验收**：1. 实现前红，且失败项是上面两种形态之一（若是第一种，先等批次表 #7 落地 `STAGE_EVENT_ENABLED`，再回到本 step）。2. 实现后全绿；`tests/test_debates.py`/`tests/test_debate_driver.py` 零改动全绿。3. 零新 Settings 字段：`git diff app/config.py backend/.env.example deploy/backend/.env.example` 为空，且 `tests/test_env_example_consistency.py` 绿（双向证明本 step 没有偷加字段，也没有留下悬空 env 行）。4. `git diff --numstat app/services/debate_service.py` 的 deletions 列为 0。5. 经济守恒双证：`test_audience_path_never_mentions_money`（源码扫描）+ `test_settle_only_burns_and_audience_adds_zero`（总币量恰为 `2000 - burn`、居民金库分文未动）。6. `len(list(ActionType)) == 16`。

**§⑤ 验收 SQL（本 step 相关的三条，含开闸前后预期值）**：
- **M3 剧院到访**（`memories.metadata_json->'move'->>'target' = 'theater' AND ->>'arrived' = 'true'`，14 天窗）。开闸前：`visits = 0 / people = 0`（生产 15 天实测基线）。开闸后（需 #7+#9 同时开）：`visits ≥ 20 且 people ≥ 6`。**M3 是本 step 的前置条件而非产出** —— M3 为 0 时观众名单恒空，本 step 逐字节等于今天。
- **M4 到访归因**（`world_events WHERE type='script' AND payload_json->>'location_id'='theater'` 左连接到访时间窗）。开闸前：两列皆 0（今天零条 script 事件指向 theater）。开闸后：`during_event / (during_event + off_event) ≥ 0.6`。这条同时验证 #7 建的那条事件行确实存在 —— 它正是 `stage_venue_of` 反查的同一行，M4 为 0 而 M3 非 0 说明场地反查断链。
- **M7 辩论生命周期护栏**（`SELECT count(*) FROM debates WHERE status <> 'settled' AND starts_at < now() - interval '2 days'`）。开闸前后**恒为 0**。本 step 的代码跑在 settle 的 `db.commit()` 之后且异常自吞，结构上不可能让辩论卡住；开闸后此值一旦非 0，是 `drive_due_debates` 的问题，不是本 step 的。
- 补充口径护栏：`SELECT count(*) FROM memories WHERE source='debate' AND created_at >= now() - interval '14 days'` 除以同期 settled 辩论数，开闸前 ≈ 2（两位辩手），开闸后 ≤ 2 + AUDIENCE_LIMIT(8)。超过 10 说明观众去重或 exclude_slugs 失效。

**commit**：

```
feat(debate): 观众收益接进 _resident_aftermath——记忆/心情/social/关系四层,零 SC 流动
```

### P2-S17 — OBSERVE 补 execute 分支 + 非移动动作写 metadata['act']（M5 验收 SQL 的唯一数据源）

**Flag / 批次**：沿用批次表 #7 的 `STAGE_EVENT_ENABLED`（默认 false，本 step 不引入、不翻）。闸关时 execute 的 elif 短路、memorize 不写 `act` 键，与今天逐字节等价。非迁移批次、非开闸批次。

**为什么**：design_P2.md §③ 附带小改。两处各治一个洞：

1. **OBSERVE 在 `execute/basic.py:131-243` 的 switch 里没有任何分支** —— 选了它等于什么都没发生，status 还停在上一 tick 的 `"walking"`（这人对别人来说仍算 `idle_nearby`，自己看上去却在赶路）。剧院 boosted 的两个动作里 OBSERVE 是 always-available 的，补一个纯状态分支成本极低。**不在 execute 里写记忆**：memorize 已经为 OBSERVE 写了一条（`memorize/basic.py:135-136`），再写一条就是同一 tick 双份记忆，污染检索与 `_normalize_importance` 的分位。
2. **`metadata["act"]` 是 M5 的唯一数据源** —— 今天 `metadata_json` 只有 `move`/`plan`/`raw_importance` 三个键，`->'act'` 全表为 NULL，「剧院里到底发生了什么」在 SQL 上完全不可见。

`act.loc` 必须是**具体**地点 id：`get_location_id_at(175,45)` 生产实测返 `"east_gardens"`，照它写 M5 恒查不到一行。先用 stage 能力反查穿透遮蔽、查不到再回落粗查 —— P3 的具体性排序闸开了之后两项等价。

#### 先写的测试（必须跑出失败）

新建文件：/Volumes/data/dev/simverse-world/backend/tests/test_act_observability.py

```python
"""P2-S17: OBSERVE 的 execute 分支 + 非移动动作的 act 痕迹(M5 数据源)。

act.loc 必须是**具体**地点 id:theater(172,40,178,50) 完全落在 outdoor 街区
east_gardens(140,35,179,58) 内部,get_location_id_at 首命中即返(map_data.py:
243-249),生产实测 (175,45) 返 "east_gardens"。照粗查写,M5 的
`metadata_json->'act'->>'loc' = 'theater'` 恒查不到一行。
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.actions import ActionResult, ActionType
from app.agent.location_caps import CAP_STAGE
from app.agent.map_data import LOCATIONS, get_location_id_at
from app.agent.schemas import TickContext
from app.config import settings

THEATER = {
    "name": "剧院", "type": "public", "role": "culture",
    "bounds": (172, 40, 178, 50), "center": (175, 45), "entrance": (172, 45),
    "description": "小镇剧院:说书、演展、故事会的舞台",
    "boosted_actions": ["CHAT_RESIDENT", "OBSERVE"],
}
INSIDE = (175, 45)


@pytest.fixture
def overlay():
    added: list[str] = []

    def _merge(slug: str, data: dict, capabilities=None) -> str:
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


def _ctx(action, *, tile=INSIDE, status="walking"):
    resident = SimpleNamespace(
        id="r-1", slug="watcher", name="看客", resident_type="npc",
        status=status, tile_x=tile[0], tile_y=tile[1],
        meta_json={}, mood_json={}, home_location_id=None,
        home_tile_x=None, home_tile_y=None, daily_plans_json=None,
    )
    return TickContext(
        db=AsyncMock(), resident=resident, world_time="20:00", hour=20,
        schedule_phase="夜晚",
        action_result=ActionResult(action, None, None, "看看"),
        available_actions=[ActionType.OBSERVE, ActionType.CHAT_RESIDENT],
    )


async def _memorize(ctx):
    from app.agent.phases.memorize.basic import BasicMemorizePlugin
    with patch("app.agent.phases.memorize.basic.MemoryService") as MockMS:
        svc = AsyncMock()
        MockMS.return_value = svc
        await BasicMemorizePlugin(params={}).execute(ctx)
    return svc


# ── 依赖边守卫 ────────────────────────────────────────────────────────

def test_stage_event_flag_is_registered_by_the_previous_batch():
    from app.config import Settings
    field = Settings.model_fields.get("stage_event_enabled")
    assert field is not None, (
        "app/config.py 缺 stage_event_enabled —— 批次表 #7 必须先引入 "
        "STAGE_EVENT_ENABLED(默认 false),#10/#11 沿用同一道闸")
    assert field.default is False


# ── OBSERVE 的 execute 分支 ──────────────────────────────────────────

@pytest.mark.anyio
async def test_observe_is_a_no_op_when_the_gate_is_off(monkeypatch):
    """闸关 = 今天:选了 OBSERVE,status 还停在 walking。"""
    from app.agent.phases.execute.basic import BasicExecutePlugin
    monkeypatch.setattr(settings, "stage_event_enabled", False)
    ctx = _ctx(ActionType.OBSERVE)
    await BasicExecutePlugin(params={}).execute(ctx)
    assert ctx.resident.status == "walking"


@pytest.mark.anyio
async def test_observe_settles_the_resident_when_the_gate_is_on(monkeypatch):
    from app.agent.phases.execute.basic import BasicExecutePlugin
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    ctx = _ctx(ActionType.OBSERVE)
    await BasicExecutePlugin(params={}).execute(ctx)
    assert ctx.resident.status == "idle"
    ctx.db.commit.assert_awaited()


@pytest.mark.anyio
async def test_observe_never_interrupts_an_ongoing_chat(monkeypatch):
    from app.agent.phases.execute.basic import BasicExecutePlugin
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    for busy in ("chatting", "socializing"):
        ctx = _ctx(ActionType.OBSERVE, status=busy)
        await BasicExecutePlugin(params={}).execute(ctx)
        assert ctx.resident.status == busy


@pytest.mark.anyio
async def test_observe_writes_no_memory_in_execute(monkeypatch):
    """记忆归 memorize(memorize/basic.py:135-136);execute 再写一条就是双份。"""
    from app.agent.phases.execute import basic as execute_basic
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    assert "MemoryService" not in execute_basic.BasicExecutePlugin.execute.__doc__ or True
    import inspect
    src = inspect.getsource(execute_basic.BasicExecutePlugin.execute)
    assert "add_memory" not in src


# ── metadata["act"] ──────────────────────────────────────────────────

@pytest.mark.anyio
async def test_act_records_the_specific_venue_not_the_masking_block(
        overlay, monkeypatch):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    # 遮蔽是真的。
    assert get_location_id_at(*INSIDE) == "east_gardens"
    svc = await _memorize(_ctx(ActionType.CHAT_RESIDENT))
    meta = svc.add_memory.call_args[1]["metadata_json"]
    assert meta["act"] == {"action": "CHAT_RESIDENT", "loc": "theater"}


@pytest.mark.anyio
async def test_act_falls_back_to_the_coarse_lookup_elsewhere(monkeypatch):
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    svc = await _memorize(_ctx(ActionType.OBSERVE, tile=(88, 104)))
    meta = svc.add_memory.call_args[1]["metadata_json"]
    assert meta["act"] == {"action": "OBSERVE", "loc": "south_quarter"}


@pytest.mark.anyio
async def test_gate_off_writes_no_act_key(monkeypatch):
    monkeypatch.setattr(settings, "stage_event_enabled", False)
    svc = await _memorize(_ctx(ActionType.OBSERVE))
    meta = svc.add_memory.call_args[1]["metadata_json"]
    assert meta is None or "act" not in meta


@pytest.mark.anyio
async def test_movement_keeps_move_and_never_gets_act(monkeypatch):
    """move 与 act 互斥:移动动作的落点已由 move 记录,不重复写。"""
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    monkeypatch.setattr(settings, "realism_enabled", True)
    svc = await _memorize(_ctx(ActionType.VISIT_DISTRICT))
    meta = svc.add_memory.call_args[1]["metadata_json"]
    assert "move" in meta and "act" not in meta


@pytest.mark.anyio
async def test_every_non_movement_action_is_covered(monkeypatch):
    """M5 要能数清剧院里发生了什么 —— 非移动动作必须条条有痕迹。"""
    from app.agent.phases.memorize.basic import _MOVEMENT_ACTIONS
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    for action in ActionType:
        if action in _MOVEMENT_ACTIONS:
            continue
        svc = await _memorize(_ctx(action))
        meta = svc.add_memory.call_args[1]["metadata_json"]
        assert meta["act"]["action"] == action.value, action


def test_action_type_enum_is_untouched():
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
```

先跑一次拿红。预期失败形态：`test_observe_settles_the_resident_when_the_gate_is_on` 断言 `"walking" == "idle"` 失败；`test_act_records_the_specific_venue_not_the_masking_block` 抛 `TypeError: 'NoneType' object is not subscriptable`（今天 metadata 为空字典 → 传 None）。

#### 实现

改两个文件，均为纯插入。

**改文件 1：/Volumes/data/dev/simverse-world/backend/app/agent/phases/execute/basic.py**

锚点：EAT 分支末尾（`execute/basic.py:242-244`）与 `except Exception as e:` 之间。`grep -n 'await _charge_meal' app/agent/phases/execute/basic.py` 应为 1 行，确认锚点唯一。

before：
```python
                if settings.npc_economy_enabled:
                    await _charge_meal(ctx.db, ctx.resident)
        except Exception as e:
```

after：
```python
                if settings.npc_economy_enabled:
                    await _charge_meal(ctx.db, ctx.resident)
            elif action == ActionType.OBSERVE and settings.stage_event_enabled:
                # P2 #11:OBSERVE 今天在这条 switch 里没有任何分支 —— 选了它等于
                # 什么都没发生,status 还停在上一 tick 的 "walking"(这人对别人来说
                # 仍算 idle_nearby,自己看上去却在赶路)。观察是一个「停下来」的动作。
                #
                # **不在这里写记忆**:memorize 已经为 OBSERVE 写了一条
                # (memorize/basic.py:135-136「在X静静地观察着周围的情况」),再写一条
                # 就是同一 tick 双份记忆,污染检索与 _normalize_importance 的分位。
                # 「在哪观察的」由同批的 metadata["act"] 结构化记录。
                if ctx.resident.status not in ("chatting", "socializing"):
                    ctx.resident.status = "idle"
                    await ctx.db.commit()
        except Exception as e:
```

**改文件 2：/Volumes/data/dev/simverse-world/backend/app/agent/phases/memorize/basic.py**

*改动 2a —— 新增 `_act_metadata`*。锚点：`_plan_memory` 结束（`memorize/basic.py:112-119`）与 `format_action_memory`（`:121`）之间。

before：
```python
        "followed": bool(ctx.plan_followed and ctx.action_result.action.value == plan.action),
        "interrupt_reason": getattr(ctx, "plan_interrupt_reason", None),
    }


def format_action_memory(action_result, resident) -> str:
```

after：
```python
        "followed": bool(ctx.plan_followed and ctx.action_result.action.value == plan.action),
        "interrupt_reason": getattr(ctx, "plan_interrupt_reason", None),
    }


def _act_metadata(ctx) -> dict:
    """非移动动作的「做了什么 / 在哪做的」结构化痕迹(P2 #11)。

    今天 metadata_json 只有 move / plan / raw_importance 三个键,「某栋楼里到底
    发生了什么」在 SQL 上完全不可见 —— 这条就是那半份数据。

    ``loc`` 必须是**具体**地点 id 而不是遮蔽它的 outdoor 街区:
    theater(172,40,178,50) 完全落在 east_gardens(140,35,179,58) 内部,而
    get_location_id_at 首命中即返(map_data.py:243-249),生产实测
    get_location_id_at(175,45) 返 "east_gardens"。照粗查写,验收 SQL 的
    ``metadata_json->'act'->>'loc' = 'theater'`` 恒查不到一行。

    先用 stage 能力反查穿透遮蔽,查不到再回落粗查。等 P3 的具体性排序闸
    (LOCATION_SPECIFIC_FIRST_ENABLED)开了,回落项自己就返回 theater,两项等价 ——
    所以这里不是第二份真相源,只是它到位之前的剧院专用穿透。
    """
    from app.agent.location_caps import CAP_STAGE
    from app.agent.map_data import capability_location_at, get_location_id_at

    res = ctx.resident
    x, y = res.tile_x or 0, res.tile_y or 0
    loc_id = capability_location_at(x, y, CAP_STAGE) or get_location_id_at(x, y)
    return {"action": ctx.action_result.action.value, "loc": loc_id}


def format_action_memory(action_result, resident) -> str:
```

*改动 2b —— 接线*。锚点：`BasicMemorizePlugin.execute` 内 `memorize/basic.py:187-190`。

before：
```python
            metadata = {}
            if move_meta:
                metadata["move"] = move_meta
            plan_meta = _plan_memory(ctx)
```

after：
```python
            metadata = {}
            if move_meta:
                metadata["move"] = move_meta
            # P2 #11:非移动动作补一条 act 痕迹。与 move 互斥 —— 移动动作的落点
            # 已由 move 记录(:87-95),重复写会让同一行出现两套地点口径。
            if settings.stage_event_enabled and action not in _MOVEMENT_ACTIONS:
                metadata["act"] = _act_metadata(ctx)
            plan_meta = _plan_memory(ctx)
```

（`action` 已在 `:173` 由 `action = ctx.action_result.action` 绑定，`settings` 与 `_MOVEMENT_ACTIONS` 均为模块级已有符号，无需新增 import。）

本 step 不改任何其它文件，不引入任何 Settings 字段。

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_act_observability.py tests/test_agent_phases.py tests/test_realism_plan_move.py tests/test_debate_audience_payoff.py tests/test_debate_stage_audience.py tests/test_env_example_consistency.py tests/test_lab_building.py -q
```

**验收**：1. 实现前红（`walking != idle` + `TypeError: 'NoneType' object is not subscriptable`）。2. 实现后全绿；`tests/test_agent_phases.py`（含既有 memorize/execute 用例）与 `tests/test_realism_plan_move.py` 零改动全绿 —— 这两个文件是「闸关时逐字节等于今天」的机器证明。3. 零新 Settings 字段：`git diff app/config.py backend/.env.example deploy/backend/.env.example` 为空，且 `tests/test_env_example_consistency.py` 绿。4. `git diff --numstat app/agent/phases/execute/basic.py app/agent/phases/memorize/basic.py` 两行的 deletions 列均为 0（纯插入）。5. `len(list(ActionType)) == 16` 由本文件与 `tests/test_lab_building.py` 双份钉死；`test_every_non_movement_action_is_covered` 参数化跑遍全枚举，新增动作漏接线会当场红。

**§⑤ 验收 SQL（本 step 是 M5 的唯一数据源，含开闸前后预期值）**：
- **M5 社交自锁是否真破了**（`SELECT metadata_json->'act'->>'action', count(*) FROM memories WHERE metadata_json->'act'->>'loc' = 'theater' AND created_at >= now() - interval '14 days' GROUP BY 1`）。**开闸前：零行**——今天 `metadata_json` 只有 `move`/`plan`/`raw_importance` 三个键，`->'act'` 全表为 NULL（`grep -n 'metadata\[' app/agent/phases/memorize/basic.py` 只有 move/plan 两处，可先在本地复核）。开闸后（本 step + #7 + #9 全开）：`CHAT_RESIDENT ≥ 5`，且 `OBSERVE` 应同时出现若干行 —— 只有 OBSERVE 没有 CHAT_RESIDENT 说明人到了但 `idle_nearby` 仍空（回去查 #9 的 cohort 人数），两者皆无说明 M3 就是 0（人根本没到）。
- **M5 的口径陷阱**：`metadata_json` 是 `sa.JSON()`（PG 上是 `json` 非 `jsonb`），`@>` 与 GIN 索引不可用，**必须带 `created_at` 时间窗**；且 realism 开时每条 event 记忆都被塞了 `raw_importance`（`memory/service.py:120-123`），所以 `metadata_json IS NOT NULL` 会全表命中，判存在只能用 `->'act'`。
- **M5 的自检项（不需要剧院也能查，用来分辨「没数据」和「没接线」）**：`SELECT count(*) FROM memories WHERE metadata_json ? 'act' AND created_at >= now() - interval '1 day'`（PG 上 `json` 不支持 `?`，改用 `metadata_json->'act' IS NOT NULL`）。开闸后此值应与同期非移动动作的 tick 数同量级；恒为 0 = 闸没开或 memorize 接线断了，与剧院冷清无关。
- **M3/M4/M7 见 P2-S16 的 acceptance**，本 step 不改变它们的任何一项。

**commit**：

```
feat(agent): OBSERVE 补 execute 状态分支 + 非移动动作写 metadata['act']——M5 观测口打通
```

---

# P3 修复法案落实新建建筑的功能

## P3 修复公投建楼接线 — bite-sized TDD 执行计划（14 step / 14 commit）

## 依赖图

```
批 1（纯代码，全部新闸默认关）
  S1 ──> S2 ──> S3            (同一函数 validate_location_patch，必须串行)
  S4                          (新模块 civic_build，独立)
  S1..S3 + S4 ──> S5 ──> S6   (同改 _add_dynamic_location，串行)
  S6 ──> S10 ──> S11          (同改 civic_service，串行)
  S7                          (lab/apply.reload_world，独立，可与 S1-S6 并行)
  S8                          (map_data + location_tracker，独立，可并行)
  S9                          (town_facts_service，独立，可并行)
  S12                         (env 文档，依赖 S1..S11 全部落地)
批 2（迁移，独立部署批次）
  S13                         (alembic 068，必须在 S7/S8 已开闸并稳定后才跑)
  S13 ──> S14                 (CIVIC_AGENDA 字面量同步，纯代码单独 commit)
```

可并行三组：`{S1→S2→S3}`、`{S4}`、`{S7}`、`{S8}`、`{S9}` 五条互不相交；`S5/S6/S10/S11` 必须在 `S3`+`S4` 之后串行（都写 `civic_service._add_dynamic_location` / `_close_one_tally`）。

## 全量新闸（默认全关）与开闸硬顺序

| flag | 默认 | step |
|---|---|---|
| `WORLD_RELOAD_RESET_PATH_CACHE` | False | S7 |
| `LOCATION_SPECIFIC_FIRST_ENABLED` | False | S8 |
| `CIVIC_BUILD_SCHEMA_ENABLED` | False | S5 |
| `CIVIC_BUILD_VALIDATE_ENABLED` | False | S6 |
| `CIVIC_EFFECT_AUDIT_ENABLED` | False | S10 |
| `CIVIC_BUILD_OPENING_EVENT_ENABLED` | False | S11 |
| `CIVIC_FACTS_PLACES_DYNAMIC_RESERVE` | 0 | S9 |

开闸顺序：① `WORLD_RELOAD_RESET_PATH_CACHE` → ② `LOCATION_SPECIFIC_FIRST_ENABLED`（必须先于任何依赖 `get_location_id_at` 的 P1 能力闸，否则新楼里能力门恒 False，会假报「P1 接线失败」）→ ③ `CIVIC_BUILD_SCHEMA_ENABLED` + `CIVIC_BUILD_VALIDATE_ENABLED` → ④ `CIVIC_EFFECT_AUDIT_ENABLED` / `CIVIC_FACTS_PLACES_DYNAMIC_RESERVE`（可任意序）→ ⑤ `CIVIC_BUILD_OPENING_EVENT_ENABLED`，且**必须同时开 `REALISM_CROWD_ENABLED`**（实测 `deploy/backend/.env.example` 无此行 → 生产取代码默认 False，不开则庆典只进记忆与 prompt、零位移拉力）。

## 已在本机实测确认的基线（写进各 step 的断言）

- `_overlap(post_office, *)` → 仅命中 `south_quarter`(outdoor)；`_overlap(theater, *)` → 仅命中 `east_gardens`(outdoor)
- outdoor 6 条在静态 LOCATIONS 的索引 28–33（尾部），动态楼追加在 34+ → 首命中 `break` 会让「排在 outdoor 之后的真·楼压楼」漏检
- walkable 有效域 x∈[14,173] y∈[12,123]；`(175,45)` walkable=False/reachable=False（两栋楼未 merge 时），`(172,45)`/`(46,100)` reachable=True
- `tests/test_world_governance.py:36` 的 `good` patch 是 `bounds[5,88,15,96] entrance[10,88]` —— x1=5 与 entrance 均在 walkable 域外 → S2/S3 的新规则**必须走 keyword flag**，否则这条既有断言当场判红
- 静态 public 恰 9 条；`alembic` 单 head = `067_market_economy_loop`
- 测试姿势：`cd backend && .venv/bin/python -m pytest ... -q`（系统 python3 缺 prometheus_client 会 collect 崩）

### P3-S1 — validate_location_patch：outdoor 重叠降级为 warning + 扫完不 break + upsert 允许同 slug

**Flag / 批次**：无（纯函数新增 keyword 参数，默认值 = 旧行为；调用点不变，无行为差）

**为什么**：实测 validate_add_location 把邮局/剧院判成 `bounds overlap existing location 'south_quarter'/'east_gardens'`，而这两条命中的**唯一**对象都是 type=outdoor 的大街区——outdoor 是「地面」，楼盖在街区里是常态。同时既有实现在首个命中就 `break`：outdoor 6 条排在静态字面量索引 28-33、动态楼追加在 34+，降级后若保留 break，「先撞上 outdoor 就收工」会漏掉排在它后面的真·楼压楼。公投是 upsert（civic_service.py:927 同 slug 整包覆盖），所以还需要 allow_existing_slug 让同 slug 既不算错也不与自己判重叠。旧入口 validate_add_location 必须逐字节不变（app/routers/admin/world.py:45 与 lab/apply._apply_add_location:140 在用，tests/test_world_governance.py:34 钉死）。

#### 先写的测试（必须跑出失败）

新建 `/Volumes/data/dev/simverse-world/backend/tests/test_civic_build_validation.py`：

```python
"""P3 ②:公投建楼的落库前几何校验(纯函数层)。

validate_add_location(apply.py:50-87) 今天把邮局/剧院判成 bounds overlap ——
命中的唯一对象都是 type="outdoor" 的大街区(south_quarter / east_gardens)。
本文件钉三件事:旧入口逐字节不变、outdoor 降级为 warning 且不 break、
upsert 允许同 slug。
"""
import pytest

from app.agent import map_data
from app.lab.apply import validate_add_location, validate_location_patch

POST_OFFICE = {"slug": "post_office", "data": {
    "name": "邮局", "type": "public", "role": "logistics",
    "bounds": [44, 100, 48, 106], "center": [46, 103], "entrance": [46, 100],
    "description": "小镇邮局:寄信、收件、时间胶囊的中转站",
    "boosted_actions": ["WORK"]}}
THEATER = {"slug": "theater", "data": {
    "name": "剧院", "type": "public", "role": "culture",
    "bounds": [172, 40, 178, 50], "center": [175, 45], "entrance": [172, 45],
    "description": "小镇剧院:说书、演展、故事会的舞台",
    "boosted_actions": ["CHAT_RESIDENT", "OBSERVE"]}}


@pytest.fixture
def locations_snapshot():
    """LOCATIONS 是可变全局;本文件会往尾部塞动态楼,必须快照+还原。"""
    snap = {k: dict(v) for k, v in map_data.LOCATIONS.items()}
    snap_dyn = set(map_data._dynamic_slugs)
    yield map_data.LOCATIONS
    map_data.LOCATIONS.clear()
    map_data.LOCATIONS.update(snap)
    map_data._dynamic_slugs = snap_dyn


def test_legacy_entry_is_byte_identical():
    """旧入口的返回值一个字都不许变(admin 预览 + lab apply 都在读它)。"""
    assert validate_add_location(POST_OFFICE) == [
        "bounds overlap existing location 'south_quarter'"]
    assert validate_add_location(THEATER) == [
        "bounds overlap existing location 'east_gardens'"]
    good = {"slug": "observatory", "data": {
        "name": "天文台", "bounds": [5, 88, 15, 96], "entrance": [10, 88]}}
    assert validate_add_location(good) == []


def test_outdoor_overlap_downgrades_to_warning():
    for patch, block in ((POST_OFFICE, "south_quarter"), (THEATER, "east_gardens")):
        errors, warnings = validate_location_patch(
            patch, outdoor_overlap_is_warning=True)
        assert errors == [], f"{patch['slug']} 是合法选址,不该被 outdoor 街区误杀"
        assert warnings == [f"bounds sit inside outdoor block '{block}'"]


def test_non_outdoor_overlap_stays_an_error():
    """楼压楼才是真冲突:academy(15,18,42,34) 是 public。"""
    patch = {"slug": "x", "data": {"name": "X", "bounds": [20, 20, 30, 30]}}
    errors, warnings = validate_location_patch(
        patch, outdoor_overlap_is_warning=True)
    assert errors == ["bounds overlap existing location 'academy'"]
    assert warnings == []


def test_scan_does_not_stop_at_the_first_outdoor_block(locations_snapshot):
    """east_gardens(索引 32) 排在动态楼(尾部)之前 —— 降级后若还 break,
    压在剧院身上的新楼就查不出来了。"""
    locations_snapshot["theater"] = {**THEATER["data"],
                                     "bounds": (172, 40, 178, 50)}
    patch = {"slug": "annex", "data": {
        "name": "侧厅", "bounds": [174, 44, 177, 48], "entrance": [175, 45]}}
    assert validate_add_location(patch) == [
        "bounds overlap existing location 'east_gardens'"], "legacy 仍是首命中即停"
    errors, warnings = validate_location_patch(
        patch, outdoor_overlap_is_warning=True)
    assert errors == ["bounds overlap existing location 'theater'"]
    assert warnings == ["bounds sit inside outdoor block 'east_gardens'"]


def test_existing_slug_is_an_upsert_when_allowed(locations_snapshot):
    """公投重复执行同一条 effect 是覆盖写,不是冲突;自己也不与自己重叠。"""
    locations_snapshot["theater"] = {**THEATER["data"],
                                     "bounds": (172, 40, 178, 50)}
    assert any("already exists" in e for e in validate_add_location(THEATER))
    errors, warnings = validate_location_patch(
        THEATER, allow_existing_slug=True, outdoor_overlap_is_warning=True)
    assert errors == []
    assert warnings == ["bounds sit inside outdoor block 'east_gardens'"]
```

实现前跑：`ImportError: cannot import name 'validate_location_patch'` → collect 阶段整文件红。

#### 实现

改 `/Volumes/data/dev/simverse-world/backend/app/lab/apply.py`，把 `validate_add_location`（apply.py:50-87）整段替换为下面两个函数（其余行不动）：

```python
def validate_location_patch(
    patch: dict,
    *,
    allow_existing_slug: bool = False,
    outdoor_overlap_is_warning: bool = False,
) -> tuple[list[str], list[str]]:
    """Structural + conflict checks for an add_location patch.

    Returns ``(errors, warnings)`` — empty ``errors`` = may be persisted.
    Checked against the *current* merged LOCATIONS (static + already-applied
    dynamic).

    ``outdoor_overlap_is_warning`` — an ``outdoor`` block is the ground, not a
    building: post_office sits inside south_quarter and theater inside
    east_gardens, and both are legitimate. With the downgrade on, the scan must
    NOT stop at the first hit: the six outdoor blocks are the last static
    entries (indices 28-33) and dynamic buildings are appended after them
    (map_data.py:386), so an early break would hide a real building clash.

    ``allow_existing_slug`` — the civic path is an upsert
    (civic_service.py:927 overwrites data_json for a known slug), so a known
    slug is neither fatal nor an overlap with itself.
    """
    errors: list[str] = []
    warnings: list[str] = []
    slug = patch.get("slug")
    data = patch.get("data") or {}
    if not slug or not isinstance(slug, str):
        errors.append("missing slug")
    elif slug in LOCATIONS and not allow_existing_slug:
        errors.append(f"slug '{slug}' already exists")

    bounds = _norm_bounds(data.get("bounds"))
    if bounds is None:
        errors.append("bounds must be [x1,y1,x2,y2] integers")
    else:
        x1, y1, x2, y2 = bounds
        if not (
            0 <= x1 <= x2 < MAP_WIDTH_TILES
            and 0 <= y1 <= y2 < MAP_HEIGHT_TILES
        ):
            errors.append(f"bounds out of map range or inverted: {bounds}")
        else:
            for other_slug, loc in LOCATIONS.items():
                if allow_existing_slug and other_slug == slug:
                    continue
                ob = _norm_bounds(loc.get("bounds"))
                if not (ob and _overlap(bounds, ob)):
                    continue
                if outdoor_overlap_is_warning and loc.get("type") == "outdoor":
                    warnings.append(
                        f"bounds sit inside outdoor block '{other_slug}'")
                    continue
                errors.append(f"bounds overlap existing location '{other_slug}'")
                if not outdoor_overlap_is_warning:
                    break
            # Spawn reachability heuristic: entrance must be inside the new bbox
            # and not swallowed by another location's bbox.
            entrance = data.get("entrance") or data.get("center")
            if entrance and len(entrance) == 2:
                ex, ey = int(entrance[0]), int(entrance[1])
                if not (x1 <= ex <= x2 and y1 <= ey <= y2):
                    errors.append("entrance/center must lie within bounds")
    if not data.get("name"):
        errors.append("missing name")
    return errors, warnings


def validate_add_location(patch: dict) -> list[str]:
    """Pre-P3 contract kept byte-for-byte: errors only, first overlap wins, a
    known slug is fatal. Callers: lab apply engine + admin proposal preview."""
    errors, _ = validate_location_patch(patch)
    return errors
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_civic_build_validation.py tests/test_world_governance.py -q
```

**验收**：新文件 5 条测试全绿；`tests/test_world_governance.py` 4 条仍全绿（旧入口逐字节不变）；`validate_add_location(POST_OFFICE) == ["bounds overlap existing location 'south_quarter'"]` 与 `validate_location_patch(POST_OFFICE, outdoor_overlap_is_warning=True) == ([], ["bounds sit inside outdoor block 'south_quarter'"])` 同时成立。

**commit**：

```
refactor(lab): validate_location_patch 拆出 errors/warnings——outdoor 重叠降级且扫完不 break,旧入口逐字节不变
```

### P3-S2 — validate_location_patch 增 require_walkable_range：bounds/入口必须落在 [14,173]×[12,123]

**Flag / 批次**：无新增 settings 闸（keyword 参数 `require_walkable_range` 默认 False = 旧行为，调用点在 S6 才接）

**为什么**：既有范围检查只比 MAP_WIDTH_TILES=180 / MAP_HEIGHT_TILES=128（apply.py:23,67-71），theater 的 x2=178 < 180 合法通过；world_geometry.WALKABLE_X_RANGE=range(14,174) 在校验器里零引用。这就是「即便把死码接活，theater 这类越界楼仍然放行」的机制原文。必须走 keyword flag：既有 `tests/test_world_governance.py:36` 的 good patch bounds x1=5 落在 walkable 域外，无条件加规则会当场判红。

#### 先写的测试（必须跑出失败）

追加到 `/Volumes/data/dev/simverse-world/backend/tests/test_civic_build_validation.py` 末尾：

```python
# ── walkable 域越界(S2) ────────────────────────────────────────────────

def test_walkable_range_check_is_opt_in():
    """默认关 = 旧行为:天文台 bounds x1=5 在 walkable 域外,旧入口照样放行。"""
    good = {"slug": "observatory", "data": {
        "name": "天文台", "bounds": [5, 88, 15, 96], "entrance": [10, 88]}}
    assert validate_add_location(good) == []
    errors, _ = validate_location_patch(good, require_walkable_range=True)
    assert errors == [
        "bounds/entrance leave the walkable area [14,173]x[12,123]: (5,88)"]


def test_theater_is_rejected_by_walkable_range():
    """WALKABLE_X_RANGE 上限 173,而剧院 bounds x2=178 / center x=175。
    只比 MAP_WIDTH_TILES=180 的旧规则放行了它 —— 这条就是那道缺口。"""
    errors, _ = validate_location_patch(
        THEATER, allow_existing_slug=True, outdoor_overlap_is_warning=True,
        require_walkable_range=True)
    assert errors == [
        "bounds/entrance leave the walkable area [14,173]x[12,123]: (178,50)"]


def test_post_office_passes_walkable_range():
    errors, warnings = validate_location_patch(
        POST_OFFICE, outdoor_overlap_is_warning=True, require_walkable_range=True)
    assert errors == []
    assert warnings == ["bounds sit inside outdoor block 'south_quarter'"]
```

实现前跑：`TypeError: validate_location_patch() got an unexpected keyword argument 'require_walkable_range'`。

#### 实现

改 `/Volumes/data/dev/simverse-world/backend/app/lab/apply.py`：

1) 第 23 行 import 扩写：
```python
# before
from app.world_geometry import MAP_HEIGHT_TILES, MAP_WIDTH_TILES
# after
from app.world_geometry import (
    MAP_HEIGHT_TILES, MAP_WIDTH_TILES, WALKABLE_X_RANGE, WALKABLE_Y_RANGE,
)
```

2) `validate_location_patch` 签名加一个 keyword 参数（S1 版本的签名之后）：
```python
    require_walkable_range: bool = False,
```

3) 在 S1 实现里 `entrance = data.get("entrance") or data.get("center")` 那段**之后**、`if not data.get("name")` 之前（即仍在 `else:`（bounds 在地图范围内）块的末尾）插入：
```python
            if require_walkable_range:
                wx1, wx2 = min(WALKABLE_X_RANGE), max(WALKABLE_X_RANGE)
                wy1, wy2 = min(WALKABLE_Y_RANGE), max(WALKABLE_Y_RANGE)
                probes = [(x1, y1), (x2, y2)]
                ent = data.get("entrance") or data.get("center")
                if ent and len(ent) == 2:
                    probes.append((int(ent[0]), int(ent[1])))
                for px, py in probes:
                    if not (wx1 <= px <= wx2 and wy1 <= py <= wy2):
                        errors.append(
                            "bounds/entrance leave the walkable area "
                            f"[{wx1},{wx2}]x[{wy1},{wy2}]: ({px},{py})")
                        break
```

4) `validate_location_patch` 的 docstring 追加一段：
```python
    ``require_walkable_range`` — the pre-P3 range check only compared against
    MAP_WIDTH_TILES/MAP_HEIGHT_TILES, so theater (x2=178 < 180) passed while
    WALKABLE_X_RANGE tops out at 173. Off by default: the pre-P3 fixtures
    (tests/test_world_governance.py:36) legitimately sit outside that band.
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_civic_build_validation.py tests/test_world_governance.py -q
```

**验收**：8 条新测试全绿；`tests/test_world_governance.py` 仍 4 passed；`validate_location_patch(THEATER, ..., require_walkable_range=True)` 的 errors 恰含 `(178,50)` 那条，而 `POST_OFFICE` 的 errors 为空。

**commit**：

```
feat(lab): 建楼校验补 walkable 域越界规则——挂 keyword 开关,默认关不动既有 fixture
```

### P3-S3 — validate_location_patch 增 require_reachable_entrance：入口须落在 town hub 连通分量上 🔧

**Flag / 批次**：无新增 settings 闸（keyword 参数 `require_reachable_entrance` 默认 False = 旧行为）

**为什么**：pathfinder._get_forced_walkable(pathfinder.py:60-68) 无条件把每个地点的 entrance/center 塞进 walkable 集合，所以用 get_walkable_tiles() 校验会自证成功。实测 theater center (175,45) walkable=True 但 reachable=False、find_path 返 None —— 唯一正确的判据是 get_reachable_tiles()（pathfinder.py:111，从 central_plaza BFS 的连通分量）。取值口径必须与 get_valid_target_tile(map_data.py:453) 一致：entrance 存在就永不回退 center。评估必须在把新楼并进 LOCATIONS **之前**做，否则 forced_walkable 会把它自己的入口塞进去自证。

#### 先写的测试（必须跑出失败）

追加到 `/Volumes/data/dev/simverse-world/backend/tests/test_civic_build_validation.py` 末尾（并在文件顶部 import 段加 `from app.agent import pathfinder`）：

```python
# ── 入口可达性(S3) ─────────────────────────────────────────────────────

@pytest.fixture
def fresh_path_cache(locations_snapshot):
    """pathfinder 的 walkable/reachable 是 module-global 缓存,别的测试改过
    LOCATIONS 会串味 —— 用还原后的静态地图重算一次。"""
    pathfinder.reset_walkable_cache()
    yield
    pathfinder.reset_walkable_cache()


def test_reachable_entrance_check_is_opt_in(fresh_path_cache):
    """天文台入口(10,88) 在 walkable 域外 → 不可达;默认关时旧入口照样放行。"""
    good = {"slug": "observatory", "data": {
        "name": "天文台", "bounds": [5, 88, 15, 96], "entrance": [10, 88]}}
    assert validate_add_location(good) == []
    errors, _ = validate_location_patch(good, require_reachable_entrance=True)
    assert errors == ["entrance (10, 88) is not reachable from the town hub"]


def test_post_office_entrance_is_reachable(fresh_path_cache):
    errors, _ = validate_location_patch(
        POST_OFFICE, outdoor_overlap_is_warning=True,
        require_walkable_range=True, require_reachable_entrance=True)
    assert errors == [], "邮局入口(46,100) 实测可达,不该被任何一条新规则挡住"


def test_walkable_set_would_self_certify_but_reachable_does_not(fresh_path_cache):
    """判据必须是 get_reachable_tiles 而不是 get_walkable_tiles:剧院 center
    (175,45) 被 _get_forced_walkable 无条件强标,只有连通分量能戳穿它。"""
    locations_snapshot_center = (175, 45)
    assert locations_snapshot_center not in pathfinder.get_reachable_tiles()
    errors, _ = validate_location_patch(
        {"slug": "isle", "data": {"name": "孤岛",
                                  "bounds": [4, 4, 8, 8], "entrance": [6, 6]}},
        require_reachable_entrance=True)
    assert errors == ["entrance (6, 6) is not reachable from the town hub"]


def test_missing_entrance_is_rejected_when_reachability_required(fresh_path_cache):
    errors, _ = validate_location_patch(
        {"slug": "blob", "data": {"name": "无门之楼", "bounds": [20, 90, 24, 94]}},
        require_reachable_entrance=True)
    assert errors == ["missing entrance/center"]
```

实现前跑：`TypeError: ... unexpected keyword argument 'require_reachable_entrance'`。

#### 实现

改 `/Volumes/data/dev/simverse-world/backend/app/lab/apply.py`：

1) `validate_location_patch` 签名再加一个 keyword 参数：
```python
    require_reachable_entrance: bool = False,
```

2) 在 S2 插入的 `if require_walkable_range:` 块**之后**（仍在 bounds-in-map-range 的 `else:` 块内）追加：
```python
            if require_reachable_entrance:
                ent = data.get("entrance") or data.get("center")
                if not ent or len(ent) != 2:
                    errors.append("missing entrance/center")
                else:
                    # get_walkable_tiles() force-adds every location's
                    # entrance/center (pathfinder.py:60-68) and would
                    # self-certify; only the hub-connected component tells the
                    # truth (theater's center is walkable but unreachable).
                    # Evaluated BEFORE the new row is merged into LOCATIONS, so
                    # this asks "does the door land on the existing town?".
                    from app.agent.pathfinder import get_reachable_tiles
                    tile = (int(ent[0]), int(ent[1]))
                    if tile not in get_reachable_tiles():
                        errors.append(
                            f"entrance {tile} is not reachable from the town hub")
```

3) docstring 追加：
```python
    ``require_reachable_entrance`` — uses get_reachable_tiles(), never
    get_walkable_tiles(). Same pick order as map_data.get_valid_target_tile:
    entrance wins, center is only the fallback.
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_civic_build_validation.py tests/test_world_governance.py tests/test_pathfinder.py -q
```

**验收**：12 条新测试全绿；`tests/test_world_governance.py` / `tests/test_pathfinder.py` 零新增失败；`validate_location_patch(POST_OFFICE, outdoor_overlap_is_warning=True, require_walkable_range=True, require_reachable_entrance=True)` 的 errors 为空列表，而入口 (10,88) 与 (6,6) 各自被拒。

**commit**：

```
feat(lab): 建楼校验补入口可达性——判据用 get_reachable_tiles,不用会自证的 walkable 集
```

> #### 🔧 本 step 已被 critic 修订（1 处）
>
> 执行时以下列补丁为准，逐条覆盖上文对应字段。
>
> **修订 1 — 🟠 major · 字段 `test_first`**
>
> 处置：critic：「docstring 声称要证明剧院 center 被 _get_forced_walkable 强标，但 fixture 链只到 locations_snapshot，theater 根本没被并进 LOCATIONS…断言通过的原因和它宣称证明的事实完全不是一回事」
>
> 定位锚点：
>
> ```
> test_walkable_set_would_self_certify_but_reachable
> ```
>
> 替换为：
>
> 【替换 anchor 那条测试的整体（函数签名 + docstring + 全部断言），改成下面这条；同时删掉悬空变量 locations_snapshot_center】
>
> ```python
> THEATER_CENTER = (175, 45)
>
>
> def test_walkable_set_would_self_certify_but_reachable_does_not(fresh_path_cache):
>     """必须先把剧院并进 LOCATIONS 才谈得上「自证」:_get_forced_walkable
>     (pathfinder.py:60-68) 只对 LOCATIONS 里的 entrance/center 强标。不并进去时
>     (175,45) 是 walkable=False/reachable=False,原断言恒真但跟 forced-walkable
>     机制毫无关系 —— 那是伪证据。"""
>     map_data.LOCATIONS["theater"] = {**THEATER["data"],
>                                      "bounds": (172, 40, 178, 50),
>                                      "center": (175, 45),
>                                      "entrance": (172, 45)}
>     pathfinder.reset_walkable_cache()
>     assert THEATER_CENTER in pathfinder.get_walkable_tiles(), \
>         "forced_walkable 会把它自己的 center 无条件塞进 walkable(自证)"
>     assert THEATER_CENTER not in pathfinder.get_reachable_tiles(), \
>         "hub 连通分量才戳得穿:find_path 到它实测返 None"
>     # 拿这枚「walkable 但不可达」的 tile 当新楼入口 —— 实现前因缺
>     # require_reachable_entrance 关键字必然 TypeError 红,实现后必须被拒。
>     errors, _ = validate_location_patch(
>         {"slug": "isle", "data": {"name": "孤岛",
>                                   "bounds": [174, 40, 178, 50],
>                                   "entrance": [175, 45]}},
>         require_reachable_entrance=True)
>     assert errors == [
>         "entrance (175, 45) is not reachable from the town hub"]
> ```
>
> （`fresh_path_cache` 依赖 `locations_snapshot`，退出时先还原 LOCATIONS 再 reset 缓存，不会串味。）
>

### P3-S4 — 新模块 civic_build.normalize_location_data：白名单投影 + type 缺省 + 非法动作码剔除 🔧

**Flag / 批次**：无（纯函数新模块，零调用方；S5 才接线）

**为什么**：_add_dynamic_location:923 把 effect.data 除 slug 外整包落库、load_dynamic_locations:379 又整包塞进 LOCATIONS，中间无人看过这些键。两个真实爆点：① 缺 `type` 的一行会让 format_location_list_for_prompt 的硬下标 loc["type"](map_data.py:429) 抛 KeyError，把全镇当天 planner 打爆；② boosted_actions 被 prompts.py:80 直接 join 进 system prompt 且不校验成员，公投能造出 ["DANCE"] → LLM 照抄后被 parse_action_result(schemas.py:124-127) 静默丢弃整 tick。策略是丢弃+warning 而非整条拒绝：拒绝会让「新字段先落库、代码后上线」的部署顺序把合法行判成非法。research 能力走 denylist 硬挡（公投不得绕过实验楼的身份门）。

#### 先写的测试（必须跑出失败）

新建 `/Volumes/data/dev/simverse-world/backend/tests/test_civic_build_payload.py`：

```python
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


def test_research_capability_cannot_be_granted_by_referendum():
    """能力声明能被公投携带,但 research 是身份门(实验楼)的另一半,
    公投授予它等于绕过 has_trusted_lab_access。"""
    clean, warns = normalize_location_data(
        {**POST_OFFICE_DATA, "capabilities": ["postal", "research", "", 3]})
    assert clean["capabilities"] == ["postal"]
    assert warns == ["dropped 3 disallowed capabilities"]


def test_capabilities_pass_through_when_allowed():
    clean, warns = normalize_location_data(
        {**POST_OFFICE_DATA, "capabilities": ["postal", "stage"]})
    assert clean["capabilities"] == ["postal", "stage"]
    assert warns == []


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
```

实现前跑：`ModuleNotFoundError: No module named 'app.services.civic_build'`。

#### 实现

新建 `/Volumes/data/dev/simverse-world/backend/app/services/civic_build.py`（全文）：

```python
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

#: 允许落进 dynamic_locations.data_json 的键(与 LOCATIONS 条目同构)。
ALLOWED_KEYS = frozenset({
    "name", "type", "role", "bounds", "center", "entrance", "description",
    "boosted_actions", "category", "capabilities", "indoor", "capacity",
    "duty_keys", "office_key", "opening_event_days",
})

LOCATION_TYPES = ("public", "private", "apartment", "outdoor")
DEFAULT_TYPE = "public"
MAX_NAME_CHARS = 20
MAX_DESCRIPTION_CHARS = 200
MAX_LIST_ITEMS = 6
MAX_CAPABILITY_CHARS = 24

#: 公投永远不能授予的能力。``research`` 是实验楼地点门的另一半
#: (actions.py:127-130),公投授予它等于绕过 has_trusted_lab_access。
FORBIDDEN_CAPABILITIES = frozenset({"research", "lab", "admin"})


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
        if not isinstance(raw, list):
            clean["capabilities"] = []
            warnings.append("capabilities must be a list")
        else:
            kept = [c for c in raw
                    if isinstance(c, str) and c
                    and len(c) <= MAX_CAPABILITY_CHARS
                    and c not in FORBIDDEN_CAPABILITIES]
            kept = kept[:MAX_LIST_ITEMS]
            if len(kept) != len(raw):
                warnings.append(
                    f"dropped {len(raw) - len(kept)} disallowed capabilities")
            clean["capabilities"] = kept

    return clean, warnings
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_civic_build_payload.py -q
```

**验收**：10 条测试全绿；`normalize_location_data(POST_OFFICE_DATA)` 返回的 clean 与输入逐键相等且 warns 为空（生产两条 agenda 载荷零改写）；`capabilities=["postal","research"]` 的结果恒不含 "research"。

**commit**：

```
feat(civic): 新增建楼载荷白名单投影——补 type 缺省、剔非法动作码、公投不得授予 research 能力
```

> #### 🔧 本 step 已被 critic 修订（2 处）
>
> 执行时以下列补丁为准，逐条覆盖上文对应字段。
>
> **修订 1 — 🔴 blocker · 字段 `implementation`**
>
> 处置：critic blocker：「以 P1-S1 的注册表为唯一真值：P3-S4 删掉 FORBIDDEN_CAPABILITIES，改为 from app.agent.location_caps import normalize_capabilities, CIVIC_GRANTABLE_CAPABILITIES，先归一成 dict 再按白名单过滤，输出保持 dict 形态」+「host_duty 设为 dining 能力的必填参数，缺失时丢掉 dining 并回 warning」
>
> 定位锚点：
>
> ```
> FORBIDDEN_CAPABILITIES = frozenset({"research",
> ```
>
> 替换为：
>
> 【① 删掉 anchor 所在的 FORBIDDEN_CAPABILITIES 常量块与 MAX_CAPABILITY_CHARS（已无消费方），改成在模块顶部 import 段引入 P1-S1 的注册表】
>
> ```python
> # capabilities 的唯一真值是 P1-S1 的闭集注册表,本模块绝不自立词表
> # (两套词表必然漂移:黑名单挡不住未来新增的非 civic_grantable 能力)。
> # location_caps 不 import 任何 app 模块,顶层 import 无环。
> from app.agent.location_caps import (
>     CAP_DINING, CIVIC_GRANTABLE_CAPABILITIES, normalize_capabilities,
> )
> ```
>
> 【② 把函数体里 `if "capabilities" in clean:` 那整段替换为】
>
> ```python
>     if "capabilities" in clean:
>         raw = clean["capabilities"]
>         if not isinstance(raw, (dict, list, tuple, set, frozenset)):
>             clean["capabilities"] = {}
>             warnings.append("capabilities must be a dict or list")
>         else:
>             # 先归一成规范形态 dict[str, dict](list -> 参数空字典),再按
>             # civic_grantable 白名单过滤 —— research/market 与任何未登记名
>             # 天然被挡掉,不需要黑名单。输出保持 dict 形态,P1-S7 读得到
>             # {"dining": {"host_duty": ...}} 这类参数。
>             caps = normalize_capabilities(raw)
>             kept = {n: p for n, p in caps.items()
>                     if n in CIVIC_GRANTABLE_CAPABILITIES}
>             dropped = len(raw) - len(kept)
>             # 缺 host_duty 的 dining 会让餐费兜底走 coin_service.treasury_debit
>             # (纯销毁、无对手方),所以整项丢掉 —— 丢键不拒条,但不留销毁口。
>             if CAP_DINING in kept and not kept[CAP_DINING].get("host_duty"):
>                 kept.pop(CAP_DINING)
>                 dropped += 1
>                 warnings.append("dining without host_duty dropped")
>             if dropped > 0:
>                 warnings.append(f"dropped {dropped} disallowed capabilities")
>             clean["capabilities"] = kept
> ```
>
> **修订 2 — 🔴 blocker · 字段 `test_first`**
>
> 处置：critic blocker：「P3-S4 的测试同步改成：{"dining":{"host_duty":"x"}} 原样通过、["dining"] 归一成 {"dining":{}}、["research"]/["postal"] 一律被丢并回 warning…再补一条跨模块对拍」
>
> 定位锚点：
>
> ```
> def test_research_capability_cannot_be_granted_by_referendum
> ```
>
> 替换为：
>
> 【替换 anchor 那条与紧随其后的 test_capabilities_pass_through_when_allowed 两条，改成下面五条】
>
> ```python
> def test_capabilities_canonical_dict_form_passes_through():
>     clean, warns = normalize_location_data(
>         {**POST_OFFICE_DATA,
>          "capabilities": {"dining": {"host_duty": "cafe_host"}}})
>     assert clean["capabilities"] == {"dining": {"host_duty": "cafe_host"}}
>     assert warns == []
>
>
> def test_list_form_is_normalized_to_the_canonical_dict():
>     clean, warns = normalize_location_data(
>         {**POST_OFFICE_DATA, "capabilities": ["postal", "stage"]})
>     assert clean["capabilities"] == {"postal": {}, "stage": {}}
>     assert warns == []
>
>
> def test_research_market_and_unregistered_names_are_dropped():
>     """research 是实验楼身份门的另一半;market 的 civic_grantable=False;
>     未登记名由闭集注册表直接丢掉 —— 三者都不该落库。"""
>     clean, warns = normalize_location_data(
>         {**POST_OFFICE_DATA,
>          "capabilities": ["postal", "research", "market", "wat"]})
>     assert clean["capabilities"] == {"postal": {}}
>     assert warns == ["dropped 3 disallowed capabilities"]
>
>
> def test_dining_without_host_duty_is_dropped_not_kept():
>     """缺 host_duty 的 dining 会让餐费兜底走 treasury_debit(纯销毁)。"""
>     clean, warns = normalize_location_data(
>         {**POST_OFFICE_DATA, "capabilities": ["dining"]})
>     assert clean["capabilities"] == {}
>     assert "dining without host_duty dropped" in warns
>
>
> def test_the_whitelist_is_the_registry_itself():
>     """跨模块对拍:civic_build 不得自带第二份词表。"""
>     from app.agent import location_caps
>     from app.services import civic_build
>     assert (civic_build.CIVIC_GRANTABLE_CAPABILITIES
>             is location_caps.CIVIC_GRANTABLE_CAPABILITIES)
>     assert location_caps.CIVIC_GRANTABLE_CAPABILITIES == frozenset(
>         {"dining", "postal", "stage"})
> ```
>
> 实现前额外红点：`ImportError: cannot import name 'CIVIC_GRANTABLE_CAPABILITIES' from 'app.services.civic_build'`（P1-S1 未登记 postal/stage 时最后一条会指名报出依赖边）。
>

### P3-S5 — _add_dynamic_location 接入载荷净化（CIVIC_BUILD_SCHEMA_ENABLED，默认关） 🔧

**Flag / 批次**：`CIVIC_BUILD_SCHEMA_ENABLED`，默认 `False`

**为什么**：把 S4 的纯函数接到公投唯一落库点。注意另一条入口：routers/polls.py:92-98 的 admin propose 允许直接塞任意 effect dict（ProposeOption.effect: dict | None），所以校验必须做在 _add_dynamic_location 而不是 CIVIC_AGENDA 侧。闸关时 data 原样透传 = 逐字节旧行为。

#### 先写的测试（必须跑出失败）

新建 `/Volumes/data/dev/simverse-world/backend/tests/test_civic_build_wiring.py`：

```python
"""P3 ①⑤:公投落库点 _add_dynamic_location 的净化与校验接线。"""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.dynamic_location import DynamicLocation
from app.services import civic_service

POST_OFFICE_DATA = {
    "slug": "post_office", "name": "邮局", "type": "public", "role": "logistics",
    "bounds": [44, 100, 48, 106], "center": [46, 103], "entrance": [46, 100],
    "description": "小镇邮局:寄信、收件、时间胶囊的中转站",
    "boosted_actions": ["WORK"],
}


@pytest.fixture(autouse=True)
def no_world_reload(monkeypatch):
    """_add_dynamic_location 尾部会 reload_world + publish(懒 import),测试里
    不该去碰全局 engine 与 Redis。"""
    monkeypatch.setattr("app.lab.apply.reload_world", AsyncMock(return_value=0))
    monkeypatch.setattr("app.lab.apply.publish_world_reload", AsyncMock())


async def _stored(db, slug="post_office"):
    return (await db.execute(
        select(DynamicLocation).where(DynamicLocation.slug == slug)
    )).scalar_one_or_none()


@pytest.mark.anyio
async def test_gate_off_persists_the_payload_verbatim(db_session):
    data = {**POST_OFFICE_DATA, "wallet": 999, "boosted_actions": ["DANCE"]}
    assert await civic_service._add_dynamic_location(db_session, data) is True
    row = await _stored(db_session)
    assert row.data_json["wallet"] == 999
    assert row.data_json["boosted_actions"] == ["DANCE"]
    assert "slug" not in row.data_json


@pytest.mark.anyio
async def test_gate_on_strips_unknown_keys_and_bogus_actions(db_session, monkeypatch):
    monkeypatch.setattr(settings, "civic_build_schema_enabled", True)
    data = {**POST_OFFICE_DATA, "wallet": 999,
            "boosted_actions": ["WORK", "DANCE"]}
    assert await civic_service._add_dynamic_location(db_session, data) is True
    row = await _stored(db_session)
    assert "wallet" not in row.data_json
    assert row.data_json["boosted_actions"] == ["WORK"]
    assert "slug" not in row.data_json


@pytest.mark.anyio
async def test_gate_on_backfills_missing_type(db_session, monkeypatch):
    """缺 type 的一行会让计划 prompt 的 loc['type'] 硬下标打爆全镇 planner。"""
    monkeypatch.setattr(settings, "civic_build_schema_enabled", True)
    data = {k: v for k, v in POST_OFFICE_DATA.items() if k != "type"}
    assert await civic_service._add_dynamic_location(db_session, data) is True
    assert (await _stored(db_session)).data_json["type"] == "public"


@pytest.mark.anyio
async def test_gate_on_still_rejects_a_payload_without_bounds(db_session, monkeypatch):
    monkeypatch.setattr(settings, "civic_build_schema_enabled", True)
    assert await civic_service._add_dynamic_location(
        db_session, {"slug": "x", "name": "X"}) is False
    assert await _stored(db_session, "x") is None
```

实现前：`test_gate_on_*` 三条红（unknown key 仍在库里 / type 未补）。

#### 实现

1) `/Volumes/data/dev/simverse-world/backend/app/config.py` —— 在 `civic_poll_days: int = 3`（:684）之后新增：
```python
    # --- P3 公投建楼接线(每道闸一个独立回滚面,默认全关) -----------------
    # 关 = 逐字节旧行为。开闸硬顺序见 deploy/backend/.env.example。
    civic_build_schema_enabled: bool = False   # effect.data 白名单投影 + type 缺省
```

2) `/Volumes/data/dev/simverse-world/backend/app/services/civic_service.py` —— `_add_dynamic_location`（:913-919）改：
```python
# before
    from app.models.dynamic_location import DynamicLocation
    slug = data.get("slug")
    if not slug or "bounds" not in data:
        return False
# after
    from app.models.dynamic_location import DynamicLocation
    slug = data.get("slug")
    if not slug or "bounds" not in data:
        return False
    if settings.civic_build_schema_enabled:
        # routers/polls.py:92-98 允许 admin 直接塞任意 effect dict,所以净化必须
        # 挂在落库点而不是 CIVIC_AGENDA 侧。丢键不拒条:拒绝会让「新字段先落库、
        # 代码后上线」的部署顺序把合法行判成非法。
        from app.services.civic_build import normalize_location_data
        data, _schema_warns = normalize_location_data(data)
        for _w in _schema_warns:
            logger.warning("civic build payload normalized (%s): %s", slug, _w)
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_civic_build_wiring.py tests/test_civic_build_payload.py -q
```

**验收**：4 条接线测试全绿；闸关时 data_json 含 `wallet=999` 与 `boosted_actions=["DANCE"]`（逐字节旧行为），闸开时两者分别消失/被剔为 `["WORK"]`；两种闸态下 `data_json` 均不含 `slug` 键。

**commit**：

```
feat(civic): 建楼载荷净化接进 _add_dynamic_location——挂 CIVIC_BUILD_SCHEMA_ENABLED,默认关逐字节旧行为
```

> #### 🔧 本 step 已被 critic 修订（2 处）
>
> 执行时以下列补丁为准，逐条覆盖上文对应字段。
>
> **修订 1 — 🔴 blocker · 字段 `implementation`**
>
> 处置：critic blocker：「七个 step 各在 config.py 新增一个 Settings 字段，但没有一个改 .env.example…每一个都带着已知红入库…把 env 文档拆散回各自 step」
>
> 定位锚点：
>
> ```
> civic_build_schema_enabled: bool = False   # effect.data
> ```
>
> 替换为：
>
> 【anchor 所在的 config.py 块保留原样；在它与「2) civic_service.py」之间插入新的一节 1b，本 step 同一 commit 内必须同时改两份 env 模板】
>
> 1b) 同 commit 补 env 文档 —— 漏了这步本 commit 当场带红入库：`tests/test_env_example_consistency.py:167` 断言 `set(Settings.model_fields) - (_example_keys() | UNDOCUMENTED_OK)` 为空；`CIVIC_` 前缀还命中 `:375` 的 `GOVERNANCE_PREFIXES` deploy parity，必须双写。
>
> `/Volumes/data/dev/simverse-world/backend/.env.example` 与 `/Volumes/data/dev/simverse-world/deploy/backend/.env.example` 各在 `CIVIC_FACTS_ENABLED=false` 之后追加（两份逐字一致）：
>
> ```
> # 公投/Lab 建楼载荷的白名单投影 + type 缺省 + 非法动作码剔除。
> # 关(默认) = effect.data 除 slug 外整包落库 = 逐字节旧行为。
> # 开 = 未登记键被丢弃并记 warning;capabilities 归一成 dict 后按
> #      location_caps.CIVIC_GRANTABLE_CAPABILITIES 白名单过滤。
> CIVIC_BUILD_SCHEMA_ENABLED=false
> ```
>
> **修订 2 — 🔴 blocker · 字段 `verify_cmd`**
>
> 处置：critic blocker：「七个 step 的 verify_cmd 全部追加该测试文件」（否则本 step 自报绿、把红留给全量跑）
>
> 定位锚点：
>
> ```
> tests/test_civic_build_payload.py -q
> ```
>
> 替换为：
>
> tests/test_civic_build_payload.py tests/test_env_example_consistency.py -q
>

### P3-S6 — _add_dynamic_location 接入几何校验（CIVIC_BUILD_VALIDATE_ENABLED，默认关） 🔧

**Flag / 批次**：`CIVIC_BUILD_VALIDATE_ENABLED`，默认 `False`

**为什么**：把 S1-S3 的三条规则接到落库点，error 拒收返 False（走 _close_one_tally:748 的既有失败公告），warning 只记日志。四个参数必须成套：allow_existing_slug=True（公投是 upsert）、outdoor_overlap_is_warning=True（否则邮局这类合法楼被误杀）、require_walkable_range=True、require_reachable_entrance=True。校验必须在读/写 DB **之前**（可达性一旦并进 LOCATIONS 就会被 forced_walkable 自证），且绝不能让 ApplyError 冒到 _execute_outcome 的 except Exception（会被吞成不可区分的 False）——validate_location_patch 本身不抛，这条自然成立。

#### 先写的测试（必须跑出失败）

追加到 `/Volumes/data/dev/simverse-world/backend/tests/test_civic_build_wiring.py` 末尾（顶部 import 段补 `from app.agent import pathfinder`）：

```python
# ── 几何校验(S6) ───────────────────────────────────────────────────────

THEATER_DATA = {
    "slug": "theater", "name": "剧院", "type": "public", "role": "culture",
    "bounds": [172, 40, 178, 50], "center": [175, 45], "entrance": [172, 45],
    "description": "小镇剧院:说书、演展、故事会的舞台",
    "boosted_actions": ["CHAT_RESIDENT", "OBSERVE"],
}


@pytest.fixture
def validate_on(monkeypatch):
    monkeypatch.setattr(settings, "civic_build_validate_enabled", True)
    pathfinder.reset_walkable_cache()
    yield
    pathfinder.reset_walkable_cache()


@pytest.mark.anyio
async def test_post_office_still_lands_with_validation_on(db_session, validate_on):
    """合法楼不许被 outdoor 街区误杀 —— 这条是整个 P3 的回归红线。"""
    assert await civic_service._add_dynamic_location(
        db_session, dict(POST_OFFICE_DATA)) is True
    assert await _stored(db_session) is not None


@pytest.mark.anyio
async def test_out_of_walkable_bounds_are_refused(db_session, validate_on):
    """剧院 bounds x2=178 越过 WALKABLE_X_RANGE 上限 173。"""
    assert await civic_service._add_dynamic_location(
        db_session, dict(THEATER_DATA)) is False
    assert await _stored(db_session, "theater") is None


@pytest.mark.anyio
async def test_unreachable_entrance_is_refused(db_session, validate_on):
    data = {"slug": "observatory", "name": "天文台", "type": "public",
            "bounds": [5, 88, 15, 96], "entrance": [10, 88]}
    assert await civic_service._add_dynamic_location(db_session, data) is False
    assert await _stored(db_session, "observatory") is None


@pytest.mark.anyio
async def test_building_on_building_is_refused(db_session, validate_on):
    """楼压楼(academy 15,18,42,34)才是真冲突。"""
    data = {"slug": "annex", "name": "侧楼", "type": "public",
            "bounds": [20, 20, 30, 30], "entrance": [25, 25]}
    assert await civic_service._add_dynamic_location(db_session, data) is False
    assert await _stored(db_session, "annex") is None


@pytest.mark.anyio
async def test_gate_off_keeps_landing_the_bad_geometry(db_session):
    """闸关 = 旧行为:剧院照样落库(这就是生产今天的状态)。"""
    assert await civic_service._add_dynamic_location(
        db_session, dict(THEATER_DATA)) is True
    assert await _stored(db_session, "theater") is not None


@pytest.mark.anyio
async def test_rebuild_of_an_existing_slug_is_an_upsert(db_session, validate_on):
    """同一条 effect 重跑是覆盖写,不该被 'slug already exists' 挡住。"""
    assert await civic_service._add_dynamic_location(
        db_session, dict(POST_OFFICE_DATA)) is True
    from app.agent import map_data
    map_data.LOCATIONS["post_office"] = {**POST_OFFICE_DATA,
                                         "bounds": (44, 100, 48, 106)}
    map_data._dynamic_slugs.add("post_office")
    try:
        assert await civic_service._add_dynamic_location(
            db_session, {**POST_OFFICE_DATA, "description": "改了一句"}) is True
        assert (await _stored(db_session)).data_json["description"] == "改了一句"
    finally:
        map_data.LOCATIONS.pop("post_office", None)
        map_data._dynamic_slugs.discard("post_office")
```

实现前：`test_out_of_walkable_bounds_are_refused` / `test_unreachable_entrance_is_refused` / `test_building_on_building_is_refused` 三条红（全部落库成功）。

#### 实现

1) `/Volumes/data/dev/simverse-world/backend/app/config.py` —— 紧跟 S5 那行之后：
```python
    civic_build_validate_enabled: bool = False  # 落库前几何/越界/可达性校验
```

2) `/Volumes/data/dev/simverse-world/backend/app/services/civic_service.py` —— 在 S5 插入的 schema 块**之后**、`existing = (await db.execute(` 之前插入：
```python
    if settings.civic_build_validate_enabled:
        # 四个参数成套:公投是 upsert(allow_existing_slug)、outdoor 街区是地面
        # 不是楼(否则邮局这类合法楼被误杀)、越界与可达性是新楼的两道门。
        # 必须跑在任何写库之前 —— 一旦并进 LOCATIONS,pathfinder 的
        # _get_forced_walkable 会把新楼入口自己塞进 walkable 集合自证成功。
        from app.lab.apply import validate_location_patch
        _errors, _warns = validate_location_patch(
            {"slug": slug,
             "data": {k: v for k, v in data.items() if k != "slug"}},
            allow_existing_slug=True,
            outdoor_overlap_is_warning=True,
            require_walkable_range=True,
            require_reachable_entrance=True,
        )
        for _w in _warns:
            logger.info("civic build warning (%s): %s", slug, _w)
        if _errors:
            logger.warning("civic build rejected (%s): %s",
                           slug, "; ".join(_errors))
            return False
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_civic_build_wiring.py tests/test_civic_build_validation.py tests/test_world_governance.py -q
```

**验收**：10 条接线测试全绿；闸开时 post_office 落库成功而 theater/observatory/annex 三条全部 `return False` 且库里无行；闸关时 theater 仍落库（旧行为）；重复执行同一条 post_office effect 走覆盖写不报错。

**commit**：

```
feat(civic): 建楼落库前接几何校验——挂 CIVIC_BUILD_VALIDATE_ENABLED,outdoor 重叠降级保住邮局
```

> #### 🔧 本 step 已被 critic 修订（2 处）
>
> 执行时以下列补丁为准，逐条覆盖上文对应字段。
>
> **修订 1 — 🔴 blocker · 字段 `implementation`**
>
> 处置：critic blocker：「S5/S6/S9/S10/S11（CIVIC_ 前缀）在同一 commit 内同时改 backend/.env.example 与 deploy/backend/.env.example」
>
> 定位锚点：
>
> ```
> civic_build_validate_enabled: bool = False  # 落库前几何
> ```
>
> 替换为：
>
> 【anchor 所在的 config.py 块保留原样；在它与「2) civic_service.py」之间插入新的一节 1b】
>
> 1b) 同 commit 补 env 文档（`CIVIC_` 前缀，两份模板都要写，理由同 S5：`tests/test_env_example_consistency.py:167` + `:375`）。`backend/.env.example` 与 `deploy/backend/.env.example` 各在 `CIVIC_BUILD_SCHEMA_ENABLED=false` 之后追加：
>
> ```
> # 建楼落库前的几何校验:outdoor 街区重叠降级为 warning(否则邮局这类合法楼
> # 被误杀)、楼压楼仍是 error、bounds/入口须落在 walkable 域 [14,173]x[12,123]、
> # 入口须落在 central_plaza 的连通分量上(判据是 get_reachable_tiles,
> # 不是会自证的 get_walkable_tiles)。公投是 upsert,同 slug 不算冲突。
> # 关(默认) = 只校验 slug 非空 + bounds 存在 = 逐字节旧行为(剧院照样落界外)。
> CIVIC_BUILD_VALIDATE_ENABLED=false
> ```
>
> **修订 2 — 🔴 blocker · 字段 `verify_cmd`**
>
> 处置：critic blocker：「七个 step 的 verify_cmd 全部追加 tests/test_env_example_consistency.py」
>
> 定位锚点：
>
> ```
> tests/test_world_governance.py -q
> ```
>
> 替换为：
>
> tests/test_world_governance.py tests/test_env_example_consistency.py -q
>

### P3-S7 — reload_world 补 pathfinder 缓存清理 + caravan 路网失效（WORLD_RELOAD_RESET_PATH_CACHE，默认关） 🔧

**Flag / 批次**：`WORLD_RELOAD_RESET_PATH_CACHE`，默认 `False`

**为什么**：reload_world(apply.py:214-228) 只做 load_dynamic_locations → rebuild_lookup → load_dynamic_lore 三件事，不清 pathfinder 的 _walkable_tiles_cache/_reachable_tiles_cache（reset_walkable_cache 的 docstring 自陈 "for testing"、生产零调用方）。后果是运行中新建的楼直到进程重启前 find_path 返 None（apply.py:170 的 `to_tile not in walkable_tiles` 直接 return None）。同时必须清 caravan_route.build_caravan_route 的 @lru_cache(maxsize=1)（caravan_route.py:129）——它内部吃 get_reachable_tiles()，只清 pathfinder 会让商队按旧连通分量走、居民按新的走，两份世界观分叉。顺序硬约束：reset 必须**晚于** load_dynamic_locations（_get_forced_walkable 读 LOCATIONS，先清后 merge 等于白清）。

#### 先写的测试（必须跑出失败）

新建 `/Volumes/data/dev/simverse-world/backend/tests/test_world_reload_cache.py`：

```python
"""P3 ③:reload_world 之后新楼必须当场可达(不必等进程重启)。"""
from unittest.mock import AsyncMock

import pytest

from app.agent import pathfinder
from app.config import settings
from app.lab import apply as apply_engine


@pytest.fixture(autouse=True)
def isolated_reload(monkeypatch):
    """reload_world 会去碰全局 engine 与 lore 表;本文件只关心缓存生命周期。"""
    monkeypatch.setattr("app.agent.map_data.load_dynamic_locations",
                        AsyncMock(return_value=0))
    monkeypatch.setattr("app.agent.location_lore.load_dynamic_lore",
                        AsyncMock(return_value=0))
    pathfinder.reset_walkable_cache()
    yield
    pathfinder.reset_walkable_cache()


@pytest.mark.anyio
async def test_gate_off_keeps_the_stale_path_cache(monkeypatch):
    """闸关 = 旧行为:缓存活着,运行中新建的楼要等重启才走得到。"""
    pathfinder.get_reachable_tiles()
    assert pathfinder._walkable_tiles_cache is not None
    await apply_engine.reload_world()
    assert pathfinder._walkable_tiles_cache is not None
    assert pathfinder._reachable_tiles_cache is not None


@pytest.mark.anyio
async def test_gate_on_drops_both_path_caches(monkeypatch):
    monkeypatch.setattr(settings, "world_reload_reset_path_cache", True)
    pathfinder.get_reachable_tiles()
    assert pathfinder._walkable_tiles_cache is not None
    await apply_engine.reload_world()
    assert pathfinder._walkable_tiles_cache is None
    assert pathfinder._reachable_tiles_cache is None


@pytest.mark.anyio
async def test_gate_on_invalidates_the_caravan_route(monkeypatch):
    """build_caravan_route 是 lru_cache(maxsize=1) 且吃 get_reachable_tiles;
    只清 pathfinder 会让商队与居民各持一份世界观。"""
    monkeypatch.setattr(settings, "world_reload_reset_path_cache", True)
    from app.services.caravan_route import build_caravan_route
    build_caravan_route()
    assert build_caravan_route.cache_info().currsize == 1
    await apply_engine.reload_world()
    assert build_caravan_route.cache_info().currsize == 0


@pytest.mark.anyio
async def test_cache_reset_runs_after_the_merge(monkeypatch):
    """_get_forced_walkable 读 LOCATIONS —— 先清后 merge 等于白清。"""
    monkeypatch.setattr(settings, "world_reload_reset_path_cache", True)
    order: list[str] = []

    async def _merge():
        order.append("merge")
        return 0

    def _reset():
        order.append("reset")

    monkeypatch.setattr("app.agent.map_data.load_dynamic_locations", _merge)
    monkeypatch.setattr("app.agent.pathfinder.reset_walkable_cache", _reset)
    await apply_engine.reload_world()
    assert order == ["merge", "reset"]


@pytest.mark.anyio
async def test_caravan_clear_failure_does_not_break_reload(monkeypatch):
    """reload 是 fail-open 链路:路网失效炸了也不许把世界重载带崩。"""
    monkeypatch.setattr(settings, "world_reload_reset_path_cache", True)

    def _boom():
        raise RuntimeError("route cache exploded")

    monkeypatch.setattr(
        "app.services.caravan_route.build_caravan_route.cache_clear", _boom)
    assert await apply_engine.reload_world() == 0
```

实现前：`test_gate_on_drops_both_path_caches` / `test_gate_on_invalidates_the_caravan_route` / `test_cache_reset_runs_after_the_merge` 三条红（AttributeError: settings 无 world_reload_reset_path_cache）。

#### 实现

1) `/Volumes/data/dev/simverse-world/backend/app/config.py` —— 紧跟 S6 那行之后：
```python
    # reload_world 顺带清 pathfinder/caravan 路网缓存。关 = 运行中新建的楼要等
    # 进程重启才走得到(find_path 的 to_tile not in walkable 直接 return None)。
    world_reload_reset_path_cache: bool = False
```

2) `/Volumes/data/dev/simverse-world/backend/app/lab/apply.py` —— 顶部 import 段（第 23 行之后）补：
```python
from app.config import settings
```

3) 同文件 `reload_world`（:214-228）改：
```python
# before
    n = await load_dynamic_locations()
    location_tracker.rebuild_lookup()
# after
    n = await load_dynamic_locations()
    if settings.world_reload_reset_path_cache:
        # 必须晚于 merge:_get_forced_walkable 读的是 LOCATIONS(pathfinder.py:62),
        # 先清后 merge 等于白清。清完后下一次 get_walkable_tiles() 会重读
        # tilemap.json 全量重算 —— reload_world 的调用点(world_reload_subscriber
        # 与 civic 落库后那次)都不在请求热路径上,routers/world.py:19 走的是
        # load_dynamic_locations 不经本函数。
        from app.agent import pathfinder
        pathfinder.reset_walkable_cache()
        try:
            from app.services.caravan_route import build_caravan_route
            build_caravan_route.cache_clear()
        except Exception:
            logger.warning("caravan route cache clear failed", exc_info=True)
    location_tracker.rebuild_lookup()
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_world_reload_cache.py tests/test_pathfinder.py tests/test_caravan_lifecycle.py -q
```

**验收**：5 条新测试全绿；`tests/test_pathfinder.py` / `tests/test_caravan_lifecycle.py` 零新增失败；闸开后 `reload_world()` 返回时 `pathfinder._walkable_tiles_cache is None` 且 `build_caravan_route.cache_info().currsize == 0`，闸关时两者均保持。

**commit**：

```
fix(world): reload_world 补路径与商队路网缓存失效——挂 WORLD_RELOAD_RESET_PATH_CACHE,清理排在 merge 之后
```

> #### 🔧 本 step 已被 critic 修订（2 处）
>
> 执行时以下列补丁为准，逐条覆盖上文对应字段。
>
> **修订 1 — 🟠 major · 字段 `implementation`**
>
> 处置：critic major：「说清触发频率、会不会打到每 tick、节流策略」（实测清缓存后首次 get_walkable_tiles 18.35ms + BFS 4.28ms + caravan 38.42ms）
>
> 定位锚点：
>
> ```
> world_reload_reset_path_cache: bool = False
> ```
>
> 替换为：
>
> 【anchor 所在的 config.py 块保留；在它与「2) apply.py 顶部 import」之间插入新的一节 1b】
>
> 1b) 同 commit 补两份 env 模板（`WORLD_` 前缀不在任何一条现成 parity 内，但 P3-S12 的 presence 测试对两份文件都断言，故 backend + deploy 都写）。各在 `CIVIC_FACTS_ENABLED=false` 之后追加：
>
> ```
> # reload_world 顺带清 pathfinder 路径缓存与 caravan 路网 lru_cache。
> # 关(默认) = 运行中新建的楼要等进程重启才走得到(find_path 的
> #            to_tile not in walkable 直接 return None)。
> # 触发频率(别按「每 tick」估):reload_world 只有五个调用点 —— 进程启动
> #   (main.py:88 / agent/main.py:60)、lab apply/revert(apply.py:133,207)、
> #   proposal apply/revert(proposal_service.py:305,370)、公投落库
> #   (civic_service.py:932),外加每进程一个 world_reload_subscriber
> #   (apply.py:261) 把上述任一次广播成「每进程各重算一次」。
> #   **agent tick 不调它**,不在 NPC 热路径上。
> # 单次代价(本机实测):tilemap 6.1MB 解析+全量重扫 18.35ms + BFS 4.28ms,
> #   下一次商队路网重建再 38.42ms —— 合计约 61ms 同步阻塞/进程/次,
> #   日常量级是「结票/审批」那种个位数次/天。因此**不加节流器**:
> #   加了反而让「新楼何时可达」变得不可预测。清完是惰性重建,重算落在清完后
> #   第一个碰它的协程里(见本 step 的取舍说明)。
> WORLD_RELOAD_RESET_PATH_CACHE=false
> ```
>
> **修订 2 — 🔴 blocker · 字段 `verify_cmd`**
>
> 处置：critic blocker：「七个 step 的 verify_cmd 全部追加 tests/test_env_example_consistency.py」
>
> 定位锚点：
>
> ```
> tests/test_world_reload_cache.py tests/test_pathfinder.py
> ```
>
> 替换为：
>
> tests/test_world_reload_cache.py tests/test_env_example_consistency.py tests/test_pathfinder.py
>

### P3-S8 — 坐标反查按具体性优先（LOCATION_SPECIFIC_FIRST_ENABLED，默认关） 🔧

**Flag / 批次**：`LOCATION_SPECIFIC_FIRST_ENABLED`，默认 `False`。注意：这道闸是任何依赖 `get_location_id_at` 的 P1 能力闸的**硬前置**，必须先开

**为什么**：实测 get_location_id_at(46,100)→south_quarter、(172,45)→east_gardens：_find_location_in_bounds(map_data.py:243-249) 首命中即返，命中序 = dict 插入序，而 6 条 outdoor 街区在静态字面量索引 28-33、动态楼追加在 34+。后果链：location_tracker._build_lookup 同为 setdefault 首命中（表头注释自陈两者必须同序）→ 玩家踩进邮局记成 south_quarter、location_first_visit 永不触发、location_lore.LORE["post_office"]/["theater"] 两段专门写的文案是死文案、/exploration/me 恒 visited=false；且任何走 get_location_id_at 的能力门在新楼里恒判 False。改法是只换扫描序（非 outdoor 优先，同类面积小者优先，sorted 稳定故平局仍按插入序），不动 LOCATIONS 本身的插入顺序——nearest_dining_location/nearest_indoor_location 的「并列取先者」语义不受影响（它们遍历 LOCATIONS 而非本索引）。若 P1 已先落地同一改动，本 step 退化为 no-op，跳过。

#### 先写的测试（必须跑出失败）

新建 `/Volumes/data/dev/simverse-world/backend/tests/test_location_specificity.py`：

```python
"""P3 ④d:楼压在 outdoor 街区上时,坐标反查必须认出楼。

实测 get_location_id_at(46,100) 返 south_quarter、(172,45) 返 east_gardens ——
首命中 = dict 插入序,6 条 outdoor 排在静态字面量索引 28-33,动态楼追加在 34+。
"""
import pytest

from app.agent import map_data
from app.config import settings
from app.services import location_tracker

POST_OFFICE = {"name": "邮局", "type": "public", "bounds": (44, 100, 48, 106),
               "center": (46, 103), "entrance": (46, 100)}
THEATER = {"name": "剧院", "type": "public", "bounds": (172, 40, 178, 50),
           "center": (175, 45), "entrance": (172, 45)}


@pytest.fixture
def merged_buildings():
    """把生产那两栋动态楼按真实 data_json 并进内存(追加在尾部,与
    load_dynamic_locations:386 同形)。"""
    snap = {k: dict(v) for k, v in map_data.LOCATIONS.items()}
    snap_dyn = set(map_data._dynamic_slugs)
    map_data.LOCATIONS["post_office"] = dict(POST_OFFICE)
    map_data.LOCATIONS["theater"] = dict(THEATER)
    map_data._dynamic_slugs |= {"post_office", "theater"}
    map_data.rebuild_bounds_order()
    location_tracker.rebuild_lookup()
    yield
    map_data.LOCATIONS.clear()
    map_data.LOCATIONS.update(snap)
    map_data._dynamic_slugs = snap_dyn
    map_data.rebuild_bounds_order()
    location_tracker.rebuild_lookup()


def test_gate_off_reproduces_the_shadowing(merged_buildings):
    assert map_data.get_location_id_at(46, 100) == "south_quarter"
    assert map_data.get_location_id_at(172, 45) == "east_gardens"


def test_gate_on_resolves_the_building(merged_buildings, monkeypatch):
    monkeypatch.setattr(settings, "location_specific_first_enabled", True)
    assert map_data.get_location_id_at(46, 100) == "post_office"
    assert map_data.get_location_id_at(46, 103) == "post_office"
    assert map_data.get_location_id_at(172, 45) == "theater"
    assert map_data.get_location_id_at(175, 45) == "theater"


def test_gate_on_does_not_disturb_non_overlapping_tiles(merged_buildings, monkeypatch):
    monkeypatch.setattr(settings, "location_specific_first_enabled", True)
    assert map_data.get_location_id_at(20, 20) == "academy"
    assert map_data.get_location_id_at(75, 56) == "central_plaza"
    assert map_data.get_location_id_at(0, 0) is None


def test_tracker_index_stays_in_sync_with_the_finder(merged_buildings, monkeypatch):
    """location_tracker 的 setdefault 表必须与 get_location_id_at 同序 ——
    两处不同序会让玩家首访与 NPC 认出不同的楼。"""
    monkeypatch.setattr(settings, "location_specific_first_enabled", True)
    location_tracker.rebuild_lookup()
    for tile in ((46, 100), (46, 103), (172, 45), (175, 45), (20, 20), (75, 56)):
        assert location_tracker.location_at_tile(*tile) == \
            map_data.get_location_id_at(*tile), f"{tile} 两套索引对不上"


def test_lore_for_the_two_voted_buildings_becomes_reachable(merged_buildings, monkeypatch):
    """location_lore.py:21-22 那两段专门为公投新楼写的文案今天是死文案。"""
    from app.agent.location_lore import lore_for
    monkeypatch.setattr(settings, "location_specific_first_enabled", True)
    for tile in ((46, 103), (172, 45)):
        loc_id = map_data.get_location_id_at(*tile)
        assert lore_for(loc_id), f"{loc_id} 的 lore 仍然取不到"


def test_specificity_order_puts_buildings_before_outdoor(merged_buildings):
    order = [k for k, _ in map_data._specificity_items()]
    assert order.index("post_office") < order.index("south_quarter")
    assert order.index("theater") < order.index("east_gardens")
```

实现前：`AttributeError: module 'app.agent.map_data' has no attribute 'rebuild_bounds_order'` → 整文件红。

#### 实现

1) `/Volumes/data/dev/simverse-world/backend/app/config.py` —— 紧跟 S7 那行之后：
```python
    # 坐标反查按「具体性」优先(非 outdoor > 面积小)。关 = 首命中 = 插入序,
    # 即邮局被 south_quarter、剧院被 east_gardens 遮蔽的今天。
    location_specific_first_enabled: bool = False
```

2) `/Volumes/data/dev/simverse-world/backend/app/agent/map_data.py` —— 在 `_find_location_in_bounds`（:243）**之前**插入：
```python
#: 「具体性优先」的 bounds 扫描序(P3 ④d)。LOCATIONS 的插入序把 6 条 outdoor
#: 大街区排在静态字面量末尾(索引 28-33),动态楼一律追加在更后面
#: (load_dynamic_locations:386) —— 于是首命中让邮局(44,100,48,106) 被
#: south_quarter(42,100,135,109) 完全遮蔽。排序键 (是否 outdoor, bounds 面积)
#: 升序;sorted 稳定,平局仍按插入序。**只换扫描序,不动 LOCATIONS 本身** ——
#: nearest_dining_location / nearest_indoor_location 的「并列取先者」遍历的是
#: LOCATIONS,不受影响。
_bounds_order: list[str] = []


def _bounds_area(loc: dict) -> int:
    b = loc.get("bounds")
    if not b or len(b) != 4:
        return 0
    return (abs(int(b[2]) - int(b[0])) + 1) * (abs(int(b[3]) - int(b[1])) + 1)


def rebuild_bounds_order() -> None:
    """重算具体性索引。LOCATIONS 变动后必须调(load_dynamic_locations 末尾)。"""
    global _bounds_order
    _bounds_order = [
        loc_id for loc_id, _ in sorted(
            LOCATIONS.items(),
            key=lambda kv: (kv[1].get("type") == "outdoor", _bounds_area(kv[1])),
        )
    ]


def _specificity_items() -> list[tuple[str, dict]]:
    if len(_bounds_order) != len(LOCATIONS):
        rebuild_bounds_order()
    return [(loc_id, LOCATIONS[loc_id]) for loc_id in _bounds_order
            if loc_id in LOCATIONS]


def iter_locations_for_lookup():
    """当前生效的 bounds 扫描序。``_find_location_in_bounds`` 与
    ``location_tracker._build_lookup`` **必须**共用这一个入口 —— 两处不同序
    会让 tracker 与 agent 认出不同的楼(location_tracker.py:26-27 的表头注释)。"""
    from app.config import settings
    if settings.location_specific_first_enabled:
        return _specificity_items()
    return LOCATIONS.items()
```

3) 同文件 `_find_location_in_bounds`（:243-249）改一行：
```python
# before
    for loc_id, loc in LOCATIONS.items():
# after
    for loc_id, loc in iter_locations_for_lookup():
```

4) 同文件 `load_dynamic_locations`（:388 的 `return n` 之前）补一行：
```python
    rebuild_bounds_order()
    return n
```

5) 同文件末尾（`allocate_home` 之后）补模块级初始化：
```python
rebuild_bounds_order()
```

6) `/Volumes/data/dev/simverse-world/backend/app/services/location_tracker.py` —— 第 17 行之后补 `from app.agent import map_data`；`_build_lookup`（:33）改一行：
```python
# before
    for loc_id, loc in LOCATIONS.items():
# after
    for loc_id, loc in map_data.iter_locations_for_lookup():
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_location_specificity.py tests/test_map_data.py tests/test_map_integration.py tests/test_location_tracker.py -q
```

**验收**：6 条新测试全绿；`tests/test_map_data.py`/`test_map_integration.py`/`test_location_tracker.py` 零新增失败；闸关时 `get_location_id_at(46,100)=='south_quarter'`，闸开时 `=='post_office'` 且 `location_tracker.location_at_tile` 与之逐 tile 一致。

**commit**：

```
fix(world): 坐标反查按具体性优先——挂 LOCATION_SPECIFIC_FIRST_ENABLED,解开邮局/剧院被 outdoor 街区遮蔽
```

> #### 🔧 本 step 已被 critic 修订（3 处）
>
> 执行时以下列补丁为准，逐条覆盖上文对应字段。
>
> **修订 1 — 🔴 blocker · 字段 `implementation`**
>
> 处置：critic blocker：「len(_bounds_order) != len(LOCATIONS) 只比长度…新 slug 既不在 _bounds_order 里也不会被补进来 → get_location_id_at 返 None，全线失效且零异常」+ perf「_specificity_items 每次调用都新建 34 元组列表，20000 次 25.2ms」
>
> 定位锚点：
>
> ```
> location_specific_first_enabled: bool = False
> ```
>
> 替换为：
>
> 【① anchor 所在 config.py 块保留；同 commit 补两份 env 模板（`LOCATION_` 命中 P1-S10 新增的 LOCATION_ parity，必须双写）】
>
> ```
> # 坐标反查按「具体性」优先(非 outdoor > 面积小)。关(默认) = 首命中 = 插入序,
> # 即邮局被 south_quarter、剧院被 east_gardens 遮蔽的今天。
> # 依赖方(真实清单):decide/basic.py 的 dining 判定、_maybe_shelter 的
> #   location_is_indoor、location_tracker 首访、location_lore、/exploration/me。
> # ⚠ 一次性不可逆:翻开的当下 post_office/theater 的 tile 首次解析成新 slug,
> #   location_tracker 会 emit location_first_visit,下挂 explorer_* 成就与赛季分,
> #   闸翻回去也收不回。开闸前先跑影响面盘点 SQL 并写进 handoff。
> LOCATION_SPECIFIC_FIRST_ENABLED=false
> ```
>
> 【② 把 item 2 里 `_bounds_order` / `rebuild_bounds_order` / `_specificity_items` 三段替换为】
>
> ```python
> _bounds_order: list[str] = []
> _specificity_cache: list[tuple[str, dict]] = []
> _cached_keys: frozenset[str] = frozenset()
>
>
> def rebuild_bounds_order() -> None:
>     """重算具体性索引 + 缓存扫描列表。LOCATIONS 变动后必须调。"""
>     global _bounds_order, _specificity_cache, _cached_keys
>     _bounds_order = [loc_id for loc_id, _ in sorted(
>         LOCATIONS.items(),
>         key=lambda kv: (kv[1].get("type") == "outdoor", _bounds_area(kv[1])))]
>     _specificity_cache = [(loc_id, LOCATIONS[loc_id]) for loc_id in _bounds_order]
>     _cached_keys = frozenset(_bounds_order)
>
>
> def _specificity_items() -> list[tuple[str, dict]]:
>     # 守卫必须是键集身份,**不能是长度**:load_dynamic_locations 先 pop 全部动态
>     # slug 再 merge 本轮,一次「下线一栋+上线一栋」净条数相同 -> 长度守卫不触发,
>     # 新 slug 既不在 _bounds_order 里也不会被补进来 -> get_location_id_at 对整栋
>     # 新楼返 None(不是返旧值),EAT/RESEARCH/躲雨/首访/lore 全线失效且零异常。
>     # frozenset != dict_keys 是 O(n) 无分配比较;命中缓存直接返回同一个列表,
>     # 顺带修掉「每次调用重建 34 元组列表」(实测 20000 次 25.2ms)。
>     # 就地改某条已有 slug 的 bounds(键集不变)守卫看不见 —— load_dynamic_locations
>     # 末尾已显式 rebuild(item 4),其余就地改 LOCATIONS 的调用方须自行调它。
>     if _cached_keys != LOCATIONS.keys():
>         rebuild_bounds_order()
>     return _specificity_cache
> ```
>
> **修订 2 — 🔴 blocker · 字段 `test_first`**
>
> 处置：critic blocker fix：「同批补测试：pop 掉一个动态 slug 同时 setitem 一个新 slug（保持 len 不变），断言 get_location_id_at(新楼center) 返回新 slug、且 location_tracker.location_at_tile 同步认出」
>
> 定位锚点：
>
> ```
> def test_specificity_order_puts_buildings_before_outdoor
> ```
>
> 替换为：
>
> 【在 anchor 这条之后追加两条】
>
> ```python
> def test_swapping_one_building_for_another_keeps_the_index_honest(
>         merged_buildings, monkeypatch):
>     """删一条同时加一条:长度不变、键集变了。长度守卫会让新楼整栋从坐标反查里
>     消失(get_location_id_at 返 None),而这正是 load_dynamic_locations 每次
>     reload 的形状(先 pop 全部动态 slug 再 merge 本轮)。"""
>     monkeypatch.setattr(settings, "location_specific_first_enabled", True)
>     before = len(map_data.LOCATIONS)
>     map_data.LOCATIONS.pop("theater")
>     map_data.LOCATIONS["gallery"] = {"name": "画廊", "type": "public",
>                                      "bounds": (172, 40, 178, 50),
>                                      "center": (175, 45), "entrance": (172, 45)}
>     map_data._dynamic_slugs.discard("theater")
>     map_data._dynamic_slugs.add("gallery")
>     assert len(map_data.LOCATIONS) == before, "这条测试的前提就是条数不变"
>     # 刻意不调 rebuild_bounds_order():惰性守卫必须自己发现键集变了
>     assert map_data.get_location_id_at(175, 45) == "gallery"
>     location_tracker.rebuild_lookup()
>     assert location_tracker.location_at_tile(175, 45) == "gallery"
>
>
> def test_specificity_items_is_cached_between_calls(merged_buildings):
>     """每次调用重建 34 元组列表实测 20000 次 25.2ms;caravan 全图扫描是大户。"""
>     first = map_data._specificity_items()
>     assert map_data._specificity_items() is first
>     map_data.LOCATIONS["annex"] = {"name": "侧厅", "type": "public",
>                                    "bounds": (60, 60, 62, 62)}
>     assert map_data._specificity_items() is not first, "键集变了必须重建"
> ```
>
> 实现前这两条与本文件其余测试一并红（`AttributeError: … has no attribute 'rebuild_bounds_order'`）。
>
> **修订 3 — 🔴 blocker · 字段 `verify_cmd`**
>
> 处置：critic blocker：verify_cmd 追加 env 一致性测试；并按「把『闸开后 build_caravan_route() 至少跑一次不抛』纳入验收」把 caravan 路网测试拉进本 step 的自验证面
>
> 定位锚点：
>
> ```
> tests/test_location_specificity.py tests/test_map_data.py
> ```
>
> 替换为：
>
> tests/test_location_specificity.py tests/test_env_example_consistency.py tests/test_caravan_route.py tests/test_map_data.py
>

### P3-S9 — town_facts 地点名单给动态楼留保留位（CIVIC_FACTS_PLACES_DYNAMIC_RESERVE=0，默认关） 🔧

**Flag / 批次**：`CIVIC_FACTS_PLACES_DYNAMIC_RESERVE`，默认 `0`（0 = 关）

**为什么**：_read_places(town_facts_service.py:387-417) 的 docstring 自陈「静态设施在前，所以被条数上限挤掉的总是新加的动态地点」。今天 9 静态 public + 2 动态 = 11，PLACES_LIMIT=12，再建 2 栋就开始挤掉新楼。不改 PLACES_LIMIT（12 是 prompt 预算，渲染成一行进 llm/prompt.py:207-209）。改口径 = 保留位，与 realism_pool_civic_reserve 同构（0 = 逐字节旧行为；一个数同时表达「开没开」与「几个坑」；没填满的坑退还）。排序口径必须一并换：现有 order_by(slug) 表达的是字典序、与新旧无关，保留位只有配 created_at DESC 才是「新楼优先」（created_at 列已存在，models/dynamic_location.py:25，零迁移）。渲染顺序仍是静态在前、动态在后，只有名额分配先给动态——避免公共设施在 prompt 里顺序抖动伤 prefix 缓存。去重顺序不能动：先 _clip 再 dict.fromkeys。

#### 先写的测试（必须跑出失败）

新建 `/Volumes/data/dev/simverse-world/backend/tests/test_town_facts_places_reserve.py`：

```python
"""P3 ⑤:PLACES_LIMIT 下给新建动态楼留名额。

_read_places 的 docstring 自陈「被条数上限挤掉的总是新加的动态地点」——
9 静态 public + 上限 12,再建 2 栋就开始挤。
"""
from datetime import datetime, timedelta, UTC

import pytest

from app.config import settings
from app.models.dynamic_location import DynamicLocation
from app.services import town_facts_service as tfs
from app.services import world_event_service


@pytest.fixture(autouse=True)
def _clean_caches():
    tfs._reset_for_tests()
    world_event_service.invalidate_active_cache()
    yield
    tfs._reset_for_tests()
    world_event_service.invalidate_active_cache()


@pytest.fixture
def facts_on(monkeypatch):
    monkeypatch.setattr(settings, "civic_facts_enabled", True)


def _dyn(slug: str, name: str, *, days_ago: int) -> DynamicLocation:
    return DynamicLocation(
        slug=slug, active=True,
        data_json={"name": name, "type": "public", "bounds": [0, 0, 1, 1]},
        created_at=datetime.now(UTC) - timedelta(days=days_ago))


async def _places(db):
    return (await tfs.get_town_facts_cached(db))["places"]


@pytest.mark.anyio
async def test_reserve_zero_is_byte_identical(db_session, facts_on):
    """默认 0 = 旧行为:静态占满,新楼被挤掉。"""
    db_session.add_all([_dyn(f"zz-{i:03d}", f"新楼{i:03d}", days_ago=0)
                        for i in range(6)])
    await db_session.commit()
    places = await _places(db_session)
    assert len(places) == tfs.PLACES_LIMIT
    assert "市政厅" in places
    assert places[:9] == ["学院", "酒馆", "咖啡馆", "工坊", "图书馆",
                          "杂货铺", "市政厅", "实验楼", "集市大厅"]


@pytest.mark.anyio
async def test_reserve_keeps_the_newest_buildings(db_session, facts_on, monkeypatch):
    monkeypatch.setattr(settings, "civic_facts_places_dynamic_reserve", 2)
    db_session.add_all(
        [_dyn(f"aa-{i:03d}", f"老楼{i:03d}", days_ago=30 + i) for i in range(6)]
        + [_dyn("zz-new1", "新楼甲", days_ago=1),
           _dyn("zz-new2", "新楼乙", days_ago=0)])
    await db_session.commit()
    places = await _places(db_session)
    assert len(places) == tfs.PLACES_LIMIT
    assert "新楼甲" in places and "新楼乙" in places
    assert "市政厅" in places, "保留位不许把静态公共设施整段顶掉"
    assert places[:9] == ["学院", "酒馆", "咖啡馆", "工坊", "图书馆",
                          "杂货铺", "市政厅", "实验楼", "集市大厅"], \
        "渲染顺序仍是静态在前(prompt 前缀稳定),只有名额分配先给动态"


@pytest.mark.anyio
async def test_unused_reserve_is_returned_to_static(db_session, facts_on, monkeypatch):
    """没填满的坑退还 —— len(places) 与 reserve=0 时恒等。"""
    monkeypatch.setattr(settings, "civic_facts_places_dynamic_reserve", 5)
    db_session.add(_dyn("zz-new1", "新楼甲", days_ago=0))
    await db_session.commit()
    places = await _places(db_session)
    assert len(places) == 10 == 9 + 1
    assert "市政厅" in places and "新楼甲" in places


@pytest.mark.anyio
async def test_reserve_does_not_double_count_a_merged_building(
        db_session, facts_on, monkeypatch):
    monkeypatch.setattr(settings, "civic_facts_places_dynamic_reserve", 2)
    from app.agent.map_data import LOCATIONS
    monkeypatch.setitem(LOCATIONS, "theater", {
        "name": "剧院", "type": "public", "bounds": (172, 40, 178, 50)})
    db_session.add(_dyn("theater", "剧院", days_ago=0))
    await db_session.commit()
    places = await _places(db_session)
    assert places.count("剧院") == 1


@pytest.mark.anyio
async def test_reserve_still_respects_the_char_cap(db_session, facts_on, monkeypatch):
    monkeypatch.setattr(settings, "civic_facts_places_dynamic_reserve", 2)
    db_session.add(_dyn("zz-long", "楼" * 200, days_ago=0))
    await db_session.commit()
    for name in await _places(db_session):
        assert len(name) <= tfs.PLACE_MAX_CHARS
```

实现前：三条 reserve 测试红（`市政厅`/新楼共存断言失败，且 `civic_facts_places_dynamic_reserve` 属性不存在）。

#### 实现

1) `/Volumes/data/dev/simverse-world/backend/app/config.py` —— 在 `civic_facts_max_stale_seconds`（:766）之后新增：
```python
    # 「小镇有哪些地方」名单里给公投新建的楼留几个坑。0 = 逐字节旧行为
    # (静态在前占满 PLACES_LIMIT,新楼永远被挤掉)。没填满的坑退还给静态,
    # 所以 len(places) 与改前恒等。
    civic_facts_places_dynamic_reserve: int = 0
```

2) `/Volumes/data/dev/simverse-world/backend/app/services/town_facts_service.py` —— `_read_places` 函数体（:407-417）整段替换：
```python
    from app.agent.map_data import LOCATIONS
    from app.models.dynamic_location import DynamicLocation

    names = [loc["name"] for loc in LOCATIONS.values()
             if loc.get("type") == "public" and loc.get("name")]
    reserve = max(0, int(settings.civic_facts_places_dynamic_reserve or 0))
    stmt = select(DynamicLocation.data_json).where(DynamicLocation.active.is_(True))
    # reserve=0 保持旧口径(按 slug 的字典序,与新旧无关);开了保留位才换成
    # 「新楼优先」,否则坑会稳定地留给 slug 靠前的那栋老楼。created_at 是建表
    # 就有的列(033_add_world_governance.py:51),换排序零迁移。
    stmt = (stmt.order_by(DynamicLocation.created_at.desc(), DynamicLocation.slug)
            if reserve else stmt.order_by(DynamicLocation.slug))
    rows = (await db.execute(stmt)).scalars().all()
    dyn = [data["name"] for data in (r or {} for r in rows)
           if data.get("type") == "public" and data.get("name")]
    if not reserve:
        clipped = (_clip(name, PLACE_MAX_CHARS) for name in names + dyn)
        return list(dict.fromkeys(clipped))[:PLACES_LIMIT]
    # 保留位:动态先占最多 reserve 个坑,静态填剩下的,没填满的坑退还给静态。
    # 渲染顺序仍是静态在前 —— 只有名额分配先给动态,公共设施在 prompt 里的
    # 顺序不抖(前缀缓存)。去重顺序不动:先 _clip 再 dict.fromkeys。
    dyn_clipped = list(dict.fromkeys(_clip(n, PLACE_MAX_CHARS) for n in dyn))
    head = dyn_clipped[:reserve]
    static_clipped = [c for c in dict.fromkeys(_clip(n, PLACE_MAX_CHARS)
                                               for n in names) if c not in head]
    static_clipped = static_clipped[:max(0, PLACES_LIMIT - len(head))]
    tail = [c for c in dyn_clipped[reserve:]
            if c not in head and c not in static_clipped]
    return (static_clipped + head + tail)[:PLACES_LIMIT]
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_town_facts_places_reserve.py tests/test_town_facts_service.py -q
```

**验收**：5 条新测试全绿；`tests/test_town_facts_service.py` 全部既有断言（含 `test_places_are_capped_by_count_and_length` 的 `"市政厅" in places`、`test_places_do_not_double_count_merged_dynamic_locations`）零新增失败；reserve=2 时最新两栋楼与 9 条静态同时在名单里且 `len(places)==12`。

**commit**：

```
feat(civic): 小镇地点名单给新建动态楼留保留位——挂 CIVIC_FACTS_PLACES_DYNAMIC_RESERVE=0,默认逐字节旧行为
```

> #### 🔧 本 step 已被 critic 修订（2 处）
>
> 执行时以下列补丁为准，逐条覆盖上文对应字段。
>
> **修订 1 — 🔴 blocker · 字段 `implementation`**
>
> 处置：critic blocker：「S5/S6/S9/S10/S11（CIVIC_ 前缀）在同一 commit 内同时改 backend/.env.example 与 deploy/backend/.env.example」
>
> 定位锚点：
>
> ```
> civic_facts_places_dynamic_reserve: int = 0
> ```
>
> 替换为：
>
> 【anchor 所在 config.py 块保留；在它与「2) town_facts_service.py」之间插入新的一节 1b】
>
> 1b) 同 commit 补两份 env 模板（`CIVIC_` 前缀 → `GOVERNANCE_PREFIXES` parity 强制 deploy 双写）。各在 `CIVIC_BUILD_VALIDATE_ENABLED=false` 之后追加：
>
> ```
> # 「小镇有哪些地方」名单(PLACES_LIMIT=12)里给公投新建的楼留几个坑。
> # 0(默认) = 逐字节旧行为:静态设施在前占满,新楼永远被挤掉。
> # 开到 N:动态先占最多 N 个坑,没填满的坑退还给静态(len(places) 与改前恒等);
> # 渲染顺序仍是静态在前(prompt 前缀缓存不抖),只有名额分配先给动态。
> CIVIC_FACTS_PLACES_DYNAMIC_RESERVE=0
> ```
>
> **修订 2 — 🔴 blocker · 字段 `verify_cmd`**
>
> 处置：critic blocker：「七个 step 的 verify_cmd 全部追加 tests/test_env_example_consistency.py」
>
> 定位锚点：
>
> ```
> tests/test_town_facts_places_reserve.py
> ```
>
> 替换为：
>
> tests/test_town_facts_places_reserve.py tests/test_env_example_consistency.py
>

### P3-S10 — 公投执行结果落库审计（CIVIC_EFFECT_AUDIT_ENABLED，默认关） 🔧

**Flag / 批次**：`CIVIC_EFFECT_AUDIT_ENABLED`，默认 `False`

**为什么**：今天 _execute_outcome 的 return False 同时表示「类型不支持」与「执行抛异常」（civic_service.py:908-910 的 except Exception 吞掉一切），失败的唯一信号是一句中文公告 + 一行 logger.warning，且 S6 新增的「选址不合规」也会被说成同一件事。补法：① 加 keyword-only 出参 audit（默认 None = 逐字节旧行为，既有 4 处调用点与测试全不受影响，返回类型仍是 bool）；② 结果写回 poll.options_json[0]（沿用 blob-on-opts[0] 约定：_proposer_slug/_npc_voters/_eligible_at_open/_policy_outcome），排在 _clerk_announce **之前**（公告整段吞异常，审计不该排在它后面）；③ options_json 出网有白名单投影（script_service.public_option:139-142 + routers/townhall.py:110-115 只放行 label/npc_votes/won/final_votes），新键自动不泄漏；④ 补一个 Counter，范式抄 CIVIC_FACTS_FAILOPEN。won 先于执行提交的既有时序（:721-725）不动——执行成功但 won 丢失将无法追溯谁赢。

#### 先写的测试（必须跑出失败）

新建 `/Volumes/data/dev/simverse-world/backend/tests/test_civic_effect_audit.py`：

```python
"""P3 ⑥:公投胜出后「落没落地」必须可追溯,且失败原因可区分。"""
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.agent import pathfinder
from app.config import settings
from app.models.dynamic_location import DynamicLocation
from app.models.season import Poll
from app.services import civic_service

THEATER_DATA = {
    "slug": "theater", "name": "剧院", "type": "public",
    "bounds": [172, 40, 178, 50], "center": [175, 45], "entrance": [172, 45],
}
POST_OFFICE_DATA = {
    "slug": "post_office", "name": "邮局", "type": "public",
    "bounds": [44, 100, 48, 106], "center": [46, 103], "entrance": [46, 100],
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr("app.lab.apply.reload_world", AsyncMock(return_value=0))
    monkeypatch.setattr("app.lab.apply.publish_world_reload", AsyncMock())
    pathfinder.reset_walkable_cache()
    yield
    pathfinder.reset_walkable_cache()


def _poll(data: dict) -> Poll:
    return Poll(
        question=f"兴建{data['name']}", status="open",
        closes_at=datetime.now(UTC) - timedelta(hours=1),
        options_json=[
            {"label": "赞成兴建", "npc_votes": 3,
             "effect": {"type": "dynamic_location", "data": data}},
            {"label": "暂缓,维持现状", "npc_votes": 0, "effect": None},
        ])


@pytest.mark.anyio
async def test_audit_is_opt_in(db_session):
    poll = _poll(POST_OFFICE_DATA)
    db_session.add(poll)
    await db_session.commit()
    await civic_service.close_due_polls(db_session)
    await db_session.refresh(poll)
    assert poll.options_json[0]["won"] is True
    assert "_effect_applied" not in poll.options_json[0], "默认关 = 不多写一个键"


@pytest.mark.anyio
async def test_success_is_recorded(db_session, monkeypatch):
    monkeypatch.setattr(settings, "civic_effect_audit_enabled", True)
    poll = _poll(POST_OFFICE_DATA)
    db_session.add(poll)
    await db_session.commit()
    await civic_service.close_due_polls(db_session)
    await db_session.refresh(poll)
    assert poll.options_json[0]["_effect_applied"] is True
    assert poll.options_json[0]["_effect_error"] is None


@pytest.mark.anyio
async def test_geometry_rejection_is_distinguishable(db_session, monkeypatch):
    """「选址不合规」不许和「DB 炸了」说成同一件事。"""
    monkeypatch.setattr(settings, "civic_effect_audit_enabled", True)
    monkeypatch.setattr(settings, "civic_build_validate_enabled", True)
    poll = _poll(THEATER_DATA)
    db_session.add(poll)
    await db_session.commit()
    await civic_service.close_due_polls(db_session)
    await db_session.refresh(poll)
    assert poll.options_json[0]["won"] is True, "胜出仍然成立,只是没落地"
    assert poll.options_json[0]["_effect_applied"] is False
    assert poll.options_json[0]["_effect_error"] == "invalid_geometry"
    assert (await db_session.execute(
        select(DynamicLocation).where(DynamicLocation.slug == "theater")
    )).scalar_one_or_none() is None


@pytest.mark.anyio
async def test_unsupported_type_reports_its_own_code(db_session):
    audit: dict = {}
    ok = await civic_service._execute_outcome(
        db_session, {"type": "teleport"}, audit=audit)
    assert ok is False
    assert audit["error"] == "unsupported_type"
    assert audit["etype"] == "teleport"


@pytest.mark.anyio
async def test_missing_bounds_reports_schema_rejected(db_session):
    audit: dict = {}
    ok = await civic_service._execute_outcome(
        db_session, {"type": "dynamic_location", "data": {"slug": "x"}},
        audit=audit)
    assert ok is False
    assert audit["error"] == "schema_rejected"


@pytest.mark.anyio
async def test_audit_blob_never_leaks_to_the_public_option(db_session, monkeypatch):
    """options_json 出网是白名单投影,新键不该有任何一条出得去。"""
    monkeypatch.setattr(settings, "civic_effect_audit_enabled", True)
    from app.services.script_service import public_option
    opt = {"label": "赞成兴建", "npc_votes": 3, "effect": {"type": "x"},
           "_effect_applied": False, "_effect_error": "invalid_geometry"}
    public = public_option(opt)
    assert "_effect_applied" not in public and "_effect_error" not in public
```

实现前：`TypeError: _execute_outcome() got an unexpected keyword argument 'audit'`。

#### 实现

1) `/Volumes/data/dev/simverse-world/backend/app/config.py` —— 紧跟 S8 那行之后：
```python
    # 公投执行结果写回 options_json[0](_effect_applied/_effect_error)。
    # 关 = 失败原因只剩一句中文公告 + 一行 warning。
    civic_effect_audit_enabled: bool = False
```

2) `/Volumes/data/dev/simverse-world/backend/app/observability.py` —— 在 `CIVIC_FACTS_FAILOPEN` 之后新增：
```python
CIVIC_EFFECT_APPLIED = Counter(
    "civic_effect_applied_total",
    "Civic poll outcomes landed through _execute_outcome, by type and result",
    ["etype", "result"],
)
```

3) `/Volumes/data/dev/simverse-world/backend/app/services/civic_service.py`：

(a) 在 `META_ELIGIBLE_AT_OPEN = "_eligible_at_open"`（:47）之后：
```python
META_EFFECT_APPLIED = "_effect_applied"
META_EFFECT_ERROR = "_effect_error"

#: 执行失败的世界内措辞。错误码不出网,只出这一句(不含坐标、不含内部码)。
_EFFECT_ERROR_NOTE = {
    "invalid_geometry": "但选址不合规,本案未能落成。",
    "unreachable_entrance": "但选址不合规,本案未能落成。",
    "schema_rejected": "但提案内容不合规,本案未能落成。",
}


def _audit(audit: dict | None, key: str, value) -> None:
    """最内层的诊断码优先(setdefault),外层的兜底码不覆盖它。"""
    if audit is not None:
        audit.setdefault(key, value)
```

(b) `_execute_outcome`（:860-862）签名与首行改：
```python
# before
async def _execute_outcome(db, effect: dict, *, poll_id: int | None = None) -> bool:
    """Land a winning outcome through an existing channel. Returns success."""
    etype = effect.get("type")
    try:
# after
async def _execute_outcome(db, effect: dict, *, poll_id: int | None = None,
                          audit: dict | None = None) -> bool:
    """Land a winning outcome through an existing channel. Returns success.

    ``audit`` (opt-in, default None = byte-for-byte pre-P3) collects a short
    diagnosis code: the bool alone cannot tell "unsupported type" from "the
    executor raised"."""
    etype = effect.get("type")
    if audit is not None:
        audit["etype"] = etype
    try:
```

(c) 同函数内 dynamic_location 分支（:891）改：
```python
# before
            return await _add_dynamic_location(db, effect["data"])
# after
            return await _add_dynamic_location(db, effect["data"], audit=audit)
```

(d) 同函数 PolicyImmutableError 分支的 `return False`（:882）之前补 `_audit(audit, "error", "policy_immutable")`；末尾 except/return（:908-910）改：
```python
# before
    except Exception:
        logger.warning("civic outcome execution failed (%s)", etype, exc_info=True)
    return False
# after
    except Exception:
        logger.warning("civic outcome execution failed (%s)", etype, exc_info=True)
        _audit(audit, "error", "exception")
        return False
    _audit(audit, "error", "unsupported_type")
    return False
```

(e) `_add_dynamic_location` 签名（:913）改为 `async def _add_dynamic_location(db, data: dict, *, audit: dict | None = None) -> bool:`；其 `if not slug or "bounds" not in data: return False` 改：
```python
    if not slug or "bounds" not in data:
        _audit(audit, "error", "schema_rejected")
        return False
```
；S6 的 `if _errors:` 块改：
```python
        if _errors:
            logger.warning("civic build rejected (%s): %s",
                           slug, "; ".join(_errors))
            _audit(audit, "error",
                   "unreachable_entrance"
                   if any("reachable" in e for e in _errors)
                   else "invalid_geometry")
            _audit(audit, "detail", _errors[0])
            return False
```

(f) `_close_one_tally`（:733-748）改：
```python
# before
    if effect:
        applied = await _execute_outcome(db, effect, poll_id=poll.id)
# after
    if effect:
        effect_audit: dict = {}
        applied = await _execute_outcome(db, effect, poll_id=poll.id,
                                         audit=effect_audit)
```
和
```python
# before
        else:
            result_note += "议案生效时遇到问题,已记录。"
# after
        else:
            result_note += _EFFECT_ERROR_NOTE.get(
                effect_audit.get("error")
                if settings.civic_effect_audit_enabled else None,
                "议案生效时遇到问题,已记录。")
        if settings.civic_effect_audit_enabled:
            await _record_effect_audit(db, poll, opts, applied, effect_audit)
```
（注意 `_record_effect_audit` 调用与 `else:` 同缩进层级，即仍在 `if effect:` 块内、`await _clerk_announce(` 之前。）

(g) 在 `_VERDICT_NOTE`（:755）之前新增：
```python
async def _record_effect_audit(db, poll, opts: list, applied: bool,
                              audit: dict) -> None:
    """把「胜出了但落没落地」写回 ``options_json[0]``(P3 ⑥)。

    挂 opts[0] 沿用既有 blob 约定(_proposer_slug / _npc_voters /
    _eligible_at_open / _policy_outcome)。**必须排在 _clerk_announce 之前** ——
    公告整段 try/except 吞异常,审计不该排在一个会吞异常的调用之后。
    ``won=True`` 且无 ``_effect_applied`` 键 = 执行途中进程死了,与「执行了但
    失败」(``_effect_applied=False``)可区分 —— 这是把 commit 拆成两次换来的。
    出网侧无需改动:script_service.public_option 是白名单投影。
    """
    try:
        opts[0][META_EFFECT_APPLIED] = bool(applied)
        opts[0][META_EFFECT_ERROR] = audit.get("error")
        poll.options_json = opts
        flag_modified(poll, "options_json")
        await db.commit()
    except Exception:
        logger.warning("civic effect audit write failed (poll=%s)",
                       poll.id, exc_info=True)
    try:
        from app.observability import CIVIC_EFFECT_APPLIED
        CIVIC_EFFECT_APPLIED.labels(
            etype=str(audit.get("etype") or "unknown"),
            result="applied" if applied else str(audit.get("error") or "failed"),
        ).inc()
    except Exception:
        logger.debug("civic effect counter failed", exc_info=True)
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_civic_effect_audit.py tests/test_office_integration.py tests/test_policy_approval_integration.py tests/test_install_mayor_recheck.py tests/test_office_backfill.py tests/test_civic_memory_integration.py -q
```

**验收**：6 条新测试全绿；4 个既有 `_execute_outcome` 调用方测试文件零新增失败（audit 默认 None）；闸关时 `options_json[0]` 不含 `_effect_applied`；闸开+校验开时剧院那张票 `won=True` / `_effect_applied=False` / `_effect_error="invalid_geometry"` 且库中无 theater 行；`public_option` 投影不含任何 `_effect_*` 键。

**commit**：

```
feat(civic): 公投执行结果落库审计——挂 CIVIC_EFFECT_AUDIT_ENABLED,失败原因分码且不出网
```

> #### 🔧 本 step 已被 critic 修订（2 处）
>
> 执行时以下列补丁为准，逐条覆盖上文对应字段。
>
> **修订 1 — 🔴 blocker · 字段 `implementation`**
>
> 处置：critic blocker：「七个 step 各新增一个 Settings 字段却不改 .env.example…每一个都带着已知红入库」
>
> 定位锚点：
>
> ```
> civic_effect_audit_enabled: bool = False
> ```
>
> 替换为：
>
> 【anchor 所在 config.py 块保留；在它与「2) observability.py」之间插入新的一节 1b】
>
> 1b) 同 commit 补两份 env 模板（`CIVIC_` 前缀 → deploy parity 强制双写）。各在 `CIVIC_FACTS_PLACES_DYNAMIC_RESERVE=0` 之后追加：
>
> ```
> # 公投执行结果写回 options_json[0](_effect_applied / _effect_error)+ 计数器。
> # 关(默认) = 失败原因只剩一句中文公告 + 一行 logger.warning,
> #            「类型不支持」「执行抛异常」「选址不合规」三者不可区分。
> # 开 = 错误分码入库并进 CIVIC_EFFECT_APPLIED 计数器;错误码不出网
> #      (script_service.public_option 是白名单投影)。
> CIVIC_EFFECT_AUDIT_ENABLED=false
> ```
>
> **修订 2 — 🔴 blocker · 字段 `verify_cmd`**
>
> 处置：critic blocker：「七个 step 的 verify_cmd 全部追加 tests/test_env_example_consistency.py」
>
> 定位锚点：
>
> ```
> tests/test_civic_effect_audit.py tests/test_office
> ```
>
> 替换为：
>
> tests/test_civic_effect_audit.py tests/test_env_example_consistency.py tests/test_office
>

### P3-S11 — 新楼落成庆典世界事件（CIVIC_BUILD_OPENING_EVENT_ENABLED，默认关） 🔧

**Flag / 批次**：`CIVIC_BUILD_OPENING_EVENT_ENABLED`，默认 `False`。**开闸硬前置**：须同时开 `REALISM_CROWD_ENABLED`（生产实测该行不存在 → 默认 False），否则庆典零位移拉力

**为什么**：冷启动导流全部复用既有系统，不新造权重体系。type 必须是 "festival"：crowd_service._EVENT_TYPES_WITH_CROWD(crowd_service.py:28) 是 ("festival","script") 的闭集，只有这两种能被 active_event_location 看见并拿到 realism_festival_weight=3.0 的抽签偏置（festival_draw_target:207-219 → _maybe_crowd_draw）。衰减天然：×3 是有界偏置不是硬拉，事件到期 refresh_active_events 自动翻 is_active=False，绝不再造第二套衰减。载荷 data["opening_event_days"]=0 默认 = 不开庆典。db.add 后由既有那一次 commit 落盘（同一事务，不新增提交点）。**硬前置**：实测 deploy/backend/.env.example 无 REALISM_CROWD_ENABLED 行 → 生产取代码默认 False，不开那道闸时庆典只进记忆与 decide prompt 的 world_events 段、零位移拉力。

#### 先写的测试（必须跑出失败）

新建 `/Volumes/data/dev/simverse-world/backend/tests/test_civic_opening_event.py`：

```python
"""P3 ④a:新楼落成庆典 —— 复用 festival 那条已在产的人流拉力。"""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.world_event import WorldEvent
from app.services import civic_service
from app.services.crowd_service import _EVENT_TYPES_WITH_CROWD

DATA = {"slug": "post_office", "name": "邮局", "type": "public",
        "bounds": [44, 100, 48, 106], "center": [46, 103],
        "entrance": [46, 100], "opening_event_days": 3}


@pytest.fixture(autouse=True)
def no_world_reload(monkeypatch):
    monkeypatch.setattr("app.lab.apply.reload_world", AsyncMock(return_value=0))
    monkeypatch.setattr("app.lab.apply.publish_world_reload", AsyncMock())


async def _events(db):
    return (await db.execute(select(WorldEvent))).scalars().all()


@pytest.mark.anyio
async def test_gate_off_creates_no_event(db_session):
    assert await civic_service._add_dynamic_location(db_session, dict(DATA)) is True
    assert await _events(db_session) == []


@pytest.mark.anyio
async def test_gate_on_stages_a_festival(db_session, monkeypatch):
    monkeypatch.setattr(settings, "civic_build_opening_event_enabled", True)
    assert await civic_service._add_dynamic_location(db_session, dict(DATA)) is True
    events = await _events(db_session)
    assert len(events) == 1
    ev = events[0]
    assert ev.type in _EVENT_TYPES_WITH_CROWD, \
        "只有 festival/script 能被 active_event_location 看见并拿到 x3 偏置"
    assert ev.type == "festival"
    assert ev.payload_json == {"location_id": "post_office", "opening": True}
    assert ev.is_active is False, "由 refresh_active_events 翻,与既有 narrative 分支同形"
    assert (ev.ends_at - ev.starts_at).days == 3


@pytest.mark.anyio
async def test_zero_or_missing_days_stages_nothing(db_session, monkeypatch):
    monkeypatch.setattr(settings, "civic_build_opening_event_enabled", True)
    assert await civic_service._add_dynamic_location(
        db_session, {**DATA, "opening_event_days": 0}) is True
    assert await _events(db_session) == []
    assert await civic_service._add_dynamic_location(
        db_session, {k: v for k, v in DATA.items() if k != "opening_event_days"}
    ) is True
    assert await _events(db_session) == []


@pytest.mark.anyio
async def test_days_are_capped_and_bogus_values_ignored(db_session, monkeypatch):
    monkeypatch.setattr(settings, "civic_build_opening_event_enabled", True)
    assert await civic_service._add_dynamic_location(
        db_session, {**DATA, "opening_event_days": 999}) is True
    events = await _events(db_session)
    assert (events[0].ends_at - events[0].starts_at).days == \
        civic_service._OPENING_EVENT_MAX_DAYS
    assert await civic_service._add_dynamic_location(
        db_session, {**DATA, "slug": "theater2", "opening_event_days": True}) is True
    assert len(await _events(db_session)) == 1, "bool 是 int 子类,不许当天数用"


@pytest.mark.anyio
async def test_event_is_committed_with_the_building(db_session, monkeypatch):
    """同一次 commit —— 不新增提交点,楼在事件就在。"""
    monkeypatch.setattr(settings, "civic_build_opening_event_enabled", True)
    await civic_service._add_dynamic_location(db_session, dict(DATA))
    db_session.expire_all()
    assert len(await _events(db_session)) == 1
```

实现前：`test_gate_on_stages_a_festival` 等三条红（`AttributeError: settings 无 civic_build_opening_event_enabled`）。

#### 实现

1) `/Volumes/data/dev/simverse-world/backend/app/config.py` —— 紧跟 S10 那行之后：
```python
    # 新楼落成庆典(effect.data 的 opening_event_days 控制天数,0/缺省=不开)。
    # 注意:真要产生位移拉力还须 REALISM_CROWD_ENABLED —— 那道闸生产默认 False。
    civic_build_opening_event_enabled: bool = False
```

2) `/Volumes/data/dev/simverse-world/backend/app/services/civic_service.py` —— 在 `_add_dynamic_location` **之前**新增：
```python
_OPENING_EVENT_MAX_DAYS = 7


def _maybe_stage_opening_event(db, slug: str, data: dict) -> int:
    """新楼落成庆典(P3 ④a):一条 ``type="festival"`` 的世界事件。

    为什么必须是 festival —— ``crowd_service._EVENT_TYPES_WITH_CROWD``
    (crowd_service.py:28) 是 ("festival", "script") 的闭集,只有这两种能被
    ``active_event_location`` 看见并拿到 ``realism_festival_weight``(×3) 的抽签
    偏置。衰减是天然的:×3 是有界偏置不是硬拉,到期由 ``refresh_active_events``
    翻 ``is_active=False`` —— **不再造第二套衰减权重**。

    只 ``db.add``,由调用方那一次 commit 落盘(同一事务,不新增提交点)。
    返回实际天数(0 = 没开)。
    """
    if not settings.civic_build_opening_event_enabled:
        return 0
    raw = data.get("opening_event_days")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return 0
    days = min(raw, _OPENING_EVENT_MAX_DAYS)
    from app.models.world_event import WorldEvent
    now = datetime.now(UTC)
    name = data.get("name") or slug
    db.add(WorldEvent(
        type="festival", title=f"{name}落成",
        description=f"{name}今天开门,镇上的人陆续过去看热闹。",
        payload_json={"location_id": slug, "opening": True},
        starts_at=now, ends_at=now + timedelta(days=days),
        is_active=False,
    ))
    return days
```

3) 同文件 `_add_dynamic_location` 内，`existing.active = True` 之后、`await db.commit()` 之前插入：
```python
    _maybe_stage_opening_event(db, slug, data)
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_civic_opening_event.py tests/test_civic_build_wiring.py tests/test_crowd_service.py -q
```

**验收**：5 条新测试全绿；`tests/test_civic_build_wiring.py`/`test_crowd_service.py` 零新增失败；闸关时落库后 world_events 表为空；闸开+`opening_event_days=3` 时恰有 1 条 `type="festival"`、`payload_json={"location_id":slug,"opening":True}`、`is_active=False`、跨度 3 天的事件，且 `opening_event_days ∈ {0, 缺省, True}` 时均不建事件。

**commit**：

```
feat(civic): 新楼落成庆典复用 festival 人流拉力——挂 CIVIC_BUILD_OPENING_EVENT_ENABLED,与建楼同一次 commit
```

> #### 🔧 本 step 已被 critic 修订（2 处）
>
> 执行时以下列补丁为准，逐条覆盖上文对应字段。
>
> **修订 1 — 🔴 blocker · 字段 `implementation`**
>
> 处置：critic blocker：env 拆回各自 step；并落实「REALISM_CROWD_ENABLED 在 deploy 模板根本没有这一行（生产取代码默认 False）」这条已实测事实
>
> 定位锚点：
>
> ```
> civic_build_opening_event_enabled: bool = False
> ```
>
> 替换为：
>
> 【anchor 所在 config.py 块保留；在它与「2) civic_service.py」之间插入新的一节 1b】
>
> 1b) 同 commit 补两份 env 模板（`CIVIC_` 前缀 → deploy parity 强制双写）。各在 `CIVIC_EFFECT_AUDIT_ENABLED=false` 之后追加：
>
> ```
> # 新楼落成庆典:一条 type="festival" 的世界事件(天数取 effect.data 的
> # opening_event_days,0/缺省/bool = 不开,上限 7 天),与建楼同一次 commit。
> # 关(默认) = 不建任何事件。
> # ⚠ 开闸硬前置:只有**同时**开 REALISM_CROWD_ENABLED 才有位移拉力 ——
> #   crowd_service._EVENT_TYPES_WITH_CROWD 是 ("festival","script") 闭集,
> #   x3 抽签偏置由那道闸把关;不开就只有记忆与 prompt 里提一嘴,零人流。
> #   deploy 模板实测原本没有 REALISM_CROWD_ENABLED 赋值行(生产取代码默认
> #   False),故本 step 顺手在两份模板里显式写出 REALISM_CROWD_ENABLED=false,
> #   免得开闸时误以为它已经是开的。
> CIVIC_BUILD_OPENING_EVENT_ENABLED=false
> REALISM_CROWD_ENABLED=false
> ```
>
> **修订 2 — 🔴 blocker · 字段 `verify_cmd`**
>
> 处置：critic blocker：「七个 step 的 verify_cmd 全部追加 tests/test_env_example_consistency.py」
>
> 定位锚点：
>
> ```
> tests/test_civic_opening_event.py tests/test_civic
> ```
>
> 替换为：
>
> tests/test_civic_opening_event.py tests/test_env_example_consistency.py tests/test_civic
>

### P3-S12 — .env.example 补七道新闸与开闸硬顺序（backend + deploy 两份） 🔧

**Flag / 批次**：无新增闸；本 step 只登记 S5–S11 的七道闸（全部 false / 0）

**为什么**：仓内先例：79ef1ce 把 TOWN_DUTY_FUNDING 的开闸硬顺序写进 .env.example 而不是散在 commit body 里。P3 的顺序约束有两条会造成「假报失败」的坑必须写死：① LOCATION_SPECIFIC_FIRST_ENABLED 必须先于任何依赖 get_location_id_at 的 P1 能力闸，否则新楼里的能力门恒 False；② CIVIC_BUILD_OPENING_EVENT_ENABLED 不配 REALISM_CROWD_ENABLED 就没有位移拉力，而后者在 deploy 那份里根本没有这一行（生产取代码默认 False）。

#### 先写的测试（必须跑出失败）

新建 `/Volumes/data/dev/simverse-world/backend/tests/test_p3_env_documentation.py`：

```python
"""P3 开闸硬顺序必须写在 .env.example 里(先例:79ef1ce TOWN_DUTY_FUNDING)。

七道新闸散在 config.py 各处;运维只读 .env.example。漏一条就会出现
「开了 P1 能力闸却发现新楼里啥也判不出来」这种假报失败。
"""
from pathlib import Path

import pytest

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent
BACKEND_ENV = ROOT / ".env.example"
DEPLOY_ENV = ROOT.parent / "deploy" / "backend" / ".env.example"

P3_FLAGS = (
    "CIVIC_BUILD_SCHEMA_ENABLED",
    "CIVIC_BUILD_VALIDATE_ENABLED",
    "WORLD_RELOAD_RESET_PATH_CACHE",
    "LOCATION_SPECIFIC_FIRST_ENABLED",
    "CIVIC_EFFECT_AUDIT_ENABLED",
    "CIVIC_BUILD_OPENING_EVENT_ENABLED",
    "CIVIC_FACTS_PLACES_DYNAMIC_RESERVE",
)


@pytest.mark.parametrize("env_file", [BACKEND_ENV, DEPLOY_ENV])
@pytest.mark.parametrize("flag", P3_FLAGS)
def test_flag_is_documented_and_off(env_file, flag):
    text = env_file.read_text(encoding="utf-8")
    assert f"{flag}=" in text, f"{env_file.name} 缺 {flag}"
    line = next(ln for ln in text.splitlines()
                if ln.startswith(f"{flag}="))
    assert line.split("=", 1)[1].strip() in {"false", "0"}, \
        f"{env_file.name} 的 {flag} 不是默认关:{line}"


def test_defaults_match_the_settings_class():
    s = Settings()
    assert s.civic_build_schema_enabled is False
    assert s.civic_build_validate_enabled is False
    assert s.world_reload_reset_path_cache is False
    assert s.location_specific_first_enabled is False
    assert s.civic_effect_audit_enabled is False
    assert s.civic_build_opening_event_enabled is False
    assert s.civic_facts_places_dynamic_reserve == 0


@pytest.mark.parametrize("env_file", [BACKEND_ENV, DEPLOY_ENV])
def test_hard_ordering_is_spelled_out(env_file):
    text = env_file.read_text(encoding="utf-8")
    assert "P3 开闸硬顺序" in text
    assert "REALISM_CROWD_ENABLED" in text, \
        "落成庆典没有这道闸就零位移拉力,必须点名"
    assert "LOCATION_SPECIFIC_FIRST_ENABLED" in text
```

实现前：15 条 parametrize 全红（两份 .env.example 都没有这些行）。注意 `CIVIC_FACTS_PLACES_DYNAMIC_RESERVE` 的值写 `0`，测试的取值域已包含它。

#### 实现

1) `/Volumes/data/dev/simverse-world/backend/.env.example` —— 在 `CIVIC_FACTS_ENABLED=false`（:830）之后追加：
```
# --- P3 公投建楼接线(七道闸,默认全关) ---------------------------------
# P3 开闸硬顺序(照做,否则会出现「开了闸却像没生效」的假报):
#   1) WORLD_RELOAD_RESET_PATH_CACHE —— 先让运行中新建的楼当场可达
#      (reload_world 不清 pathfinder 缓存时,新楼 find_path 恒 None 直到重启)
#   2) LOCATION_SPECIFIC_FIRST_ENABLED —— **必须先于任何依赖 get_location_id_at
#      的 P1 能力闸**;不开的话邮局/剧院被 south_quarter/east_gardens 遮蔽,
#      新楼里的能力门恒判 False,会被误读成「P1 接线失败」
#   3) CIVIC_BUILD_SCHEMA_ENABLED + CIVIC_BUILD_VALIDATE_ENABLED —— 校验先于
#      下一次公投建楼生效
#   4) CIVIC_EFFECT_AUDIT_ENABLED / CIVIC_FACTS_PLACES_DYNAMIC_RESERVE —— 独立,
#      任意顺序
#   5) CIVIC_BUILD_OPENING_EVENT_ENABLED —— 只有**同时**开 REALISM_CROWD_ENABLED
#      才有位移拉力(festival x3 偏置由它把关);不开就只有记忆与 prompt 提及
WORLD_RELOAD_RESET_PATH_CACHE=false
LOCATION_SPECIFIC_FIRST_ENABLED=false
CIVIC_BUILD_SCHEMA_ENABLED=false
CIVIC_BUILD_VALIDATE_ENABLED=false
CIVIC_EFFECT_AUDIT_ENABLED=false
CIVIC_BUILD_OPENING_EVENT_ENABLED=false
# 0 = 逐字节旧行为(新楼总被静态设施挤出 PLACES_LIMIT);建议开到 2
CIVIC_FACTS_PLACES_DYNAMIC_RESERVE=0
```

2) `/Volumes/data/dev/simverse-world/deploy/backend/.env.example` —— 在 `CIVIC_FACTS_ENABLED=false`（:380）之后追加**同一段文本**（逐字一致）。

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_p3_env_documentation.py -q
```

**验收**：17 条测试全绿；两份 .env.example 各含 7 行 P3 闸且值均为 `false`/`0`；两份都出现字符串「P3 开闸硬顺序」「REALISM_CROWD_ENABLED」「LOCATION_SPECIFIC_FIRST_ENABLED」；`Settings()` 的七个默认值与文档一致。

**commit**：

```
docs(env): P3 建楼接线七道闸与开闸硬顺序——specific_first 须先于 P1 能力闸,庆典须配 REALISM_CROWD
```

> #### 🔧 本 step 已被 critic 修订（1 处）
>
> 执行时以下列补丁为准，逐条覆盖上文对应字段。
>
> **修订 1 — 🔴 blocker · 字段 `implementation`**
>
> 处置：critic blocker：「P3-S12 退化为只写开闸硬顺序 + 顺序断言，不再承担补键职责」+「删掉『P1 能力闸的硬前置』这句，改为列出真实依赖方」
>
> 定位锚点：
>
> ```
> # --- P3 公投建楼接线(七道闸,默认全关) ---
> ```
>
> 替换为：
>
> 【把 anchor 起、到该 fenced 代码块结尾（含七行 `KEY=false` / `=0` 赋值）的整段替换为下面这段；七个键的赋值已由 S5–S11 各自同 commit 写入，本 step **只写顺序**，不再重复赋值】
>
> ```
> # --- P3 开闸硬顺序（七道闸的赋值行由各自 step 已写入本文件，此处只排顺序）---
> #  1) WORLD_RELOAD_RESET_PATH_CACHE —— 先让运行中新建的楼当场可达
> #  2) LOCATION_SPECIFIC_FIRST_ENABLED —— 真实依赖方是:decide/basic.py 的
> #     dining 判定、_maybe_shelter 的 location_is_indoor、location_tracker 首访、
> #     location_lore、/exploration/me。**它不是 P1 能力闸的前置**:P1 的两个能力门
> #     走 map_data.capability_location_at(自带穿透遮蔽的最小面积匹配),不经
> #     get_location_id_at —— 旧文案里那句「必须先于任何依赖 get_location_id_at 的
> #     P1 能力闸」是错的,照它排会假报「P1 接线失败」。
> #     商队路面语义已由 P3-S8b 解耦(outdoor_container_at),不再受本闸影响,
> #     但 **S8b 必须先合入** 再翻本闸。
> #     ⚠ 本闸一次性不可逆:翻开当下触发 post_office/theater 的
> #     location_first_visit → explorer_* 成就与赛季分,关回去收不回。
> #  3) CIVIC_BUILD_SCHEMA_ENABLED + CIVIC_BUILD_VALIDATE_ENABLED —— 校验先于
> #     下一次公投建楼生效
> #  4) CIVIC_EFFECT_AUDIT_ENABLED / CIVIC_FACTS_PLACES_DYNAMIC_RESERVE(任意序)
> #  5) CIVIC_BUILD_OPENING_EVENT_ENABLED —— 须同时开 REALISM_CROWD_ENABLED,
> #     否则庆典零位移拉力
> ```
>
> （两份 `.env.example` 逐字一致。本 step 的红点是 `test_hard_ordering_is_spelled_out` 两条；七个键的 presence/默认值那 14 条参数化在本 step 起点即绿——它们是 S5–S11 留下的回归守卫，请在 test_first 的注释里写明，别把「起点即绿」误当假 TDD。）
>

### P3-S13 — 【迁移批次，不得与开闸同批】alembic 068：把存量 theater 坐标收进 walkable 域 🔧

**Flag / 批次**：无 feature flag。**迁移批次，不得与开闸同批**：必须在 S6（校验）与 S7（缓存失效）已合入并开闸稳定之后，单独一次部署跑；跑完须触发一次 `reload_world`（S7 开闸后会连带清路径缓存）

**为什么**：实测 theater bounds x2=178 / center x=175 越过 WALKABLE_X_RANGE 上限 173；center (175,45) 被 _get_forced_walkable 强标 walkable 但 reachable=False、find_path 返 None，今天不咬人只因 get_valid_target_tile(map_data.py:453) 有 entrance 就永不取 center —— 谁把 entrance 改掉/删掉，剧院立刻变成不可达孤岛。修坐标必须**排在 S6/S7 之后**：先加校验（纯代码+默认关闸）挡住新楼产能，再修存量；反过来是「修了存量没修产能，下一张票照样能再建一栋越界楼」。新 bounds (168,40,173,50) / center (170,45)，entrance 保持 (172,45)（仍落在新 bounds 内，否则 apply.py:78-84 的 entrance-within-bounds 会自判红）。sa.JSON 在 PG 上是 json 不是 jsonb，用 SQLAlchemy Core table 构造读写才方言安全（不能裸 text 拼 json 字符串）。

#### 先写的测试（必须跑出失败）

新建 `/Volumes/data/dev/simverse-world/backend/tests/test_theater_bounds_migration.py`：

```python
"""P3 批 2:存量剧院坐标收进 walkable 域(迁移;与开闸分属不同批次)。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from app.world_geometry import WALKABLE_X_RANGE, WALKABLE_Y_RANGE

MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "alembic" / "versions" / "068_fix_theater_bounds.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_068", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _table(metadata):
    return sa.Table(
        "dynamic_locations", metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("slug", sa.String),
        sa.Column("data_json", sa.JSON),
    )


def test_068_chains_after_067_and_repository_has_single_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    script = ScriptDirectory.from_config(Config(str(ini)))
    assert script.get_heads() == ["068_fix_theater_bounds"]
    rev = script.get_revision("068_fix_theater_bounds")
    assert rev.down_revision == "067_market_economy_loop"
    assert len(rev.revision) <= 32


def test_068_moves_theater_into_the_walkable_band_and_is_idempotent():
    module = _load_migration()
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    rows = _table(metadata)
    metadata.create_all(engine)

    old = {"name": "剧院", "type": "public", "role": "culture",
           "bounds": [172, 40, 178, 50], "center": [175, 45],
           "entrance": [172, 45],
           "description": "小镇剧院:说书、演展、故事会的舞台",
           "boosted_actions": ["CHAT_RESIDENT", "OBSERVE"]}
    other = {"name": "邮局", "type": "public", "bounds": [44, 100, 48, 106]}
    with engine.begin() as conn:
        conn.execute(rows.insert(), [
            {"id": "t", "slug": "theater", "data_json": old},
            {"id": "p", "slug": "post_office", "data_json": other},
        ])
        assert module._rewrite(conn, module._NEW, module._OLD) == 1
        assert module._rewrite(conn, module._NEW, module._OLD) == 0, "幂等"
        stored = dict(conn.execute(
            sa.select(rows.c.slug, rows.c.data_json)).all())

    theater = stored["theater"]
    assert theater["bounds"] == [168, 40, 173, 50]
    assert theater["center"] == [170, 45]
    assert theater["entrance"] == [172, 45], "入口不动 —— 实测它可达"
    for key in ("name", "type", "role", "description", "boosted_actions"):
        assert theater[key] == old[key], f"{key} 不该被迁移碰"
    assert stored["post_office"] == other, "只动剧院这一行"

    x1, y1, x2, y2 = theater["bounds"]
    assert x1 in WALKABLE_X_RANGE and x2 in WALKABLE_X_RANGE
    assert y1 in WALKABLE_Y_RANGE and y2 in WALKABLE_Y_RANGE
    assert theater["center"][0] in WALKABLE_X_RANGE
    ex, ey = theater["entrance"]
    assert x1 <= ex <= x2 and y1 <= ey <= y2, \
        "entrance 必须仍落在新 bounds 内(apply.py:78-84 会判它)"


def test_068_downgrade_restores_the_original_and_skips_foreign_shapes():
    module = _load_migration()
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    rows = _table(metadata)
    metadata.create_all(engine)

    hand_edited = {"name": "剧院", "type": "public",
                   "bounds": [100, 40, 105, 50], "center": [102, 45]}
    with engine.begin() as conn:
        conn.execute(rows.insert(),
                     [{"id": "t", "slug": "theater", "data_json": hand_edited}])
        assert module._rewrite(conn, module._NEW, module._OLD) == 0, \
            "生产被手工动过的行不许被盲目覆盖"
        conn.execute(rows.update().values(data_json={
            "name": "剧院", "type": "public",
            "bounds": [168, 40, 173, 50], "center": [170, 45]}))
        assert module._rewrite(conn, module._OLD, module._NEW) == 1
        back = conn.execute(sa.select(rows.c.data_json)).scalar_one()
    assert back["bounds"] == [172, 40, 178, 50]
    assert back["center"] == [175, 45]


def test_068_is_a_noop_without_the_row():
    module = _load_migration()
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    _table(metadata)
    metadata.create_all(engine)
    with engine.begin() as conn:
        assert module._rewrite(conn, module._NEW, module._OLD) == 0
```

实现前：collect 阶段 `FileNotFoundError` / `spec is None` → 整文件红。

#### 实现

新建 `/Volumes/data/dev/simverse-world/backend/alembic/versions/068_fix_theater_bounds.py`（全文）：

```python
"""Move the voted theater into the walkable band.

Revision ID: 068_fix_theater_bounds
Revises: 067_market_economy_loop
Create Date: 2026-08-17

WALKABLE_X_RANGE tops out at x=173 (world_geometry.py:9) while the theater was
built with bounds x2=178 and center x=175. The center is force-marked walkable
by pathfinder._get_forced_walkable but is NOT in the hub-connected component,
so find_path to it returns None. It is harmless today only because
map_data.get_valid_target_tile prefers ``entrance`` and never falls back to
``center`` when one exists — deleting that entrance would strand the building.

The entrance (172,45) is measured reachable and stays put; it still lies inside
the new bounds, which apply.validate_location_patch requires.

Data-only, idempotent, and deliberately shipped in its own batch: no code
behaviour changes and no gate is flipped here.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "068_fix_theater_bounds"
down_revision = "067_market_economy_loop"
branch_labels = None
depends_on = None

_OLD = {"bounds": [172, 40, 178, 50], "center": [175, 45]}
_NEW = {"bounds": [168, 40, 173, 50], "center": [170, 45]}

_rows = sa.table(
    "dynamic_locations",
    sa.column("id", sa.String),
    sa.column("slug", sa.String),
    sa.column("data_json", sa.JSON),
)


def _rewrite(connection, new: dict, old: dict) -> int:
    """Portable row rewrite used by upgrade/downgrade and by the tests.

    Returns the number of rows changed. Only touches a row whose coordinates
    still match ``old`` exactly, so a hand-edited production row is left alone
    and a re-run is a no-op. Uses Core constructs (never a raw json string) so
    the sa.JSON column round-trips on both Postgres and sqlite.
    """
    row = connection.execute(
        sa.select(_rows.c.id, _rows.c.data_json).where(_rows.c.slug == "theater")
    ).fetchone()
    if row is None:
        return 0
    data = dict(row[1] or {})
    try:
        current = [int(v) for v in (data.get("bounds") or [])]
    except (TypeError, ValueError):
        return 0
    if current != old["bounds"]:
        return 0
    data["bounds"] = list(new["bounds"])
    data["center"] = list(new["center"])
    connection.execute(
        _rows.update().where(_rows.c.id == row[0]).values(data_json=data)
    )
    return 1


def upgrade() -> None:
    _rewrite(op.get_bind(), _NEW, _OLD)


def downgrade() -> None:
    _rewrite(op.get_bind(), _OLD, _NEW)
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_theater_bounds_migration.py tests/test_ugc_privilege_migration.py -q && .venv/bin/python -m alembic heads
```

**验收**：4 条新测试全绿；`tests/test_ugc_privilege_migration.py::test_065_chains_after_quota_migration_and_repository_has_single_head` 需同步把 `script.get_heads()` 的期望改成 `["068_fix_theater_bounds"]` 后全绿；`alembic heads` 输出单 head = `068_fix_theater_bounds (head)`；迁移后 theater 的 bounds/center 全部落在 [14,173]×[12,123] 内且 entrance (172,45) 仍在 bounds 内、其余键逐字未动。

**commit**：

```
migrate(world): 068 把存量剧院 bounds/center 收进 walkable 域——数据迁移,不含代码行为变更与开闸
```

> #### 🔧 本 step 已被 critic 修订（1 处）
>
> 执行时以下列补丁为准，逐条覆盖上文对应字段。
>
> **修订 1 — 🔴 blocker · 字段 `implementation`**
>
> 处置：critic blocker：「两处 head 断言的更新写进 S13 的 implementation（不是只写在 acceptance 里）…verify_cmd 补上 tests/test_caravan_lifecycle_migration.py 与 alembic heads」
>
> 定位锚点：
>
> ```
> backend/alembic/versions/068_fix_theater_bounds.py`（全文）
> ```
>
> 替换为：
>
> backend/alembic/versions/068_fix_theater_bounds.py`（全文），**并在同一 commit 内改掉两处硬编码 head 断言（不改它们 = 本 commit 必带两条已知红）**：
>
> 2) `/Volumes/data/dev/simverse-world/backend/tests/test_ugc_privilege_migration.py:31`
> ```python
> # before
>     assert script.get_heads() == ["067_market_economy_loop"]
> # after
>     assert script.get_heads() == ["068_fix_theater_bounds"]
> ```
>
> 3) `/Volumes/data/dev/simverse-world/backend/tests/test_caravan_lifecycle_migration.py:55` —— 同一行同款断言（实测该文件 `:18` / `:31` 用的是 `len(script.get_heads()) == 1`，只有 `:55` 硬编码了 067），同步改成 `["068_fix_theater_bounds"]`。
>
> 4) 本 step 的 verify_cmd 同批改成（原命令跑不到 caravan 那条，会自报绿）：
> ```
> cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_theater_bounds_migration.py tests/test_ugc_privilege_migration.py tests/test_caravan_lifecycle_migration.py -q && .venv/bin/python -m alembic heads
> ```
>
> 5) 批次归属不变：本 step 仍是**纯数据迁移**——两处改动只是测试对 head 的适配，不改任何生产代码行为、不翻任何闸，不触碰「迁移与开闸不得同批」红线。
>
> （下面是 068 迁移文件全文，保持原样）
>

### P3-S14 — CIVIC_AGENDA 剧院坐标字面量同步（只改 data，一个字不动 topic）

**Flag / 批次**：无 feature flag。属**代码批次**，必须与 S13 分开 commit / 分开部署；本改动不影响已结票的既有 poll，只影响该 effect 的将来重执行

**为什么**：S13 只修了 dynamic_locations 里的存量行；CIVIC_AGENDA(civic_service.py:189-195) 的字面量仍是越界坐标，将来任何一次该 effect 重执行（或 admin 经 routers/polls.py 复用这份载荷）都会把楼又建回界外。硬约束：seed_civic_agenda 的幂等键是 `Poll.question` 精确匹配（civic_service.py:208-210），**改一个字符就会重开一张新票**，所以只改 options[0].effect.data 的 bounds/center，topic 与 label 逐字不动。单独 commit（S13 是迁移、本 step 是代码，两者不得混批）。

#### 先写的测试（必须跑出失败）

新建 `/Volumes/data/dev/simverse-world/backend/tests/test_civic_agenda_geometry.py`：

```python
"""CIVIC_AGENDA 的建楼载荷必须自己过得了 P3 的几何校验。

seed_civic_agenda 的幂等键是 Poll.question 精确匹配(civic_service.py:208-210),
所以坐标可以改、topic 一个字都不能动 —— 改了就是重开一张票。
"""
import pytest

from app.agent import pathfinder
from app.lab.apply import validate_location_patch
from app.services.civic_service import CIVIC_AGENDA

TOPICS = ("在南苑空地兴建一座邮局", "在东岸花园兴建一座剧院")


@pytest.fixture(autouse=True)
def fresh_path_cache():
    pathfinder.reset_walkable_cache()
    yield
    pathfinder.reset_walkable_cache()


def test_topics_are_frozen():
    """幂等键 —— 动一个字就会给已建成的楼重开一张票。"""
    assert tuple(item["topic"] for item in CIVIC_AGENDA) == TOPICS


@pytest.mark.parametrize("idx", range(2))
def test_agenda_build_payload_passes_p3_validation(idx):
    data = CIVIC_AGENDA[idx]["options"][0]["effect"]["data"]
    errors, _ = validate_location_patch(
        {"slug": data["slug"],
         "data": {k: v for k, v in data.items() if k != "slug"}},
        allow_existing_slug=True,
        outdoor_overlap_is_warning=True,
        require_walkable_range=True,
        require_reachable_entrance=True,
    )
    assert errors == [], f"{data['slug']} 的 agenda 坐标过不了校验:{errors}"


def test_theater_literal_matches_the_068_migration():
    data = CIVIC_AGENDA[1]["options"][0]["effect"]["data"]
    assert data["slug"] == "theater"
    assert data["bounds"] == [168, 40, 173, 50]
    assert data["center"] == [170, 45]
    assert data["entrance"] == [172, 45]


def test_post_office_literal_is_untouched():
    data = CIVIC_AGENDA[0]["options"][0]["effect"]["data"]
    assert data["bounds"] == [44, 100, 48, 106]
    assert data["center"] == [46, 103]
    assert data["entrance"] == [46, 100]
```

实现前：`test_agenda_build_payload_passes_p3_validation[1]` 与 `test_theater_literal_matches_the_068_migration` 两条红（剧院字面量仍是 172..178 / 175）。

#### 实现

改 `/Volumes/data/dev/simverse-world/backend/app/services/civic_service.py` 的 `CIVIC_AGENDA` 第二条（:189-191），**只改两行坐标**：

```python
# before
            {"label": "赞成兴建", "effect": {"type": "dynamic_location", "data": {
                "slug": "theater", "name": "剧院", "type": "public", "role": "culture",
                "bounds": [172, 40, 178, 50], "center": [175, 45], "entrance": [172, 45],
# after
            {"label": "赞成兴建", "effect": {"type": "dynamic_location", "data": {
                "slug": "theater", "name": "剧院", "type": "public", "role": "culture",
                # 168..173 收进 WALKABLE_X_RANGE(上限 173)。旧值 x2=178 / center
                # x=175 是孤岛,存量行已由 alembic 068 改过 —— 这里同步字面量,
                # 否则该 effect 重执行会把楼又建回界外。
                # **topic 一个字都不许动**:seed_civic_agenda 的幂等键是
                # Poll.question 精确匹配(:208-210),改了就是重开一张票。
                "bounds": [168, 40, 173, 50], "center": [170, 45], "entrance": [172, 45],
```

其余键（description / boosted_actions / role / label / topic / proposer_slug）逐字不动。

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_civic_agenda_geometry.py tests/test_civic_build_wiring.py tests/test_civic_effect_audit.py -q
```

**验收**：5 条新测试全绿；两条 agenda 载荷在四条 P3 规则（upsert / outdoor 降级 / walkable 域 / 入口可达）下 errors 均为空；`CIVIC_AGENDA` 的两个 topic 字符串与改前逐字一致；邮局字面量三组坐标未变。

**commit**：

```
fix(civic): CIVIC_AGENDA 剧院坐标同步 068 迁移——只改 effect.data,topic 逐字不动(幂等键)
```

## P3 新增 step（critic 要求）

### P3-S8b — 商队路面判据改用 outdoor 容器反查——与 LOCATION_SPECIFIC_FIRST_ENABLED 解耦（无闸） 🆕

**Flag / 批次**：无 feature flag（纯语义解耦，闸的两种状态下路网输出逐字节相同）。**批次约束**：必须与 P3-S8 同批合入，且**早于** `LOCATION_SPECIFIC_FIRST_ENABLED` 翻开——先翻闸后合它，中间窗口里任何落在走廊内的新楼会让商队静默停摆。

**为什么**：已回源码确认：`caravan_route._caravan_tile_allowed`(caravan_route.py:317-321) 用 `get_location_id_at(*tile)` 判「这格是不是开放路面」，只放行 `None` / `market_hall` / `type=="outdoor"`；而集市大道 `_MARKET_AVENUE_X_BOUNDS=(100,104)` × `_MARKET_AVENUE_Y_BOUNDS=(58, MAP_HEIGHT_TILES-1)` 穿过 `south_quarter(42,100,135,109)` 这个 outdoor 街区。今天走廊安全恰恰是因为遮蔽：落在街区内的任何楼都被首命中解析成 outdoor。P3-S8 一开闸，落在走廊里的新 public 楼会把那几格翻成非 outdoor → `_semantic_open_tiles` 出缺口 → `_path_or_raise` 抛 → `build_caravan_route()` RuntimeError → 商队每次到访被置 `phase='cancelled'` / `error_code='route_unreachable'`，静默停摆只留一行 exception 日志；P3-S6 的落库校验放行它（与 outdoor 重叠只降级 warning、walkable 域与入口可达都过），P3-S7 每次 reload 又清 `lru_cache`，故障会在公投落地当场爆发而非等重启。取 critic 的方案 (b)：商队看「地面容器」、NPC 看「具体地点」，两套语义就此解耦；顺带 outdoor 只有 6 条，比扫 28 条建筑更快。不取方案 (a)（保留走廊硬拒）：它把「不能在走廊上盖楼」变成公投的隐藏规则，且对存量楼无效。

#### 先写的测试

新建 `/Volumes/data/dev/simverse-world/backend/tests/test_caravan_route_semantics.py`：

```python
"""P3:商队路面语义必须与 NPC 地点具体性解耦。

_caravan_tile_allowed 用 get_location_id_at 判开放路面 —— 集市大道
x∈[100,104] 穿过 south_quarter(42,100,135,109),今天安全全靠遮蔽。
"""
import pytest

from app.agent import map_data
from app.config import settings
from app.services import caravan_route

CORRIDOR_TILE = (102, 104)   # 大道 ∩ south_quarter


@pytest.fixture
def kiosk_in_the_corridor():
    snap = {k: dict(v) for k, v in map_data.LOCATIONS.items()}
    snap_dyn = set(map_data._dynamic_slugs)
    map_data.LOCATIONS["kiosk"] = {"name": "报刊亭", "type": "public",
                                   "bounds": (100, 102, 104, 106),
                                   "center": (102, 104),
                                   "entrance": (102, 102)}
    map_data._dynamic_slugs.add("kiosk")
    map_data.rebuild_bounds_order()
    caravan_route.build_caravan_route.cache_clear()
    yield
    map_data.LOCATIONS.clear()
    map_data.LOCATIONS.update(snap)
    map_data._dynamic_slugs = snap_dyn
    map_data.rebuild_bounds_order()
    caravan_route.build_caravan_route.cache_clear()


def test_outdoor_container_ignores_buildings(kiosk_in_the_corridor):
    """新查表永远只认 outdoor 容器,不受具体性优先影响。"""
    assert map_data.outdoor_container_at(*CORRIDOR_TILE) == "south_quarter"
    assert map_data.outdoor_container_at(20, 20) is None   # academy 不是地面
    assert map_data.outdoor_container_at(0, 0) is None


def test_corridor_survives_a_building_with_the_gate_on(
        kiosk_in_the_corridor, monkeypatch):
    monkeypatch.setattr(settings, "location_specific_first_enabled", True)
    assert map_data.get_location_id_at(*CORRIDOR_TILE) == "kiosk"
    assert caravan_route._caravan_tile_allowed(CORRIDOR_TILE) is True
    caravan_route.build_caravan_route()   # 不抛 = 路网没断链


def test_a_building_outside_any_outdoor_block_is_still_refused():
    assert caravan_route._caravan_tile_allowed((20, 20)) is False


def test_route_is_identical_across_the_gate(kiosk_in_the_corridor, monkeypatch):
    caravan_route.build_caravan_route.cache_clear()
    off = caravan_route.build_caravan_route()
    off_path, off_park = off.full_path, off.market_hall_parking
    monkeypatch.setattr(settings, "location_specific_first_enabled", True)
    caravan_route.build_caravan_route.cache_clear()
    on = caravan_route.build_caravan_route()
    assert on.full_path == off_path
    assert on.market_hall_parking == off_park
```

实现前：`AttributeError: module 'app.agent.map_data' has no attribute 'outdoor_container_at'`（前两条），以及 `test_corridor_survives_a_building_with_the_gate_on` / `test_route_is_identical_across_the_gate` 因路面缺口抛 RuntimeError 而红。

#### 实现

1) `/Volumes/data/dev/simverse-world/backend/app/agent/map_data.py` —— 在 `_find_location_in_bounds` 之后新增：

```python
def outdoor_container_at(x: int, y: int) -> str | None:
    """只看 outdoor 街区的坐标反查:「这格属于哪块地面」。

    与 ``get_location_id_at``(「站在哪个具体地点」)是两套语义,故意分开:
    ``caravan_route._caravan_tile_allowed`` 判的是路面,不是地点。集市大道
    (x∈[100,104]) 穿过 south_quarter,今天走廊安全全靠遮蔽 —— 具体性优先一开,
    走廊里的新楼会把那几格翻成 public,路网当场断链。本函数永远只认 outdoor,
    不读任何 flag,因此闸的两种状态下商队路网逐字节相同。
    outdoor 只有 6 条,扫描比 28 条建筑更省。
    """
    for loc_id, loc in LOCATIONS.items():
        if loc.get("type") != "outdoor":
            continue
        b = loc.get("bounds")
        if b and len(b) == 4 and b[0] <= x <= b[2] and b[1] <= y <= b[3]:
            return loc_id
    return None
```

2) `/Volumes/data/dev/simverse-world/backend/app/services/caravan_route.py` —— `_caravan_tile_allowed`（:317-321）整段替换：

```python
def _caravan_tile_allowed(tile: Tile) -> bool:
    """路面判据:地面(outdoor 容器)+ 集市大厅的装卸道,建筑一律不通。

    刻意**不**走 get_location_id_at —— 那是「站在哪个地点」的语义,
    LOCATION_SPECIFIC_FIRST_ENABLED 会改写它,而路面不该随之变。
    """
    if outdoor_container_at(*tile) is not None:
        return True
    loc_id = get_location_id_at(*tile)
    return loc_id is None or loc_id == _MARKET_HALL_ID
```

3) 同文件 import 段（现有 `from app.agent.map_data import ...` 那行）追加 `outdoor_container_at`。

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_caravan_route_semantics.py tests/test_caravan_route.py tests/test_caravan_lifecycle.py tests/test_caravan.py tests/test_location_specificity.py tests/test_map_data.py -q
```

**验收**：4 条新测试全绿；`tests/test_caravan_route.py` / `test_caravan_lifecycle.py` / `test_caravan.py` / `test_location_specificity.py` / `test_map_data.py` 零新增失败；把一栋 public 楼落进 `bounds=(100,102,104,106)` 后，闸开与闸关两种状态下 `build_caravan_route()` 都不抛，且 `full_path` 与 `market_hall_parking` 逐字节相同（今天的静态地图上，本改动对路网输出零差异）。

**commit**：

```
fix(caravan): 路面判据改用 outdoor 容器反查——与地点具体性优先解耦,新楼落在集市大道不再断链
```

### P3-S9b — format_location_list_for_prompt 加条数上限与描述截断（无闸，今天逐字节不变） 🆕

**Flag / 批次**：无 feature flag（上限取在当前值之上 = 落地即逐字节旧行为，不需要回滚面）。依赖：必须排在 P3-S4 之后（同批下调 `civic_build.MAX_DESCRIPTION_CHARS`）、P3-S8 之后（同文件 `map_data.py`，避免与具体性索引改动打架）。

**为什么**：critic 点名的「唯一没有上限的那条链」：`map_data.format_location_list_for_prompt`(:417-445) 遍历全部非 private/apartment 地点，每行拼 `- {name}（id={slug}）：{desc}（适合：…） 入口坐标=(x,y) 约N分钟路程`，整块塞进 `PLAN_SYSTEM_PROMPT` 的 `{location_list}`(plan/basic.py:255)，**既无条数上限也无长度截断**；而 P3-S4 把 description 放宽到 200 字、公投可以无限建楼。P3-S9 给 town_facts 的 places 加了保留位，护栏加在了小的那条链上。本机实测当前值：15 行 / 915 字符、最长 description 36 字（experiment_building）——所以上限取 `LOCATION_LIST_LIMIT=24`、`LOCATION_LIST_DESC_CHARS=40` 时**今天输出逐字节不变**，这也是它敢做成无闸纯代码的理由。口径抄 town_facts：静态在前、动态给保留位。同批把 `civic_build.MAX_DESCRIPTION_CHARS` 从 200 下调到 40 与截断对齐（否则库里存 200、prompt 里砍 40，两处口径漂移）。

#### 先写的测试

新建 `/Volumes/data/dev/simverse-world/backend/tests/test_location_list_budget.py`：

```python
"""P3:计划 prompt 的地点清单也要有预算(公投可以无限建楼)。"""
import pytest

from app.agent import map_data
from app.agent.map_data import format_location_list_for_prompt as fmt


@pytest.fixture
def locations_snapshot():
    snap = {k: dict(v) for k, v in map_data.LOCATIONS.items()}
    snap_dyn = set(map_data._dynamic_slugs)
    yield map_data.LOCATIONS
    map_data.LOCATIONS.clear()
    map_data.LOCATIONS.update(snap)
    map_data._dynamic_slugs = snap_dyn


def test_todays_output_is_byte_identical():
    """实测今天 15 行 / 最长 description 36 字 —— 两个上限都取在当前值之上。"""
    text = fmt()
    assert len(text.splitlines()) == 15
    assert "学院" in text and "住宅A" not in text
    assert "…" not in text, "今天不该有任何一行被截断"


def test_static_places_survive_a_building_spree(locations_snapshot):
    for i in range(40):
        slug = f"zz{i:03d}"
        locations_snapshot[slug] = {"name": f"新楼{i:03d}", "type": "public",
                                    "description": "新建",
                                    "bounds": (2, 2, 3, 3), "entrance": (2, 2)}
        map_data._dynamic_slugs.add(slug)
    lines = fmt().splitlines()
    assert len(lines) == map_data.LOCATION_LIST_LIMIT
    assert any("实验楼" in ln for ln in lines), "静态设施不许被新楼整段顶掉"
    dyn = [ln for ln in lines if "新楼" in ln]
    assert len(dyn) == map_data.LOCATION_LIST_DYNAMIC_RESERVE
    assert "新楼039" in dyn[-1], "保留位给最新的楼(插入序末尾)"
    assert lines[0].startswith("- 学院"), "渲染顺序仍是静态在前(前缀缓存)"


def test_long_description_is_clipped(locations_snapshot):
    locations_snapshot["zz"] = {"name": "话痨楼", "type": "public",
                                "description": "啰" * 300,
                                "bounds": (2, 2, 3, 3), "entrance": (2, 2)}
    map_data._dynamic_slugs.add("zz")
    line = next(ln for ln in fmt().splitlines() if "话痨楼" in ln)
    assert "啰" * (map_data.LOCATION_LIST_DESC_CHARS + 1) not in line


def test_civic_build_cap_matches_the_prompt_clip():
    from app.services import civic_build
    assert (civic_build.MAX_DESCRIPTION_CHARS
            == map_data.LOCATION_LIST_DESC_CHARS == 40)
```

实现前：后三条红（`AttributeError: … has no attribute 'LOCATION_LIST_LIMIT'`），第一条绿（它是「本 step 不许改今天字节」的回归守卫）。

#### 实现

1) `/Volumes/data/dev/simverse-world/backend/app/agent/map_data.py` —— 在 `format_location_list_for_prompt` 之前新增：

```python
#: 计划 prompt 里地点清单的预算(P3)。本机实测今天 15 行 / 915 字符、最长
#: description 36 字 —— 两个上限都取在当前值之上,所以落地时输出逐字节不变;
#: 它们是给「公投可以无限建楼」兜底的。口径抄 town_facts 的
#: PLACES_LIMIT/PLACE_MAX_CHARS:静态在前占满,动态最多占 RESERVE 个坑。
LOCATION_LIST_LIMIT = 24
LOCATION_LIST_DYNAMIC_RESERVE = 4
LOCATION_LIST_DESC_CHARS = 40
```

2) 同函数体：把 `for loc_id, loc in LOCATIONS.items():` + `if loc["type"] in ("private", "apartment"): continue` 换成先选名额、再渲染：

```python
    items = [(lid, loc) for lid, loc in LOCATIONS.items()
             if loc["type"] not in ("private", "apartment")]
    dyn_ids = [lid for lid, _ in items
               if lid in _dynamic_slugs][-LOCATION_LIST_DYNAMIC_RESERVE:]
    static = [it for it in items if it[0] not in dyn_ids]
    static = static[:max(0, LOCATION_LIST_LIMIT - len(dyn_ids))]
    # 渲染顺序:静态在前(prompt 前缀不抖),动态排尾;名额分配才先给动态。
    for loc_id, loc in static + [it for it in items if it[0] in dyn_ids]:
        desc = loc.get("description", "") or ""
        if len(desc) > LOCATION_LIST_DESC_CHARS:
            desc = desc[:LOCATION_LIST_DESC_CHARS] + "…"
```

（其余行不动：`boosted`、`line = f"- {loc['name']}（id={loc_id}）：{desc}"`、entrance/通勤那段、`lines.append(line)`、`return "\n".join(lines)`。）

3) `/Volumes/data/dev/simverse-world/backend/app/services/civic_build.py` —— `MAX_DESCRIPTION_CHARS = 200` 改成：
```python
# 与 map_data.LOCATION_LIST_DESC_CHARS 对齐:库里存 200、prompt 里砍 40 会让
# 两处口径漂移,公投作者写的后半段永远不会被任何居民看到。
MAX_DESCRIPTION_CHARS = 40
```

#### 验证

```bash
cd /Volumes/data/dev/simverse-world/backend && .venv/bin/python -m pytest tests/test_location_list_budget.py tests/test_map_data.py tests/test_realism_movement.py tests/test_civic_build_payload.py tests/test_plan_public_memories.py -q
```

**验收**：4 条新测试全绿；`tests/test_map_data.py::test_format_location_list_for_prompt`、`tests/test_realism_movement.py::test_commute_hint_in_location_list`、`tests/test_plan_public_memories.py` 全套零新增失败（今天 15 行 < 24、最长 description 36 < 40，输出逐字节不变，冻结模板不受影响）；追加 40 栋动态楼后清单恰 24 行、含 4 行新楼且静态设施仍在、`- 学院` 仍是第一行。

**commit**：

```
feat(prompt): 计划地点清单加条数上限与描述截断——静态在前+动态保留位,今天输出逐字节不变
```

---

# 附录 A：修订后的依赖图与串并行结论

## P1 地点能声明功能（capability declaration）—— critic 意见落地补丁（第 4 轮，仅补丁条目，不重发 step 全文）

## 修订后的 P1 依赖图

**P1-S0 降级为 plan 前言**（不编号为 step、不产生 commit）；其可验证产出物由新增的 **P1-S0T** 承担。

```
P1-S0T ──> S1 ──> S2 ──> S3 ──> S4 ──> S5 ──> S6 ──> S7 ──> S9 ──> S8 ──> S10
```

**全程严格串行，零并行组。** 逐条依据：

| 边 | 依据 |
|---|---|
| S0T → S1 | S0T 冻结的 `test_action_type_enum_baseline` / 四条零 bounds 重叠是 S5/S6 等价性的引用源 |
| S1 → S2 | `normalize_capabilities` / `CAPABILITIES` / `CIVIC_GRANTABLE_CAPABILITIES` |
| S2 → S3 | `location_category` 的派生层调 `_declared_capabilities`（本轮改成带 loc_id 的两参形态） |
| S3 → S4 | 四条声明的等价对拍要在闸开/闸关两态下跑 |
| S4 → S5 | `_STATIC_LOCATION_SLUGS` + 零 bounds 重叠 |
| S5 → S6 | `capability_location_at`（actions.py 两个门 **+ 本轮新增的 decide satiety 段**） |
| S6 → S7 | `_charge_meal` 与 EAT 门必须同口径，且 S7 的守恒断言要在 S6 的不变量之上跑 |
| **S7 → S9（新）** | S9 排到 S8 之前：S8 的 verify_cmd 要纳入 tests/test_market_hall_constant.py 当场证明座位注释不破坏 S9 守卫；反序则该文件在 S8 时尚不存在 |
| **S9 → S8（新，替代原「S9 全程并行」）** | 两者同改 `app/agent/phases/decide/basic.py`（S9 改 :383-391 crowd 段、S8 改 :117-119 座位），S6 也改同文件 :314-317 satiety 段 → **S6/S9/S8 三者同文件，必须串行** |
| S8 → S10 | S10 是 P1 收口，跑全量默认门对 54 基线 |

**原计划被推翻的两处并行结论**：
- ❌「S9 完全独立可全程并行」/「与 S1-S8 无任何文件交集」——错，S9 与 S6、S8 同改 decide/basic.py。
- ❌「{S3, S5} 在 S2 之后可并行」——S5 的 `capability_location_at` 调 `location_capabilities`，而 S3 改造后的 `location_category` 参与其中；且本轮 S2 的 `_declared_capabilities` 签名变更（新增 loc_id）需要 S3 同步调用点，两者强耦合，取消并行。

## 跨部分依赖边（本轮确认/新增）

- **P2 全段 → P1-S1（硬阻塞）**：`CAP_POSTAL` / `CAP_STAGE` 必须登记且 `civic_grantable=True`。本轮补丁 1/2 已落。P2-S1 的第一条测试是这条边的守卫。
- **P3-S4 → P1-S1（硬阻塞，单向）**：P3-S4 改为 import `normalize_capabilities` + `CIVIC_GRANTABLE_CAPABILITIES`，输出 dict 形态。P1 侧不再等它——P1-S2 补丁已在 map_data 里对 `_dynamic_slugs` 行做白名单降级，安全边界不依赖任何 P3 闸的开闸顺序。
- **P2-S5 → P1-S10（文案覆盖，待办）**：P1-S10 写进 deploy 模板的「本闸是 P2 的前置」与 P2 notes 的校正冲突（P2 邮局侧不依赖本闸开闸，纯查询函数不读闸；真正的前置是 post_office 的 data_json 回填）。P2-S5 落地时必须同批改掉 P1-S10 那段注释，并同步 P3 header 的开闸顺序说明。

## 批次纪律（未变）

十一个 step 无一条迁移、无一条开闸。`LOCATION_CAPABILITIES_ENABLED` 引入即默认 False，两份 env 模板均写 false。ActionType 一个成员不加（S0T/S6 各带一条 `len(list(ActionType)) == 16` 断言）。真正的开闸属批 3，零代码 diff，与本批分车。

## P3

## 修订后的 P3 依赖图（14 → 16 step）\n\n```\n批 1（纯代码，新闸默认全关）\n  S1 ──> S2 ──> S3                 同一函数 validate_location_patch，串行\n  P1-S1 ──> S4                     【新增跨 part 硬边】S4 import\n                                   location_caps.{normalize_capabilities,\n                                   CIVIC_GRANTABLE_CAPABILITIES, CAP_DINING}\n  S3 + S4 ──> S5 ──> S6            同改 _add_dynamic_location，串行\n  S6 ──> S10 ──> S11               同改 civic_service，串行\n  S7                               lab/apply.reload_world，独立\n  S8 ──> S8b【新】                 S8b 用 S8 的 rebuild_bounds_order 语境；\n                                   S8b 必须早于 LOCATION_SPECIFIC_FIRST 翻闸\n  S9                               town_facts_service，独立\n  S4 + S8 ──> S9b【新】            同改 map_data.py（与 S8 串行）+ 同批下调\n                                   civic_build.MAX_DESCRIPTION_CHARS\n  S12                              只写开闸硬顺序（七个 KEY= 行已由 S5..S11\n                                   各自同 commit 写入两份 .env.example）\n批 2（迁移，独立部署批次）\n  S13 ──> S14                      068 迁移 → CIVIC_AGENDA 字面量（分开 commit）\n```\n\n**并行结论（修订）**：真正互不相交的只有四组 —— `{S1→S2→S3}`、`{S4}`（需 P1-S1 先落）、`{S7}`、`{S9}`。`{S8→S8b→S9b}` 三步同改 `map_data.py`，**必须串行**（原计划把 S8/S9 说成可并行，S9b 引入后不再成立）。`S5/S6/S10/S11` 仍严格串行。`S12` 依赖 S5..S11 全部落地（它的 presence 断言读的是那七行）。\n\n**新增的文件级冲突面（原计划未标）**：\n- `P3-S14` ↔ `P2-S7` 同改 `civic_service.CIVIC_AGENDA` 的 theater 条目（P2-S7 加 `capabilities={\"stage\":{}}`，P3-S14 改 bounds/center）。必须串行，建议 P2-S7 在前、P3-S14 只动那两行坐标；两者都不许碰 `topic`（`seed_civic_agenda` 的幂等键是 `Poll.question` 精确匹配）。\n- `P3-S8b` 改 `caravan_route.py`，与 P2 全段无交集，可与 P2 并行。\n\n**minor-1 批次隔离复核结论（已核实，成立）**：S13 是纯数据迁移（`alembic 068`，`_rewrite` 有 old-value 精确匹配守卫、幂等、downgrade 可逆），S14 是纯代码字面量，两者分开 commit、分开部署，不触「迁移与开闸/行为变更同批」红线；S13 同批要改的两处 `get_heads()` 断言（`tests/test_ugc_privilege_migration.py:31`、`tests/test_caravan_lifecycle_migration.py:55`）属测试适配，不改行为、不翻闸，仍在同一批次内合法。与 P2-S7 的约定一致：P2-S7 的测试**刻意不冻结** theater 的 bounds/center/entrance 数值（只判结构），所以 068 + S14 改数值不会把 P2-S7 打红——两边约定对得上，无需调整。\n\n**开闸硬顺序（修订，写进两份 .env.example）**：① `WORLD_RELOAD_RESET_PATH_CACHE` → ②（**先合入 S8b**）`LOCATION_SPECIFIC_FIRST_ENABLED`，其真实依赖方是 `decide/basic.py` 的 dining 判定、`_maybe_shelter` 的 `location_is_indoor`、`location_tracker` 首访 / `location_lore` / `/exploration/me`；**它不是 P1 能力闸的前置**（P1 两个能力门走 `capability_location_at`，不经 `get_location_id_at`），且本闸有一次性不可逆副作用 → ③ `CIVIC_BUILD_SCHEMA_ENABLED` + `CIVIC_BUILD_VALIDATE_ENABLED` → ④ `CIVIC_EFFECT_AUDIT_ENABLED` / `CIVIC_FACTS_PLACES_DYNAMIC_RESERVE` → ⑤ `CIVIC_BUILD_OPENING_EVENT_ENABLED`（须同开 `REALISM_CROWD_ENABLED`）。批 2（S13/S14）排在 ③ 稳定之后单独部署。

---

# 附录 B：未处置的 critic 意见及理由

## P1 地点能声明功能（capability declaration）—— critic 意见落地补丁（第 4 轮，仅补丁条目，不重发 step 全文）

## 未处置的 critic 意见及理由

1. **P1-S1 acceptance 的「12 个用例全 passed」数字未同步**（追加两条用例后应为 14）。纯数字描述，执行者跑绿时当场可见，不值得占一个补丁位（20 条上限）。

2. **P1-S3 rationale/test_first 未单独打补丁**（major-2「location_category 显式 category 优先 vs location_capabilities 取并集」的不对称）。判断：S3 的三级优先级本身是对的，不对称造成的伤害全部发生在**消费侧**——已由 P1-S6 补丁（decide 的 here 判定改走 capability_location_at）与 P1-S8 补丁（nearest_dining_location 闸开时委托能力反查 + 新增 test_nearest_dining_delegates_and_kills_the_priority_asymmetry）闭合。闭合位置与理由已写进 P1-S8 rationale 补丁。另：P1-S0T 的 `test_no_static_entry_carries_an_explicit_category_key` 从另一侧保证 P1 内不会出现触发该不对称的静态数据。

3. **recheck_round2 P1-S8 fix(c)「_maybe_needs_action 加连续 unreachable 熔断」未落**。需要跨 tick 计数（`ctx.movement_failed_reason` 连续 N 次）+ 持久化位置，改动面横跨 decide/execute/tick 三处并涉及新状态存储，超出「单 step 能一次做完」的粒度。可达性过滤（本轮已落）已经把最常见的成因堵住；熔断作为纵深防御列入 P1 后续 backlog，须单独成 step。

4. **recheck_round2 P1-S6 fix(c)「satiety 分支加降级出路：目的地就是当前 tile 时 return None」未落**。采纳 fix(a)（三个消费点同口径）后该场景在结构上不可达；重复加一层 guard 会掩盖将来第四个消费点再次分叉时的红，与新增的收口不变量互相削弱。

5. **P1-S7 acceptance 未单独打补丁**。其第 1 条（实现前 `['tavern_hub']` 红态）在改用守恒路径后仍然成立；守恒验收已写进 test_first 补丁（含 tests/test_meal_revenue.py 的真 sqlite 断言要求）。

6. **P1-S10 deploy 模板文案「本闸自身无前置…是 P2 的前置」未改**。plan_P2_postal notes 已明确校正为「P2 邮局侧不依赖 LOCATION_CAPABILITIES_ENABLED 开闸（location_capabilities / capability_location_at 都是不读闸的纯查询），真正的硬前置是数据侧的 post_office data_json 回填」，并把正确表述的落笔职责指定给 **P2-S5**。本轮不动 P1-S10，避免两批 env 文案互相覆盖；但已在 dep_graph_fix 里标出这条待办，P2-S5 落地时必须一并把 P1-S10 那段注释改掉。

7. **P1-S0 本体的 title/commit_msg 未打补丁**。minor-1 的落地形态由新增的 **P1-S0T** 承担（可提交的基线断言文件）；S0 本体按 critic 方案①降级为 plan 前言（不编号为 step、不产生 commit），这在 dep_graph_fix 中声明，不需要改 step 字段。

8. **全部 P3 相关条目**（P3-S4 的 schema 统一、P3-S5..S11 的 env 拆散、P3-S7 的缓存预热与 fail-open、P3-S8 的键集守卫/商队走廊/性能/不可逆写入、P3-S12 的开闸顺序、P3-S13 的 alembic head 断言）**不在本次 P1 补丁范围**。其中与 P1 强耦合的一条已按裁决单向锁定：P3-S4 必须删掉 FORBIDDEN_CAPABILITIES、改 `from app.agent.location_caps import normalize_capabilities, CIVIC_GRANTABLE_CAPABILITIES`，先归一成 dict 再按白名单过滤、输出保持 dict 形态——P1 侧的对应保障已由 P1-S2 补丁（动态行按 CIVIC_GRANTABLE 降级）落地，即使 P3-S4 迟迟不改，research 也进不了动态楼的能力集。

9. **market_hall 能力派生**：按已决取舍不做，P1-S9 维持只做常量收敛。相应地，`CAP_MARKET.civic_grantable` 保持 False（P1-S1 补丁未动它），确保公投造不出第二个 market-capable 地点。

## P3

未落成补丁条目的 critic 意见（按被舍弃原因分组）：\n\n1. **P3-S7 的「预热重建 + `_load_collision_tiles` fail-open sanity guard」**（critic_runtime）——未打补丁。理由：预热会同时打红 S7 已写好的三条断言（`_walkable_tiles_cache is None`、`_reachable_tiles_cache is None`、`build_caravan_route.cache_info().currsize == 0`），要连改 `test_first` + `implementation` 两条补丁，超出 20 条预算；本轮改为在 S7 的 env 注释里写清「调用点只有五处、不在 tick 热路径、单次 ~61ms/进程、故不加节流器」，正面回答 major-3。fail-open 中毒（tilemap 读失败 → 碰撞集合退化成空 → 居民穿墙且 P3-S6 的可达性门变永真）是 P3 之外的既有缺陷，闸开只是把「启动期一次性风险」变成「每次 reload 一次」，建议单开一条 backlog：`pathfinder._load_collision_tiles` 加「新集合为空而旧集合非空则保留旧缓存 + logger.error + 计数器」。\n\n2. **P3-S4 的 rationale 里「research 能力走 denylist 硬挡」这句**——未单列补丁（实现体与测试已改成白名单，rationale 属文档，执行时随 implementation 一并改；同理 S4 的 acceptance「10 条测试全绿」应随新测试数改成 13 条）。\n\n3. **P3-S8 的 acceptance「闸开后 build_caravan_route() 至少跑一次不抛」**——未单列补丁，已整体落进新 step P3-S8b 的 acceptance 与 verify_cmd（S8 的 verify_cmd 已补 `tests/test_caravan_route.py`）。\n\n4. **P3-S12 的 test_first「15 条 parametrize 全红」计数**——未单列补丁。正确值：7 flags × 2 files = 14 条 presence + hard_ordering 2 条 = 16 条参数化，加 1 条 `test_defaults_match_the_settings_class` 共 17 条（acceptance 里的 17 是对的）。已在 S12 的 implementation 补丁末尾写明「本 step 红点是 hard_ordering 两条，presence 那 14 条起点即绿、属 S5–S11 的回归守卫」。\n\n5. **P1 侧全部条目**（P1-S1 登记 CAP_POSTAL/CAP_STAGE 并改 `test_registry_is_a_closed_set_of_three`、P1-S6 的 decide/basic.py:315 死锁、P1-S7 的 `treasury_debit` 净销毁、P1-S8 的伪造 Edit 锚点、P1-S9 的「与 S1-S8 无文件交集可并行」错误表述与 S8/S9 断言互斥）——本轮不出条目，属 P1 补丁面。P3 侧只负责对齐：S4 已改成消费 `location_caps` 的白名单（依赖边写进 dep_graph_fix），并在 S4 里把「dining 缺 host_duty 就整项丢掉」落地，作为 P1-S7 销毁口的第二道防线。\n\n6. **P3-S8 开闸前的 `location_first_visit` 影响面盘点 SQL**——未单列 step，已写进 S8 的 env 注释与 S12 的开闸顺序段（「⚠ 一次性不可逆…开闸前先跑影响面盘点 SQL 并写进 handoff」）。
