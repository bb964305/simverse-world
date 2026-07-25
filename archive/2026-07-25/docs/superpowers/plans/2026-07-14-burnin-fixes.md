# Burn-in 修复批次 1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 vm212 burn-in 发现的 5 个问题：gossip 燃料断粮、自然互聊零次、居民就地入睡、deep forge 成本不封顶、Phaser 画布首载渲染。

**Architecture:** 后端四项都是在现有管线上的小切口：记忆提取回填 `related_resident_id`（喂 gossip）、YAML radius/加权调参 + decide 社交软提示、作息门外新增零 LLM 的"夜间归巢"规则步、forge 各 stage 复用 `llm_usage.conversation_id` 打 session 标签 + stage 间预算闸。前端一项：画布尺寸等待 + ResizeObserver 自适应。

**Tech Stack:** FastAPI + SQLAlchemy async + pytest(anyio, 内存 sqlite, fakeredis)；React + Phaser 3 + vitest(jsdom)。

## Global Constraints

- 工作分支：`fix/burnin-batch-1`，基于 `feat/rate-limiting-p1` 当前 HEAD；**不部署 vm212**（阶段 3 定版后统一部署）。
- 在独立 worktree 中执行（主工作区有并发 opencode，勿动）。
- 后端测试：`cd backend && uv run pytest tests/<file> -v`；全量回归 `uv run pytest tests/ -q`。
- 前端测试：`cd frontend && NODE_OPTIONS=--no-experimental-webstorage npm test`（本机 Node 25 必须带此环境变量，否则 jsdom localStorage 崩）；构建验证 `npm run build`。
- 玩家 avatar（`p-` 前缀）保留为特性——**本批不改** loop 的居民查询。
- 提示词模板是 `str.format` 字符串：新增 JSON 示例中的字面花括号必须写成 `{{ }}`。
- 每个 Task 一个 commit，commit message 末尾带 `Verified-by: <测试命令> <结果>`。

---

### Task 1: gossip 燃料回填 — 记忆提取写 `related_resident_id`

**Files:**
- Modify: `backend/app/services/resident_service.py`（新增批量解析 helper）
- Modify: `backend/app/memory/prompts.py:11`（extract schema）、`:73`（wrapup schema）
- Modify: `backend/app/memory/service.py:306-358`（extract_events）、`:436-462`（_persist_wrapup_side）、`:383-434`（process_chat_wrapup）
- Test: `backend/tests/test_memory_extraction.py`、`backend/tests/test_gossip.py`

**Interfaces:**
- Consumes: `MemoryService.add_memory(..., related_resident_id=...)`（已存在 keyword-only 参数）；`Memory.related_resident_id`（模型已有列 + 索引）；`gossip_service.maybe_gossip` 候选查询（type=event + importance≥0.6 + related_resident_id 非空）。
- Produces: `resident_service.resolve_resident_mentions(db, names: list[str]) -> dict[str, str]`（name 或 slug → resident.id 映射）；extract/wrapup 产出的 event Memory 带 `related_resident_id`。

- [ ] **Step 1: 写 helper 的失败测试**（加到 `tests/test_memory_extraction.py` 末尾）

```python
@pytest.mark.anyio
async def test_resolve_resident_mentions(db_session):
    from app.models.resident import Resident
    from app.services.resident_service import resolve_resident_mentions

    r1 = Resident(slug="klaus", name="克劳斯", persona_md="x")
    r2 = Resident(slug="mei", name="梅", persona_md="x")
    db_session.add_all([r1, r2])
    await db_session.commit()

    mapping = await resolve_resident_mentions(db_session, ["克劳斯", "mei", "不存在的人"])
    assert mapping["克劳斯"] == r1.id
    assert mapping["mei"] == r2.id
    assert "不存在的人" not in mapping
```

注意：Resident 构造所需最小字段以 conftest/现有测试为准（参照 `test_memory_extraction.py` L9-31 的 resident fixture，缺什么补什么）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_memory_extraction.py::test_resolve_resident_mentions -v`
Expected: FAIL `ImportError: cannot import name 'resolve_resident_mentions'`

- [ ] **Step 3: 实现 helper**（`app/services/resident_service.py`，紧跟 `get_resident_by_slug` 后）

```python
from sqlalchemy import or_, select

async def resolve_resident_mentions(db, names: list[str]) -> dict[str, str]:
    """Map mentioned names/slugs -> resident.id. Unknown names are dropped."""
    cleaned = [n.strip() for n in names if n and n.strip()]
    if not cleaned:
        return {}
    rows = (await db.execute(
        select(Resident).where(
            or_(Resident.name.in_(cleaned), Resident.slug.in_(cleaned))
        )
    )).scalars().all()
    mapping: dict[str, str] = {}
    for r in rows:
        mapping[r.name] = r.id
        mapping[r.slug] = r.id
    return {n: mapping[n] for n in cleaned if n in mapping}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_memory_extraction.py::test_resolve_resident_mentions -v`
Expected: PASS

- [ ] **Step 5: 写 extract_events 回填的失败测试**

```python
@pytest.mark.anyio
async def test_extract_events_sets_related_resident(db_session, resident):
    from app.models.resident import Resident
    third = Resident(slug="adam", name="亚当", persona_md="x")
    db_session.add(third)
    await db_session.commit()

    llm_response = json.dumps({"memories": [
        {"content": "聊到了亚当在广场发呆的事", "importance": 0.7,
         "mentioned_resident": "亚当"},
        {"content": "玩家喜欢喝咖啡", "importance": 0.4},
    ]})
    with patch("app.memory.service.llm_chat", new=AsyncMock(return_value=llm_response)):
        with patch("app.memory.service.generate_embedding", return_value=[0.1] * 1024):
            svc = MemoryService(db_session)
            memories = await svc.extract_events(
                resident=resident, other_name="Player1", conversation_text="...")

    by_content = {m.content[:6]: m for m in memories}
    assert by_content["聊到了亚当在"].related_resident_id == third.id
    assert by_content["玩家喜欢喝咖"].related_resident_id is None
```

- [ ] **Step 6: 跑测试确认失败**

Run: `uv run pytest tests/test_memory_extraction.py::test_extract_events_sets_related_resident -v`
Expected: FAIL（related_resident_id 为 None）

- [ ] **Step 7: 改 prompt schema + extract_events 实现**

`app/memory/prompts.py` L11 的 schema 行改为（保持 `{{ }}` 转义风格）：

```
{{"memories": [{{"content": "...", "importance": 0.0-1.0, "mentioned_resident": "记忆中提及的其他村民名字，没有则为 null"}}]}}
```

并在 `EXTRACT_EVENTS_SYSTEM` 正文加一行规则：`- mentioned_resident 只填村民（NPC）的名字，玩家/用户不填；没有提及其他村民时填 null`。

`app/memory/service.py` `extract_events`（L338-352 循环处）：

```python
from app.services.resident_service import resolve_resident_mentions  # 文件顶部 import

        items = (data or {}).get("memories", [])
        mentioned = [i.get("mentioned_resident") for i in items
                     if isinstance(i, dict) and i.get("mentioned_resident")]
        mention_map = await resolve_resident_mentions(self.db, mentioned) if mentioned else {}

        for item in items:
            ...
            related_id = mention_map.get((item.get("mentioned_resident") or "").strip())
            if related_id == resident.id:
                related_id = None  # 不指向自己
            mem = await self.add_memory(
                ...,  # 原有参数不变
                related_resident_id=related_id,
            )
```

（`...` 处保持原有代码不动，只加 `related_resident_id` 传参；具体行号以实际文件为准。）

- [ ] **Step 8: 跑测试确认通过**

Run: `uv run pytest tests/test_memory_extraction.py -v`
Expected: 全部 PASS（含旧测试——旧 mock JSON 没有 mentioned_resident 字段，`.get` 容忍）

- [ ] **Step 9: 写 wrapup 回填的失败测试**

wrapup 语义：event 记忆默认关于对话对象（`related_resident_id=other.id`），LLM 显式提及第三方且能解析时覆盖为第三方。加到 `tests/test_memory_extraction.py`（wrapup 测试若在其他文件则跟随该文件的 mock 样式）：

```python
@pytest.mark.anyio
async def test_wrapup_sets_related_resident_default_partner(db_session, resident):
    from app.models.resident import Resident
    partner = Resident(slug="mei", name="梅", persona_md="x")
    third = Resident(slug="adam", name="亚当", persona_md="x")
    db_session.add_all([partner, third])
    await db_session.commit()

    wrapup_json = json.dumps({
        "initiator": {"memories": [
            {"content": "梅提到亚当总在广场发呆", "importance": 0.7, "mentioned_resident": "亚当"},
            {"content": "和梅聊得很愉快", "importance": 0.5},
        ], "relationship": {"content": "对梅有好感", "importance": 0.5,
                            "metadata": {"affinity": 1, "trust": 1, "tags": []}}},
        "target": {"memories": [], "relationship": None},
        "summary": "s", "mood": "neutral",
    })
    with patch("app.memory.service.llm_chat", new=AsyncMock(return_value=wrapup_json)):
        with patch("app.memory.service.generate_embedding", return_value=[0.1] * 1024):
            svc = MemoryService(db_session)
            await svc.process_chat_wrapup(resident, partner, "对话全文")

    from sqlalchemy import select
    from app.models.memory import Memory
    rows = (await db_session.execute(select(Memory).where(
        Memory.resident_id == resident.id, Memory.type == "event"))).scalars().all()
    by_content = {m.content[:5]: m for m in rows}
    assert by_content["梅提到亚当"].related_resident_id == third.id   # 显式提及 → 第三方
    assert by_content["和梅聊得很"].related_resident_id == partner.id  # 默认 → 对话对象
```

（wrapup mock JSON 的完整结构以 `CHAT_WRAPUP_SYSTEM` L73 实际 schema 为准，relationship 段照抄现有测试的合法样例。）

- [ ] **Step 10: 跑测试确认失败**

Run: `uv run pytest tests/test_memory_extraction.py::test_wrapup_sets_related_resident_default_partner -v`
Expected: FAIL

- [ ] **Step 11: 改 wrapup schema + _persist_wrapup_side**

`app/memory/prompts.py` L73 wrapup schema 的 memories 项同样加 `"mentioned_resident"` 字段（`{{ }}` 转义），SYSTEM 正文加同款规则行。

`app/memory/service.py`：`_persist_wrapup_side` 增加一个参数 `mention_map: dict[str, str] | None = None`，event Memory 创建处（L446-449）：

```python
            raw_mention = (item.get("mentioned_resident") or "").strip()
            related_id = (mention_map or {}).get(raw_mention)
            if not related_id or related_id == resident.id:
                related_id = other.id  # 默认：记忆关于对话对象
            mem = await self.add_memory(
                ...,  # 原有参数不变
                related_resident_id=related_id,
            )
```

`process_chat_wrapup` 在调用两次 `_persist_wrapup_side` 前收集双方 memories 的 mentioned_resident 列表，一次 `resolve_resident_mentions` 得 mention_map 传入。

- [ ] **Step 12: 跑 wrapup + gossip 全量测试**

Run: `uv run pytest tests/test_memory_extraction.py tests/test_gossip.py tests/test_memory_chat_integration.py -v`
Expected: 全部 PASS

- [ ] **Step 13: 写端到端燃料测试（wrapup 产物能进 gossip 候选池）**

```python
@pytest.mark.anyio
async def test_wrapup_memory_feeds_gossip(db_session, resident):
    """wrapup 写出的高重要度记忆（related=partner）能被 maybe_gossip 选中传给第三人。"""
    from app.models.resident import Resident
    from app.services import gossip_service as gs

    partner = Resident(slug="mei", name="梅", persona_md="x")
    listener = Resident(slug="adam", name="亚当", persona_md="x")
    db_session.add_all([partner, listener])
    await db_session.commit()

    wrapup_json = json.dumps({
        "initiator": {"memories": [
            {"content": "梅答应帮全村修钟楼", "importance": 0.8}],
            "relationship": None},
        "target": {"memories": [], "relationship": None},
        "summary": "s", "mood": "neutral",
    })
    with patch("app.memory.service.llm_chat", new=AsyncMock(return_value=wrapup_json)):
        with patch("app.memory.service.generate_embedding", return_value=[0.1] * 1024):
            await MemoryService(db_session).process_chat_wrapup(resident, partner, "text")

    with patch("app.services.gossip_service.random.random", side_effect=[0.1, 0.9]):
        g = await gs.maybe_gossip(db_session, resident, listener)
    assert g is not None and g.related_resident_id == partner.id
```

- [ ] **Step 14: 跑测试确认通过**

Run: `uv run pytest tests/test_memory_extraction.py::test_wrapup_memory_feeds_gossip -v`
Expected: PASS

- [ ] **Step 15: Commit**

```bash
git add backend/app/services/resident_service.py backend/app/memory/prompts.py backend/app/memory/service.py backend/tests/test_memory_extraction.py
git commit -m "fix(memory): extract/wrapup 回填 related_resident_id——gossip 燃料断粮修复（burn-in 发现）"
```

---

### Task 2: 自然互聊 — radius 扩大 + 社交加权 + decide 社交软提示

**Files:**
- Modify: `backend/app/agent/configs/default.yaml`、`extravert.yaml`、`introvert.yaml`
- Modify: `backend/app/agent/prompts.py:50-136`（build_decision_prompt）
- Test: `backend/tests/test_agent_prompts.py`（若无此文件则新建；配置断言并入同文件）

**Interfaces:**
- Consumes: `build_decision_prompt(resident, schedule_phase, world_time, nearby_residents, memories, today_actions, available_actions, max_daily_actions, world_events=None) -> tuple[str, str]`；YAML `phases.perceive.params.radius` 与 `phases.plan.params.preferred_actions`。
- Produces: 无新接口，行为调参。

- [ ] **Step 1: 写配置断言的失败测试**

```python
# tests/test_agent_prompts.py（新文件；若仓库已有同名文件则追加）
import pytest
from app.agent.registry import registry


def _perceive_radius(config_name: str) -> int:
    cfg = registry.load_config(config_name)  # 以 registry 实际读取 API 为准：
    # 若无 load_config，直接 yaml.safe_load(open(f"app/agent/configs/{config_name}.yaml"))
    return cfg["phases"]["perceive"]["params"]["radius"]


def test_social_radius_expanded():
    assert _perceive_radius("default") == 18
    assert _perceive_radius("extravert") == 24
    assert _perceive_radius("introvert") == 10
```

（registry 读配置的函数名以 `app/agent/registry.py` 实际为准——实现者先读该文件再定 `_perceive_radius` 写法，直接 yaml.safe_load 亦可。）

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_agent_prompts.py::test_social_radius_expanded -v`
Expected: FAIL（现值 10/14/6）

- [ ] **Step 3: 改三个 YAML**

- `default.yaml`：`phases.perceive.params.radius: 10 → 18`；`phases.plan.params.preferred_actions` 若无此段则新增：
  ```yaml
      preferred_actions:
        - CHAT_RESIDENT:2
        - VISIT_DISTRICT:2
        - WORK:2
        - WANDER:1
  ```
  （若已有该段，把 CHAT_RESIDENT 与 VISIT_DISTRICT 权重设为 2，其余保持。）
- `extravert.yaml`：`radius: 14 → 24`（preferred_actions 已有 CHAT_RESIDENT:3/VISIT_DISTRICT:2，不动）
- `introvert.yaml`：`radius: 6 → 10`（preferred_actions 保持内向倾向，不动）

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_agent_prompts.py::test_social_radius_expanded -v`
Expected: PASS

- [ ] **Step 5: 写 decide 社交软提示的失败测试**

```python
def _mk_resident():
    from unittest.mock import MagicMock
    r = MagicMock()
    r.name = "克劳斯"; r.persona_md = "p"; r.tile_x = 1; r.tile_y = 1
    r.mood_json = None
    return r


def test_decision_prompt_social_hint_when_nearby():
    from app.agent.actions import ActionType
    from app.agent.prompts import build_decision_prompt
    from unittest.mock import MagicMock

    nearby = MagicMock(); nearby.name = "梅"; nearby.slug = "mei"; nearby.status = "idle"
    system, user = build_decision_prompt(
        resident=_mk_resident(), schedule_phase="afternoon", world_time="14:00",
        nearby_residents=[nearby], memories=[], today_actions=[],
        available_actions=[ActionType.CHAT_RESIDENT, ActionType.IDLE],
        max_daily_actions=20,
    )
    assert "主动搭话" in system + user


def test_decision_prompt_no_social_hint_when_alone():
    from app.agent.actions import ActionType
    from app.agent.prompts import build_decision_prompt

    system, user = build_decision_prompt(
        resident=_mk_resident(), schedule_phase="afternoon", world_time="14:00",
        nearby_residents=[], memories=[], today_actions=[],
        available_actions=[ActionType.IDLE], max_daily_actions=20,
    )
    assert "主动搭话" not in system + user
```

（`build_decision_prompt` 参数构造以实际签名为准，MagicMock 补齐模板要用的属性；跑失败时按 AttributeError 补属性而不是改断言。）

- [ ] **Step 6: 跑测试确认失败**

Run: `uv run pytest tests/test_agent_prompts.py -v -k social_hint`
Expected: FAIL（无"主动搭话"文案）

- [ ] **Step 7: 在 build_decision_prompt 注入社交软提示**

`app/agent/prompts.py`，天气软提示段（L114-126）之后、心情段之前加：

```python
    # 社交软提示（burn-in 发现：自然互聊为零；有邻居时轻推一把，不强制）
    if nearby_residents and ActionType.CHAT_RESIDENT in available_actions:
        names = "、".join(r.name for r in nearby_residents[:3])
        user_prompt += (
            f"\n附近有可以交谈的居民：{names}。"
            "如果当前没有更重要的事，主动搭话（CHAT_RESIDENT）能带来新鲜事和关系进展。"
        )
```

（`user_prompt` 变量名与追加方式对齐该函数中天气段的现有写法；`ActionType` 该文件若未 import 则补。）

- [ ] **Step 8: 跑测试确认通过 + 老测试回归**

Run: `uv run pytest tests/test_agent_prompts.py tests/test_resident_tick.py tests/test_agent_loop.py -v`
Expected: 全部 PASS

- [ ] **Step 9: Commit**

```bash
git add backend/app/agent/configs/ backend/app/agent/prompts.py backend/tests/test_agent_prompts.py
git commit -m "feat(agent): 社交半径扩大(10/14/6→18/24/10)+plan 社交加权+decide 邻居软提示——自然互聊零次修复（burn-in 发现）"
```

---

### Task 3: 夜间归巢 — 作息门外的零 LLM GO_HOME 规则步

**Files:**
- Create: `backend/app/agent/night_homing.py`
- Modify: `backend/app/agent/loop.py:112-149`（guarded_tick 增加夜间分支）
- Test: `backend/tests/test_night_homing.py`（新建）

**Interfaces:**
- Consumes: `get_valid_target_tile(loc_id)`（app/agent/map_data.py:218-223）、`find_path(start, target, walkable)` + `get_walkable_tiles()`（app/agent/pathfinder.py / map_data.py，以 execute/basic.py:39-60 的用法为准）、`get_activity_probability(schedule, hour)`（scheduler.py:110）、`manager.broadcast`（resident_move 帧，loop.py:186-192 同款）。
- Produces: `night_homing_step(db, resident) -> tuple[int, int] | None`（走一步返回新 tile；已到家/无家/无路返回 None）。

- [ ] **Step 1: 写 night_homing_step 的失败测试**

```python
# tests/test_night_homing.py
import pytest
from unittest.mock import MagicMock, patch


@pytest.mark.anyio
async def test_homing_step_moves_one_tile(db_session):
    from app.models.resident import Resident
    from app.agent.night_homing import night_homing_step

    r = Resident(slug="klaus", name="克劳斯", persona_md="x",
                 tile_x=10, tile_y=10, home_location_id="home-klaus", status="idle")
    db_session.add(r); await db_session.commit()

    with patch("app.agent.night_homing.get_valid_target_tile", return_value=(14, 10)), \
         patch("app.agent.night_homing.get_walkable_tiles", return_value=set()), \
         patch("app.agent.night_homing.find_path",
               return_value=[(10, 10), (11, 10), (12, 10)]):
        new_tile = await night_homing_step(db_session, r)

    assert new_tile == (11, 10)
    assert (r.tile_x, r.tile_y) == (11, 10)
    assert r.status == "walking"


@pytest.mark.anyio
async def test_homing_step_arrived_returns_none(db_session):
    from app.models.resident import Resident
    from app.agent.night_homing import night_homing_step

    r = Resident(slug="mei", name="梅", persona_md="x",
                 tile_x=14, tile_y=10, home_location_id="home-mei", status="walking")
    db_session.add(r); await db_session.commit()

    with patch("app.agent.night_homing.get_valid_target_tile", return_value=(14, 10)):
        assert await night_homing_step(db_session, r) is None
    assert r.status == "idle"   # 到家收尾


@pytest.mark.anyio
async def test_homing_step_no_home_returns_none(db_session):
    from app.models.resident import Resident
    from app.agent.night_homing import night_homing_step

    r = Resident(slug="adam", name="亚当", persona_md="x",
                 tile_x=5, tile_y=5, home_location_id=None, status="idle")
    db_session.add(r); await db_session.commit()
    assert await night_homing_step(db_session, r) is None
```

（Resident 最小字段/home_tile_x 回退列名以 `app/models/resident.py` 实际为准。）

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_night_homing.py -v`
Expected: FAIL `ModuleNotFoundError: app.agent.night_homing`

- [ ] **Step 3: 实现 night_homing.py**

```python
"""Night homing: 作息门关闭后（活动概率 0），居民规则化走回家——零 LLM。

burn-in 发现：sleep_hour 后 should_tick 恒 False，居民冻结在最后位置"就地入睡"。
本模块每 tick 让不在家的居民朝家走一步，与 BasicExecutePlugin 的移动语义一致。
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.map_data import get_valid_target_tile, get_walkable_tiles
from app.agent.pathfinder import find_path
from app.models.resident import Resident

logger = logging.getLogger(__name__)


def _home_target(resident: Resident) -> tuple[int, int] | None:
    if getattr(resident, "home_location_id", None):
        t = get_valid_target_tile(resident.home_location_id)
        if t:
            return (t[0], t[1])
    if resident.home_tile_x is not None and resident.home_tile_y is not None:
        return (resident.home_tile_x, resident.home_tile_y)
    return None


async def night_homing_step(db: AsyncSession, resident: Resident) -> tuple[int, int] | None:
    """Move one tile toward home. Returns the new tile, or None when settled."""
    target = _home_target(resident)
    if target is None:
        return None
    if (resident.tile_x, resident.tile_y) == target:
        if resident.status == "walking":
            resident.status = "idle"
            await db.commit()
        return None
    path = find_path((resident.tile_x, resident.tile_y), target, get_walkable_tiles())
    if not path or len(path) < 2:
        if resident.status == "walking":
            resident.status = "idle"
            await db.commit()
        return None
    nxt = path[1]
    resident.tile_x, resident.tile_y = nxt[0], nxt[1]
    resident.status = "walking"
    await db.commit()
    return (nxt[0], nxt[1])
```

（`find_path`/`get_walkable_tiles`/`get_valid_target_tile` 的实际 import 路径按 execute/basic.py 顶部的 import 照抄；若 walkable 参数形态不同，以 execute/basic.py:39-60 用法为准。）

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_night_homing.py -v`
Expected: PASS

- [ ] **Step 5: 写 loop 夜间分支的失败测试**（加到 `tests/test_agent_loop.py`）

```python
@pytest.mark.anyio
async def test_tick_round_night_runs_homing_not_llm(loop_session_factory, residents):
    """活动概率 0（夜间）时：不调 resident_tick，改跑 night_homing_step。"""
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.agent.loop import AgentLoop

    homing = AsyncMock(return_value=(1, 1))
    tick = AsyncMock()
    with patch("app.agent.loop.async_session", loop_session_factory), \
         patch("app.agent.loop.build_schedule", return_value=MagicMock(
             wake_hour=8, sleep_hour=22, peak_hours=[10], social_slots=[], rest_ratio=0.3)), \
         patch("app.agent.loop.get_activity_probability", return_value=0.0), \
         patch("app.agent.loop.night_homing_step", homing), \
         patch("app.agent.loop.resident_tick", tick), \
         patch("app.agent.loop.manager") as mock_manager:
        mock_manager.broadcast = AsyncMock()
        await AgentLoop()._tick_round()

    assert tick.await_count == 0            # 零 LLM tick
    assert homing.await_count >= 1          # 归巢步跑了
    assert mock_manager.broadcast.await_count >= 1   # resident_move 帧广播
    frame = mock_manager.broadcast.await_args_list[0].args[0]
    assert frame["type"] == "resident_move" and frame["status"] == "walking"
```

（fixture 名 `loop_session_factory`/`residents` 按 test_agent_loop.py 现有 fixture；background_tier 若需 patch 成 NORMAL 照现有测试写法补。）

- [ ] **Step 6: 跑测试确认失败**

Run: `uv run pytest tests/test_agent_loop.py::test_tick_round_night_runs_homing_not_llm -v`
Expected: FAIL（loop 没有 get_activity_probability/night_homing_step 可 patch）

- [ ] **Step 7: 改 loop.py guarded_tick**

`app/agent/loop.py`：顶部 import 补 `from app.agent.scheduler import build_schedule, should_tick, get_activity_probability` 与 `from app.agent.night_homing import night_homing_step`。`guarded_tick`（L115-124 一带）把

```python
            if not should_tick(schedule, current_hour):
                return None
```

改为：

```python
            if get_activity_probability(schedule, current_hour) <= 0.0:
                # 作息门关闭：夜间归巢（零 LLM，一 tick 一步），不计日行动数
                async with semaphore:
                    async with async_session() as db:
                        resident = await db.get(Resident, resident_id)
                        if resident is None or resident.status in (
                                "sleeping", "chatting", "socializing"):
                            return None
                        new_tile = await night_homing_step(db, resident)
                        if new_tile is not None:
                            await manager.broadcast({
                                "type": "resident_move",
                                "resident_slug": resident.slug,
                                "tile_x": resident.tile_x,
                                "tile_y": resident.tile_y,
                                "target_tile": None,
                                "status": "walking",
                            })
                return None
            if not should_tick(schedule, current_hour):
                return None
```

（帧字段与 loop.py:186-192 现有广播完全同构；semaphore/async_session 变量名以该函数现状为准。）

- [ ] **Step 8: 跑测试确认通过 + loop 回归**

Run: `uv run pytest tests/test_agent_loop.py tests/test_night_homing.py -v`
Expected: 全部 PASS

- [ ] **Step 9: Commit**

```bash
git add backend/app/agent/night_homing.py backend/app/agent/loop.py backend/tests/test_night_homing.py backend/tests/test_agent_loop.py
git commit -m "feat(agent): 夜间归巢——作息门关闭后零 LLM 规则化走回家，修就地入睡（burn-in 发现，Jimmy 目视报告）"
```

---

### Task 4: deep forge 成本封顶 — session 标签 + stage 间预算闸

**Files:**
- Modify: `backend/app/forge/extraction_stage.py`、`build_stage.py`、`validation_stage.py`、`refinement_stage.py`（record_usage 加 conversation_id）
- Modify: `backend/app/forge/pipeline.py:71-99`（run_to_completion）、`:160-242`（_run_deep）、`_run_quick`（stage 间闸）
- Test: `backend/tests/test_forge_pipeline.py`

**Interfaces:**
- Consumes: `record_usage(scenario, *, model, owner, response=None, ..., conversation_id: str | None = None)`（metering.py:100-114，槽位已存在零迁移）；`settings.budget_forge_request_usd`（config.py:70，默认 0.15）；`LlmUsage.cost_usd`。
- Produces: 各 stage 构造函数新参 `session_id: str | None = None`；`ForgePipeline._over_request_budget(session_id) -> bool`。

- [ ] **Step 1: 写 stage 传标签的失败测试**（加到 test_forge_pipeline.py）

```python
@pytest.mark.anyio
async def test_build_stage_tags_session_id(db_session):
    from unittest.mock import AsyncMock, patch
    from app.forge.build_stage import BuildStage

    mock_client = AsyncMock()
    resp = AsyncMock(); resp.content = [AsyncMock(text='{"ok": true}')]
    mock_client.messages.create = AsyncMock(return_value=resp)

    with patch("app.forge.build_stage.record_usage", new_callable=AsyncMock) as ru:
        stage = BuildStage(llm_client=mock_client, model="test-model", session_id="sess-1")
        await stage.run(...)   # run 参数照该文件现有 BuildStage 测试样例照抄

    for call in ru.await_args_list:
        assert call.kwargs.get("conversation_id") == "sess-1"
```

（`stage.run(...)` 的参数与现有 `test_forge_pipeline.py` L103-140 的 BuildStage 测试完全一致地照抄；`record_usage` patch 的命名空间是 stage 模块自身。）

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_forge_pipeline.py::test_build_stage_tags_session_id -v`
Expected: FAIL（`__init__` 不接受 session_id）

- [ ] **Step 3: 四个 stage 加 session_id 并透传**

对 `extraction_stage.py`/`build_stage.py`/`validation_stage.py`/`refinement_stage.py` 各自：

```python
    def __init__(self, llm_client, model, ..., session_id: str | None = None):
        ...
        self._session_id = session_id
```

每个 `record_usage("forge_xxx", model=..., owner=..., response=...)` 调用追加 `conversation_id=self._session_id`。（router_stage 在 session 创建前运行，不改。）

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_forge_pipeline.py -v`
Expected: 全部 PASS（旧测试不传 session_id，默认 None 兼容）

- [ ] **Step 5: 写预算闸的失败测试**

```python
@pytest.mark.anyio
async def test_pipeline_aborts_when_over_request_budget(db_session, monkeypatch):
    from unittest.mock import AsyncMock
    from app.forge.pipeline import ForgePipeline
    from app.models.llm_usage import LlmUsage
    from app.config import settings

    monkeypatch.setattr(settings, "budget_forge_request_usd", 0.10)

    mock_client = AsyncMock()
    pipeline = ForgePipeline(db=db_session, system_client=mock_client,
                             user_client=mock_client, model="test")
    # 建 session（走 quick 路由 mock，照 test_forge_pipeline.py L200-236 全管线样例）
    ...
    # 预先塞两行超预算的用量（模拟前面 stage 已烧 $0.12）
    db_session.add(LlmUsage(scenario="forge_build", model="test", owner="user",
                            conversation_id=session.id, cost_usd=0.12))
    await db_session.commit()

    result = await pipeline.run_to_completion(session.id)
    assert result.status == "error"
    assert "budget" in (result.error or "").lower()
```

（session 建法与 monkeypatch stage 的方式照抄该文件 L200-236 的 run_to_completion 样例；`result.error` 字段名以 ForgeSession 模型实际列为准——若无 error 列则只断言 status=="error"。）

- [ ] **Step 6: 跑测试确认失败**

Run: `uv run pytest tests/test_forge_pipeline.py::test_pipeline_aborts_when_over_request_budget -v`
Expected: FAIL（不会中止，status=="done"）

- [ ] **Step 7: 实现 stage 间预算闸**

`app/forge/pipeline.py`：

```python
from sqlalchemy import func, select
from app.config import settings
from app.models.llm_usage import LlmUsage


class ForgeBudgetExceeded(RuntimeError):
    pass


# ForgePipeline 内新增：
    async def _check_request_budget(self, session_id: str) -> None:
        cap = settings.budget_forge_request_usd
        if cap <= 0:
            return
        spent = (await self.db.execute(
            select(func.coalesce(func.sum(LlmUsage.cost_usd), 0.0))
            .where(LlmUsage.conversation_id == session_id)
        )).scalar_one()
        if spent > cap:
            raise ForgeBudgetExceeded(
                f"forge request budget exceeded: ${spent:.4f} > ${cap}")
```

`_run_deep` 每个 stage 之间（更新 status 之前）与 `_run_quick` 各 LLM 步之间插 `await self._check_request_budget(session.id)`；stage 构造处传 `session_id=session.id`。`run_to_completion` 的异常处理里 `ForgeBudgetExceeded` 走既有 `status="error"` 路径（若 session 有 error/message 列则写入原因）。

- [ ] **Step 8: 跑 forge 全量回归**

Run: `uv run pytest tests/test_forge_pipeline.py tests/test_forge.py -v`
Expected: 全部 PASS

- [ ] **Step 9: Commit**

```bash
git add backend/app/forge/ backend/tests/test_forge_pipeline.py
git commit -m "fix(forge): stage 计量打 session 标签(复用 conversation_id)+stage 间预算闸——单次 deep forge 实测 \$0.30 超 0.15 上限（burn-in 发现）"
```

---

### Task 5: Phaser 画布首载渲染 — 尺寸等待 + 容器自适应

**Files:**
- Create: `frontend/src/game/canvasSize.ts`
- Modify: `frontend/src/game/GameScene.ts:51-71`（initGame/destroyGame）
- Test: `frontend/src/game/canvasSize.test.ts`（新建）

**Interfaces:**
- Consumes: `Phaser.Game#scale.resize(width, height)`；`GamePage.tsx:26-37` 现有调用方式 `initGame(containerRef.current)`（调用点不改——initGame 变 async 后返回 Promise，fire-and-forget 兼容）。
- Produces: `waitForNonZeroSize(el, timeoutMs=2000) -> Promise<{width, height}>`；`observeContainerResize(el, cb) -> () => void`。

- [ ] **Step 1: 写 canvasSize 的失败测试**

```typescript
// frontend/src/game/canvasSize.test.ts
import { describe, expect, it, vi } from 'vitest'
import { waitForNonZeroSize } from './canvasSize'

function fakeEl(sizes: Array<[number, number]>): HTMLElement {
  const el = document.createElement('div')
  let i = 0
  Object.defineProperty(el, 'clientWidth', { get: () => sizes[Math.min(i, sizes.length - 1)][0] })
  Object.defineProperty(el, 'clientHeight', {
    get: () => {
      const h = sizes[Math.min(i, sizes.length - 1)][1]
      i += 1 // 每轮读取后推进一帧
      return h
    },
  })
  return el
}

describe('waitForNonZeroSize', () => {
  it('waits until the container has a real size', async () => {
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      setTimeout(() => cb(0), 0)
      return 1
    })
    const el = fakeEl([[0, 0], [0, 0], [1280, 640]])
    const size = await waitForNonZeroSize(el)
    expect(size).toEqual({ width: 1280, height: 640 })
    vi.unstubAllGlobals()
  })

  it('falls back to 1x1 floor on timeout', async () => {
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      setTimeout(() => cb(0), 0)
      return 1
    })
    const el = fakeEl([[0, 0]])
    const size = await waitForNonZeroSize(el, 50)
    expect(size.width).toBeGreaterThanOrEqual(1)
    expect(size.height).toBeGreaterThanOrEqual(1)
    vi.unstubAllGlobals()
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && NODE_OPTIONS=--no-experimental-webstorage npx vitest run src/game/canvasSize.test.ts`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 canvasSize.ts**

```typescript
/** Canvas sizing helpers — burn-in fix: first-load canvas rendered 150x90 at top-left
 * because Phaser read container size once, before layout settled (scale mode NONE). */

export function waitForNonZeroSize(
  el: HTMLElement,
  timeoutMs = 2000,
): Promise<{ width: number; height: number }> {
  return new Promise((resolve) => {
    const started = Date.now()
    const check = () => {
      const width = el.clientWidth
      const height = el.clientHeight
      if (width > 0 && height > 0) {
        resolve({ width, height })
        return
      }
      if (Date.now() - started >= timeoutMs) {
        resolve({ width: Math.max(1, width), height: Math.max(1, height) })
        return
      }
      requestAnimationFrame(check)
    }
    check()
  })
}

export function observeContainerResize(
  el: HTMLElement,
  cb: (width: number, height: number) => void,
): () => void {
  if (typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver(() => cb(el.clientWidth, el.clientHeight))
    ro.observe(el)
    return () => ro.disconnect()
  }
  const onResize = () => cb(el.clientWidth, el.clientHeight)
  window.addEventListener('resize', onResize)
  return () => window.removeEventListener('resize', onResize)
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `NODE_OPTIONS=--no-experimental-webstorage npx vitest run src/game/canvasSize.test.ts`
Expected: PASS

- [ ] **Step 5: 接入 GameScene.initGame/destroyGame**

`frontend/src/game/GameScene.ts:51-71` 改为：

```typescript
import { observeContainerResize, waitForNonZeroSize } from './canvasSize'

let gameInstance: Phaser.Game | null = null
let stopResizeObserver: (() => void) | null = null
let initGeneration = 0

export function destroyGame(): void {
  initGeneration += 1
  if (stopResizeObserver) {
    stopResizeObserver()
    stopResizeObserver = null
  }
  if (gameInstance) {
    gameInstance.destroy(true)
    gameInstance = null
  }
}

export async function initGame(container: HTMLElement): Promise<void> {
  if (gameInstance) return
  const generation = ++initGeneration
  const { width, height } = await waitForNonZeroSize(container)
  if (generation !== initGeneration || gameInstance) return // unmount 竞态防护
  const zoom = Math.max(1, window.innerWidth / 4400)
  gameInstance = new Phaser.Game({
    type: Phaser.AUTO,
    width: width / zoom,
    height: height / zoom,
    parent: container,
    pixelArt: true,
    physics: { default: 'arcade', arcade: { gravity: { x: 0, y: 0 } } },
    scene: [MainScene],
    scale: { zoom },
  })
  stopResizeObserver = observeContainerResize(container, (w, h) => {
    if (gameInstance && w > 0 && h > 0) {
      gameInstance.scale.resize(w / zoom, h / zoom)
    }
  })
}
```

（原 config 其余字段保持逐字不动；GamePage 调用点 `initGame(containerRef.current)` 不需要改——async 化后 fire-and-forget 兼容，unmount 竞态由 generation 防护。）

- [ ] **Step 6: 跑前端全量测试 + 构建**

Run: `NODE_OPTIONS=--no-experimental-webstorage npm test && npm run build`
Expected: 测试全 PASS，build 成功（Phaser 侧改动 jsdom 测不了，靠 build + 部署后真机验证——记入部署清单）

- [ ] **Step 7: Commit**

```bash
git add frontend/src/game/canvasSize.ts frontend/src/game/canvasSize.test.ts frontend/src/game/GameScene.ts
git commit -m "fix(frontend): Phaser 画布首载等待容器就位+ResizeObserver 自适应——修首登只渲染左上角（burn-in 发现）；顺带修 chatOpen 380px 推移不重排"
```

---

## 收尾（全部 Task 完成后）

- [ ] 全量回归：`cd backend && uv run pytest tests/ -q` + `cd frontend && NODE_OPTIONS=--no-experimental-webstorage npm test && npm run build`
- [ ] 分支停在 `fix/burnin-batch-1` **不合并、不部署**——阶段 3 定版后走部署 + 真机验证（Phaser 修复必须真机过一遍 verify-before-done）
- [ ] PROGRESS.md 记账修复批次（在主工作区提交，避免 worktree 冲突）

## Self-Review 记录

- Spec coverage：5 个问题各对应 Task 1-5 ✅；玩家 avatar 保留决策 = 无代码任务 ✅
- Placeholder scan：Step 内引用"以实际文件为准"处均为签名核对型指引（fixture 名/参数照抄现有测试），非逻辑留白 ✅
- Type consistency：`resolve_resident_mentions(db, list[str]) -> dict[str, str]` Task 1 内一致；`night_homing_step(db, resident) -> tuple|None` Task 3 内一致；`session_id: str | None` Task 4 各 stage 一致 ✅
