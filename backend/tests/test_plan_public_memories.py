"""P2 —— 计划阶段的 prompt 里看得见镇上的事(``REALISM_PLAN_PUBLIC_MEMORIES``)。

**缺陷**(2026-08-10 生产只读实测,``app/agent/phases/plan/basic.py``)::

    recent_all = await memory_svc.get_memories(resident.id, type="event", limit=20)
    recent = [m for m in recent_all if m.importance > 0.5][:5]

``get_memories`` 是 ``ORDER BY created_at DESC LIMIT 20``。每人每天写 480-545 条
event 记忆 —— 那 20 条只覆盖 **20-30 分钟**:

    居民            那 20 条覆盖的时间   ``>0.5`` 后剩   其中 ``world_event``
    gu-mingyuan     20 分钟              18              **0**
    zhao-qiwen      22 分钟              8               **0**
    he-qiaoyun      30 分钟              7               **0**

实质世界事件约 1.6 条/人/周、镇务结果更稀疏 —— 落进这个窗口的概率约等于 0,
六位居民的 ``world_event`` 计数**全是 0**。所以「NPC 计划自己一天时不知道镇上出了
什么事」的根因是**时间窗口**,不是阈值:

- ``importance > 0.5`` **不动**。20 条里 7-18 条都过得了,它筛掉的是天气(恒 0.5)
  与低价值 ``agent_action`` —— 做的正是该做的事;改成 ``>= 0.5`` 会把天气放进来;
- ``limit=20`` **不动**。那 20 分钟窗口对「个人近况」是**对的**口径,要覆盖一周得
  读 ~3500 条。两件事口径不同,不该用同一个 limit 表达。

**做法**:复用第 2/3 段那两条道的**取数判据**(``_query_recent_civic_results`` /
``_query_recent_substantive_world_events``,与候选池共用同一份 SQL),各取最近的,
与「个人近况」并列渲染成 prompt 里新的一段。计划阶段**不走** ``retrieve_context``
—— 它没有 query text,要的是「最近发生了什么」而不是「与某句话相关的是什么」。

五条不变量,逐条对应下面的用例:

1. 闸关(=0)时 plan prompt **逐字节**与改前一致;
2. 公共记忆与已渲染的个人记忆**不重复**(在最近 20 条里的那条只出现一次);
3. 天气**永不出现**(两条道的判据本来就排除它,但要有断言);
4. 取不到公共记忆时 ``recent`` 的既有 fallback(``if not recent: recent_all[:3]``)
   不变;
5. 一条公共记忆都没有时 prompt 里**不出现空标题**。

不变量 1 的逐字节对拍照第 2/3 段的**双轨**范式写:

- **轨 1(常驻)**:改前那份 ``PLAN_USER_PROMPT`` 冻结在本文件里,用它跑一遍真
  ``BasicPlanPlugin``,与新模板跑出来的 prompt 逐字节对。纯字符串 + 真插件,
  **不碰 git、与克隆深度无关** —— CI 的 ``actions/checkout@v4`` 默认
  ``fetch-depth: 1``,任何依赖历史 ref 的对拍在那里都取不到对象;
- **轨 2(加强,拿得到才跑)**:从 ``69f07a7``(本分支起点)那份 ``basic.py`` 里
  ``ast`` 取出真实的模板字面量,钉住轨 1 没抄漂。ref 用**固定 SHA** 而不是会随合并
  漂移的 ``master``;浅克隆里取不到就 skip。
"""
import ast
import functools
import subprocess
from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.actions import ActionType
from app.agent.phases.plan import basic
from app.agent.schemas import TickContext
from app.config import settings
from app.memory.service import MemoryService
from app.models.memory import Memory
from app.models.resident import Resident
from app.services import world_event_service as wes

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ── 轨 1:改前那份 PLAN_USER_PROMPT 的冻结快照(常驻,不依赖 git 历史)──────

#: ``69f07a7:backend/app/agent/phases/plan/basic.py`` 里 ``PLAN_USER_PROMPT`` 的
#: **逐字冻结**。
#:
#: 为什么要冻结:CI 的浅克隆里既没有 ``master`` 这个 ref、也没有 ``69f07a7`` 这个
#: 对象 —— 任何 ``git show`` 对拍在那里要么整片红,要么只能 skip 成假绿。这份快照
#: 是纯字符串,**与克隆深度无关,永远会跑**,是「闸关=逐字节旧行为」这句承诺唯一的
#: 常驻保险。它有没有抄漂由轨 2 在有 git 的环境里钉住。
_FROZEN_PLAN_USER_PROMPT = (
    "昨天做了什么：\n{yesterday_summary}\n\n最近的重要记忆：\n{recent_memories}\n\n"
    "最近的关系：\n{relationships}\n\n请生成今天的目标和 {slot_count} 个时段的计划。\n"
)


# ── 轨 2:钉死 SHA 的 git 对拍(拿得到就跑,拿不到就 skip)────────────────

#: 本分支的起点 = **计划阶段接线之前**的那棵树。钉 SHA 不钉 ``master``:``master``
#: 会随本批合入而漂,漂完之后「对拍改前」对的就是它自己(第 2 段踩过一次)。
_PRE_PLAN_SHA = "69f07a7e76b1ea98f46ef13c914833b0f047f3d7"

_TRACK1_IS_THE_REAL_GUARD = (
    "轨 2(git 对拍 {sha})只是**加强**:{why}。真正的保险是轨 1 —— 本文件里的 "
    "``_FROZEN_PLAN_USER_PROMPT``,它不碰 git、与克隆深度无关、每一次都真的跑,"
    "拿真 BasicPlanPlugin 逐字节对同一批形状。所以这里 skip 不留缺口。")


@functools.lru_cache(maxsize=1)
def _pinned_plan_source() -> str | None:
    """取 ``_PRE_PLAN_SHA`` 那份 ``plan/basic.py``;取不到返回 ``None``。

    取不到的正当情形:CI 的 ``fetch-depth: 1`` 浅克隆、导出成 tarball 的源码树、
    没有 git 的机器。这些都不该让整片测试红。
    """
    try:
        proc = subprocess.run(
            ["git", "show", f"{_PRE_PLAN_SHA}:backend/app/agent/phases/plan/basic.py"],
            cwd=_REPO_ROOT, capture_output=True, text=True)
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _template_literal_of(src: str) -> str:
    """从一份 ``basic.py`` 源码里 ``ast`` 取出 ``PLAN_USER_PROMPT`` 的字面量。

    用 ``ast`` 而不是正则:模板本身就是一段带 ``{}`` 与换行的中文,正则切它极易
    在「差一个换行」这种正好是本对拍要抓的差异上翻车。
    """
    for node in ast.parse(src).body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "PLAN_USER_PROMPT" for t in node.targets)
                and isinstance(node.value, ast.Constant)):
            return node.value.value
    raise AssertionError("这份 basic.py 里找不到 PLAN_USER_PROMPT 的字面量赋值")


# ── 播种 ────────────────────────────────────────────────────────────────

#: 镇务结果档在饱和史下的归一落点(与 test_pool_reserved_slots 同名常量一致)。
_CIVIC_NORMALIZED = 0.99

#: 实质世界事件的落库 importance。**刻意与天气同档** —— 设计反转之后实质事件不再
#: 抬 importance(见 ``_fetch_reserved_world_event_candidates`` 的注释)。它低于
#: ``importance > 0.5`` 那条个人筛,所以它进 prompt **只**可能因为公共记忆这一段。
_WE_IMPORTANCE = 0.5

#: 个人近况的 importance。高于 0.5 → 过得了既有那条筛,``recent`` 不为空。
_PERSONAL_IMPORTANCE = 0.9

_SBTI = {"type": "GOGO", "type_name": "行者", "dimensions": {
    "S1": "H", "S2": "H", "S3": "M", "E1": "H", "E2": "M", "E3": "H",
    "A1": "M", "A2": "M", "A3": "H", "Ac1": "H", "Ac2": "H", "Ac3": "H",
    "So1": "M", "So2": "H", "So3": "M"}}

_LLM_JSON = (
    '{"goal": {"goal": "学习新技能", "motivation": "好奇心驱使"}, "plans": ['
    '{"slot": 0, "hour_range": [7, 9], "action": "IDLE", "target": null,'
    ' "location": "home", "importance": 2, "reason": "起床"}]}')


async def _resident(db, slug: str = "gu-mingyuan") -> Resident:
    r = Resident(slug=slug, name=slug, resident_type="npc",
                 persona_md="一个好奇的学习者。", meta_json={"sbti": _SBTI})
    db.add(r)
    await db.commit()
    return r


async def _seed_personal(db, rid: str, n: int, *,
                         importance: float = _PERSONAL_IMPORTANCE) -> list[Memory]:
    """个人近况。下标 0 最新,每条相隔一分钟 —— 全都落在**今天**(世界日),
    免得它们从 ``yesterday_summary`` 那条路进 prompt,把断言的因果搅乱。"""
    now = datetime.now(UTC)
    out = []
    for i in range(n):
        mem = Memory(resident_id=rid, type="event", content=f"个人琐事{i}",
                     importance=importance, source="agent_action",
                     metadata_json={"raw_importance": importance})
        mem.created_at = now - timedelta(minutes=i)
        db.add(mem)
        out.append(mem)
    await db.commit()
    return out


async def _seed_civic_result(db, rid: str, content: str, *,
                             minutes_ago: int = 90) -> Memory:
    """镇务结果档 —— 判据与 ``_query_recent_civic_results`` 同源。"""
    mem = Memory(resident_id=rid, type="event", content=content,
                 importance=_CIVIC_NORMALIZED, source="civic",
                 metadata_json={"civic_event": "civic:poll_result:p-1",
                                "raw_importance": 0.9})
    mem.created_at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    db.add(mem)
    await db.commit()
    return mem


async def _seed_world_event(db, rid: str, content: str, *, tier: str | None,
                            minutes_ago: int = 100) -> Memory:
    """世界事件记忆。``tier=None`` 复现**存量那 1380 条**(没有这个键)的形状,
    ``tier='trivia'`` 是天气档 —— 两种都不该被专用道收走。"""
    meta: dict = {"first_hand": True, "event_id": f"w-{minutes_ago}"}
    if tier is not None:
        meta["tier"] = tier
    mem = Memory(resident_id=rid, type="event", content=content,
                 importance=_WE_IMPORTANCE, source="world_event",
                 metadata_json=meta)
    mem.created_at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    db.add(mem)
    await db.commit()
    return mem


# ── 跑真插件、抓真 prompt ────────────────────────────────────────────────

@pytest.fixture
def gate(monkeypatch):
    """三个闸**显式**设成想要的形状,不吃机器上的 ``.env``。

    两条 ``REALISM_POOL_*`` 一律按 0 钉住:计划阶段这条路与候选池那两条道**相互
    独立**(它不走 ``retrieve_context``)。不钉的话,哪天有人在 ``.env`` 里开了池
    保留位,这里就分不清「公共记忆进 prompt」是本段接线的功劳还是那两条道的。
    """
    def _set(n: int):
        monkeypatch.setattr(settings, "realism_plan_public_memories", n)
        monkeypatch.setattr(settings, "realism_pool_civic_reserve", 0)
        monkeypatch.setattr(settings, "realism_pool_world_event_reserve", 0)
    return _set


async def _capture_plan_prompt(db, resident: Resident, *,
                               template: str | None = None) -> str:
    """跑一次**真** ``BasicPlanPlugin.execute``,把交给 LLM 的 user prompt 抓回来。

    只替换两样东西:``llm_chat``(不打真模型)与 ``manager``(不开 websocket)。
    取数、去重、渲染全走真代码 —— 「进了容器」不等于「进了 prompt」,只有抓最终
    文本才算数。

    ``template`` 给逐字节对拍用:把 ``PLAN_USER_PROMPT`` 换成改前那份冻结快照跑
    同一条链。``str.format`` 忽略多余的关键字实参,所以旧模板照样渲染得出来。
    """
    resident.daily_plans_json = None
    resident.daily_goal_json = None
    ctx = TickContext(
        db=db, resident=resident, world_time="10:00", hour=10,
        schedule_phase="上午",
        available_actions=[ActionType.WORK, ActionType.IDLE])
    mock_llm = AsyncMock(return_value=_LLM_JSON)
    plugin = basic.BasicPlanPlugin(params={"hourly_slots": 3})
    with patch.object(basic, "llm_chat", mock_llm), \
            patch.object(basic, "manager") as mock_mgr:
        mock_mgr.broadcast = AsyncMock()
        if template is None:
            await plugin.execute(ctx)
        else:
            with patch.object(basic, "PLAN_USER_PROMPT", template):
                await plugin.execute(ctx)
    assert mock_llm.call_count == 1, (
        "计划没生成出来(llm_chat 没被调用一次)—— 后面对 prompt 的断言无从谈起。"
        "_generate_plan 里抛了异常会被 execute 吞成一行 warning。")
    return mock_llm.call_args.args[1][0]["content"]


#: prompt 里那一段的标题。写死在测试里而不是从实现 import:标题变了就是 prompt
#: 变了,应当由这条测试当场红,而不是跟着实现一起漂。
_HEADING = "镇上最近发生的事："


# ── ①闸关(=0):逐字节等于改前 ─────────────────────────────────────────

#: 轨 1 的入参形状。**每一组都在库里放着公共记忆** —— 闸关时它们必须一条都不出现,
#: 空库那组证明不了这件事。
_PARITY_CASES = [
    pytest.param(True, True, id="civic+world_event-both-present"),
    pytest.param(True, False, id="civic-only"),
    pytest.param(False, True, id="world_event-only"),
    pytest.param(False, False, id="nothing-public-at-all"),
]


@pytest.mark.anyio
@pytest.mark.parametrize("has_civic,has_we", _PARITY_CASES)
async def test_gate_zero_renders_the_plan_prompt_byte_for_byte_as_before(
        db_session, gate, has_civic, has_we):
    """``realism_plan_public_memories=0`` → prompt 与改前**逐字节**相同。

    对拍的两边都跑真 ``BasicPlanPlugin``,只有模板不同:一边是新模板(带
    ``{public_memories}``)、一边是 ``69f07a7`` 那份冻结快照。两边渲染出同一串
    字节,才谈得上「默认关 = 什么都没改」。
    """
    r = await _resident(db_session)
    await _seed_personal(db_session, r.id, 6)
    if has_civic:
        await _seed_civic_result(db_session, r.id, "公投结果：茶税定在 5%")
    if has_we:
        await _seed_world_event(db_session, r.id, "镇上要修一座剧院",
                                tier=wes.TIER_SUBSTANTIVE)

    gate(0)
    before = await _capture_plan_prompt(db_session, r,
                                        template=_FROZEN_PLAN_USER_PROMPT)
    after = await _capture_plan_prompt(db_session, r)

    assert after == before, "闸关时 plan prompt 与改前不是逐字节相同"
    assert _HEADING not in after


@pytest.mark.anyio
async def test_gate_zero_does_not_even_read_the_public_memories(db_session, gate):
    """闸关时连取数都不发生 —— 「0 = 旧行为」也包括不多打一次库。"""
    r = await _resident(db_session)
    await _seed_personal(db_session, r.id, 3)
    await _seed_civic_result(db_session, r.id, "公投结果：茶税定在 5%")

    gate(0)
    with patch.object(MemoryService, "get_public_memories",
                      new=AsyncMock(return_value=[])) as spy:
        await _capture_plan_prompt(db_session, r)
    spy.assert_not_awaited()


def test_the_frozen_template_is_not_the_current_one():
    """防恒绿:冻结快照必须**不等于**现在的模板。

    没有这条,哪天有人把冻结快照「顺手同步」成当前模板,上面那批逐字节对拍就变成
    「自己等于自己」—— 全绿,而不变量 1 一句话也没守。
    """
    assert _FROZEN_PLAN_USER_PROMPT != basic.PLAN_USER_PROMPT, (
        "冻结快照与当前模板一字不差 —— 对拍在对自己,恒绿")
    assert "{public_memories}" in basic.PLAN_USER_PROMPT
    assert "{public_memories}" not in _FROZEN_PLAN_USER_PROMPT


def test_track2_pinned_template_matches_the_frozen_copy():
    """轨 2:``69f07a7`` 那份真实模板 == 轨 1 的冻结快照(拿不到就 skip)。"""
    src = _pinned_plan_source()
    if src is None:
        pytest.skip(_TRACK1_IS_THE_REAL_GUARD.format(
            sha=_PRE_PLAN_SHA[:7],
            why="这个仓里取不到那个对象(浅克隆 / 无 git / 源码 tarball)"))
    assert "public_memories" not in src, (
        f"从 {_PRE_PLAN_SHA[:7]} 取到的 basic.py 里已经有本段的接线了 —— 对拍对的是"
        "它自己,这条断言恒绿。SHA 钉错了(应为本段落地**之前**的起点)。")
    assert _template_literal_of(src) == _FROZEN_PLAN_USER_PROMPT, (
        "轨 1 的冻结快照与 69f07a7 那份真实模板不一致 —— 快照抄漂了")


# ── ②闸开:两条道各出一条,都进最终 prompt ─────────────────────────────

_CIVIC_TEXT = "公投结果：茶税定在 5%"
_WE_TEXT = "镇上要修一座剧院"


@pytest.mark.anyio
async def test_open_gate_puts_both_lanes_into_the_plan_prompt(db_session, gate):
    """闸开 + 有镇务结果 + 有实质世界事件 → 两条都出现在 prompt 里。

    对照那半格(``gate(0)``)是必需的:没有它,「它在 prompt 里」这句话可以由一个
    本来就把所有记忆都塞进去的 prompt 说出来。两条公共记忆的 importance 分别是
    0.99 与 0.5,但它们**都在最近 20 条之外**(90/100 分钟前,而 24 条个人琐事占满
    了最近 24 分钟),所以闸关时一条都进不去。
    """
    r = await _resident(db_session)
    await _seed_personal(db_session, r.id, 24)
    await _seed_civic_result(db_session, r.id, _CIVIC_TEXT)
    await _seed_world_event(db_session, r.id, _WE_TEXT, tier=wes.TIER_SUBSTANTIVE)

    gate(0)
    closed = await _capture_plan_prompt(db_session, r)
    assert _CIVIC_TEXT not in closed and _WE_TEXT not in closed, (
        "闸关时它们就已经在 prompt 里了 —— 下面那半格的「进去了」证明不了任何事")

    gate(2)
    opened = await _capture_plan_prompt(db_session, r)
    assert _HEADING in opened
    assert _CIVIC_TEXT in opened, "镇务结果没进 plan prompt"
    assert _WE_TEXT in opened, "实质世界事件没进 plan prompt"


@pytest.mark.anyio
async def test_the_cap_is_the_gate_value_and_both_lanes_get_a_seat(db_session, gate):
    """``N`` 是**上限**,且两条道轮流出 —— 一条道再活跃也吃不掉另一条的名额。

    纯按 ``created_at DESC`` 合并的话,三条新鲜镇务会把世界事件挤没;而计划阶段要的
    正是「镇上出了什么事」的**两个方面**。
    """
    r = await _resident(db_session)
    await _seed_personal(db_session, r.id, 24)
    for i in range(3):
        await _seed_civic_result(db_session, r.id, f"公投结果{i}", minutes_ago=60 + i)
    await _seed_world_event(db_session, r.id, _WE_TEXT,
                            tier=wes.TIER_SUBSTANTIVE, minutes_ago=200)

    gate(2)
    prompt = await _capture_plan_prompt(db_session, r)

    public_lines = [ln for ln in prompt.split(_HEADING)[1].splitlines() if ln.startswith("- ")]
    assert len(public_lines) == 2, f"N=2 却渲染了 {len(public_lines)} 条: {public_lines}"
    assert "公投结果0" in prompt, "镇务道该出最新那条"
    assert _WE_TEXT in prompt, "世界事件那条道被镇务道吃掉了名额"
    assert "公投结果1" not in prompt


# ── ③天气永不出现 ───────────────────────────────────────────────────────

_WEATHER_TEXT = "今天多云转晴"


@pytest.mark.anyio
@pytest.mark.parametrize("tier", [
    pytest.param(wes.TIER_TRIVIA, id="tier=trivia"),
    pytest.param(None, id="no-tier-key(存量那 1380 条的形状)"),
])
async def test_weather_never_reaches_the_plan_prompt(db_session, gate, tier):
    """天气(``importance=0.5``,``tier`` 是 trivia 或**根本没有这个键**)不进 prompt。

    它是库里**最新**的一条 world_event,比那条实质事件还新 —— 按 ``created_at DESC``
    盲取的实现会当场把它取上来。同一条用例要求实质事件**进得去**,免得「天气没进去」
    这句话由一段根本没渲染的空白说出来。
    """
    r = await _resident(db_session)
    await _seed_personal(db_session, r.id, 24)
    await _seed_world_event(db_session, r.id, _WE_TEXT,
                            tier=wes.TIER_SUBSTANTIVE, minutes_ago=100)
    await _seed_world_event(db_session, r.id, _WEATHER_TEXT, tier=tier, minutes_ago=30)

    gate(2)
    prompt = await _capture_plan_prompt(db_session, r)

    assert _WE_TEXT in prompt, "实质事件都没进去,这条用例证明不了天气被挡住了"
    assert _WEATHER_TEXT not in prompt, "天气进了计划 prompt"


# ── ④与个人近况去重 ─────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_a_public_memory_also_in_recent_appears_exactly_once(db_session, gate):
    """公共记忆若恰好也在最近 20 条里,prompt 里**只能出现一次**。

    这是最容易漏的一格:镇务结果档 importance=0.99,只要它落在最近 20 条内就必然
    被 ``recent`` 选中 —— 结票后的那几分钟每位居民都是这个形状。
    """
    r = await _resident(db_session)
    # 镇务比全部个人琐事都新 → 它一定在最近 20 条里,也一定过得了 >0.5 那条筛
    await _seed_personal(db_session, r.id, 6)
    await _seed_civic_result(db_session, r.id, _CIVIC_TEXT, minutes_ago=0)

    gate(0)
    closed = await _capture_plan_prompt(db_session, r)
    assert closed.count(_CIVIC_TEXT) == 1, "改前它本来就该出现一次(在个人近况里)"

    gate(2)
    opened = await _capture_plan_prompt(db_session, r)
    assert opened.count(_CIVIC_TEXT) == 1, (
        f"去重没生效,同一条镇务记忆在 prompt 里出现了 {opened.count(_CIVIC_TEXT)} 次")


@pytest.mark.anyio
async def test_dedup_does_not_swallow_the_other_lane(db_session, gate):
    """去重只去掉**重复的那条**,另一条道照常出现 —— 去重不是「有重复就整段不渲染」。"""
    r = await _resident(db_session)
    await _seed_personal(db_session, r.id, 6)
    await _seed_civic_result(db_session, r.id, _CIVIC_TEXT, minutes_ago=0)
    await _seed_world_event(db_session, r.id, _WE_TEXT,
                            tier=wes.TIER_SUBSTANTIVE, minutes_ago=200)

    gate(2)
    prompt = await _capture_plan_prompt(db_session, r)

    assert prompt.count(_CIVIC_TEXT) == 1
    assert _WE_TEXT in prompt, "重复的那条被去掉之后,另一条道也跟着消失了"


# ── ⑤一条都没有时不渲染空标题 ────────────────────────────────────────────

@pytest.mark.anyio
async def test_no_public_memory_renders_no_empty_heading(db_session, gate):
    """闸开着但一条公共记忆都取不到 → prompt 里不出现标题,且与闸关时逐字节相同。

    这是**绝大多数居民绝大多数时候**的形状(实质事件约 1.6 条/人/周)。一个光秃秃
    的「镇上最近发生的事：」会让 LLM 去编一件根本没发生的事。
    """
    r = await _resident(db_session)
    await _seed_personal(db_session, r.id, 6)
    await _seed_world_event(db_session, r.id, _WEATHER_TEXT, tier=wes.TIER_TRIVIA)

    gate(0)
    closed = await _capture_plan_prompt(db_session, r)
    gate(2)
    opened = await _capture_plan_prompt(db_session, r)

    assert _HEADING not in opened, "一条公共记忆都没有,却渲染了空标题"
    assert opened == closed, "取不到公共记忆时 prompt 应与闸关时逐字节相同"


# ── ⑥recent 的既有 fallback 不被破坏 ────────────────────────────────────

@pytest.mark.anyio
async def test_recent_fallback_survives_with_the_gate_open(db_session, gate):
    """全部个人记忆都 ``<= 0.5`` 时,``if not recent: recent_all[:3]`` 照旧生效。

    公共记忆是**新增**的一段,不接管、也不绕过个人近况那条路。
    """
    r = await _resident(db_session)
    await _seed_personal(db_session, r.id, 5, importance=0.4)
    await _seed_world_event(db_session, r.id, _WE_TEXT,
                            tier=wes.TIER_SUBSTANTIVE, minutes_ago=200)

    gate(2)
    prompt = await _capture_plan_prompt(db_session, r)

    for i in range(3):
        assert f"个人琐事{i}" in prompt, f"fallback 的第 {i} 条没进 prompt"
    assert "个人琐事3" not in prompt, "fallback 应当只取 3 条"
    assert _WE_TEXT in prompt


# ── 取数出问题时 fail-open ───────────────────────────────────────────────

@pytest.mark.anyio
async def test_public_memory_failure_never_blocks_plan_generation(db_session, gate):
    """公共记忆取数抛异常 → 计划照样生成,prompt 退回闸关那一版。

    ``_generate_plan`` 的异常会被 ``execute`` 吞成一行 warning,**整天的计划就没了**
    (居民当天无目标、无时段计划)。一段锦上添花的 prompt 绝不该有这个权力。
    """
    r = await _resident(db_session)
    await _seed_personal(db_session, r.id, 6)
    await _seed_civic_result(db_session, r.id, _CIVIC_TEXT, minutes_ago=200)

    gate(0)
    closed = await _capture_plan_prompt(db_session, r)

    gate(2)
    with patch.object(MemoryService, "get_public_memories",
                      new=AsyncMock(side_effect=RuntimeError("库炸了"))):
        opened = await _capture_plan_prompt(db_session, r)

    assert opened == closed
    assert r.daily_plans_json is not None, "计划没生成出来"


# ── 取数层:与候选池共用同一份判据 ────────────────────────────────────────

@pytest.mark.anyio
async def test_get_public_memories_is_the_same_criterion_as_the_two_lanes(
        db_session, gate):
    """``get_public_memories`` 与两条道共用同一份取数 —— 不另起一套判据。

    第 2/3 段那两条道的判据(结果档 / ``tier='substantive'``)是被生产实测钉过的;
    计划阶段若另写一份 SQL,两边就会各自漂。这条用例把「同一份」钉成断言:同样的
    库,``get_public_memories`` 拿到的必须是两条道各自拿到的那些行。
    """
    r = await _resident(db_session)
    await _seed_personal(db_session, r.id, 6)
    civic = await _seed_civic_result(db_session, r.id, _CIVIC_TEXT)
    we = await _seed_world_event(db_session, r.id, _WE_TEXT,
                                 tier=wes.TIER_SUBSTANTIVE)
    await _seed_world_event(db_session, r.id, _WEATHER_TEXT, tier=wes.TIER_TRIVIA,
                            minutes_ago=5)

    svc = MemoryService(db_session)
    got = await svc.get_public_memories(r.id, 5)
    assert {m.id for m in got} == {civic.id, we.id}

    # 与候选池那两条道对同一批行(把两条道的 reserve 开到能看见它们)
    monkey_civic = await svc._query_recent_civic_results(r.id, 5)
    monkey_we = await svc._query_recent_substantive_world_events(r.id, 5)
    assert [m.id for m in monkey_civic] == [civic.id]
    assert [m.id for m in monkey_we] == [we.id]


@pytest.mark.anyio
async def test_get_public_memories_is_off_when_limit_is_not_positive(db_session):
    """``limit <= 0`` → 空列表(闸关那一格的语义,也是负数配置的兜底)。"""
    r = await _resident(db_session)
    await _seed_civic_result(db_session, r.id, _CIVIC_TEXT)
    svc = MemoryService(db_session)
    assert await svc.get_public_memories(r.id, 0) == []
    assert await svc.get_public_memories(r.id, -1) == []


@pytest.mark.anyio
async def test_get_public_memories_ignores_the_pool_reserve_knobs(db_session, monkeypatch):
    """计划阶段这条路**不吃** ``REALISM_POOL_*``:那两条闸关着它照样拿得到。

    两件事口径不同(候选池 vs 计划 prompt),耦合在一起的话「开了 plan 闸却什么都
    没变」会是个查半天的静默失败。
    """
    monkeypatch.setattr(settings, "realism_pool_civic_reserve", 0)
    monkeypatch.setattr(settings, "realism_pool_world_event_reserve", 0)
    r = await _resident(db_session)
    civic = await _seed_civic_result(db_session, r.id, _CIVIC_TEXT)

    got = await MemoryService(db_session).get_public_memories(r.id, 2)
    assert [m.id for m in got] == [civic.id]


@pytest.mark.anyio
async def test_other_residents_public_memories_never_leak(db_session, gate):
    """公共记忆也是**每人一条**的记忆行 —— 别人的那条不进我的 prompt。"""
    me = await _resident(db_session, "gu-mingyuan")
    other = await _resident(db_session, "he-qiaoyun")
    await _seed_personal(db_session, me.id, 6)
    await _seed_civic_result(db_session, other.id, "别人的公投结果")

    gate(2)
    prompt = await _capture_plan_prompt(db_session, me)
    assert "别人的公投结果" not in prompt
    assert _HEADING not in prompt
