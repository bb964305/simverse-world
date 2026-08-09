"""S3/S4/S5 —— 「小镇现况」段的渲染层、自身事实层与两处接线。

``format_town_facts`` 是纯函数,不查库不看闸门:S2 那层决定**有没有事实**(闸关
返 ``{}``),这层只决定**怎么说**。所以本文件前半段一条 db 夹具都不用;后半段
(S4 的 ``self`` 事实、S5 的两处接线)必须落到真库,因为 B3 的串人风险只在「同一
进程先后服务两位居民」时才现形,而接线要证明的恰恰是「真跑一遍链路事实到得了
prompt 里」。

三条硬约束各有断言守着:

- **不传即逐字节旧行为**:``assemble_system_prompt`` 新参数是尾部可选参数,不传
  时输出与改动前一模一样(下面的 ``_GOLDEN`` 是改动前真跑出来的固化快照),且
  ``"记忆" not in prompt``(K3:tests/test_memory_prompt.py:79 的既有断言)。
- **真 ``Resident`` 实例**:K10 —— ``MagicMock(spec=Resident)`` 的属性访问永远
  返回 mock,新段会静默渲染成 ``<MagicMock id=...>`` 串而测试照样绿。
- **decide 侧只拿裁剪子集**:K4 —— 那条链路有「全文不得出现 tax /
  town_treasury / 镇财政 / 镇库余额数字」的既有硬断言
  (tests/test_treasury_service.py:849-851)。
"""
import json
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.llm.prompt import assemble_system_prompt, format_town_facts
from app.models.conversation import Conversation
from app.models.issue_stance import IssueStance
from app.models.resident import Resident
from app.models.season import Poll
from app.models.town_treasury import TOWN_KEY, TownTreasury
from app.models.user import User
from app.services import town_facts_service as tfs, world_event_service
from app.services.config_service import ConfigService
from app.services.town_facts_service import DECIDE_FACT_KEYS

#: 改动前(S3 之前)对下面这位居民真跑出来的输出,逐字固化。
_GOLDEN = (
    "你是 陈铁生，住在 Simverse World 的east_garden街区。\n"
    "\n"
    "## 灵魂（你为什么这样做）\n"
    "我打铁,也打抱不平。\n"
    "\n"
    "## 人格（你怎么做、怎么说）\n"
    "话不多,手上有准头。\n"
    "\n"
    "## 能力（你能做什么）\n"
    "会打铁、会修农具。\n"
    "\n"
    "请始终保持角色扮演，用你的人格风格回应访客。回复简洁，不超过200字。"
)

#: 满配事实(七类俱全),形状逐字对齐 town_facts_service 的返回契约。
_FULL_FACTS = {
    "mayor": {"slug": "he-qiaoyun", "name": "何巧云"},
    "duties": [
        {"slug": "luo-xiaozhou", "name": "骆小舟", "title": "邮差"},
        {"slug": "zhao-qiwen", "name": "赵启文", "title": "公告与登记处"},
    ],
    "policies": {
        "tax_rate": 0.05,
        "business_hours": {"open": 8, "close": 20},
        "curfew_hours": [22, 6],
        "npc_default_wage_sc": 5,
        "market_day_discount": 0.9,
        "medical_subsidy_sc": 3,
    },
    "treasury_sc": 1234,
    "open_polls": [{
        "question": "是否在东岸花园兴建剧院",
        "options": ["赞成兴建", "暂缓,维持现状"],
        "closes_at": "2026-08-11T12:30:00+00:00",
    }],
    "today": {"date": "2026-08-09", "weekday": 6, "is_market_day": True},
    "places": ["市政厅", "酒馆", "诊所"],
}


def _resident() -> Resident:
    return Resident(slug="chen-tiesheng", name="陈铁生", district="east_garden",
                    status="idle", tile_x=0, tile_y=0,
                    soul_md="我打铁,也打抱不平。",
                    persona_md="话不多,手上有准头。",
                    ability_md="会打铁、会修农具。")


def _facts(**overrides) -> dict:
    return {**_FULL_FACTS, **overrides}


# ── 不传即旧行为 ────────────────────────────────────────────────────────

def test_prompt_without_town_facts_is_byte_identical():
    """S3 的全部价值前提:闸关(S5 传 None)时这条链路一个字节都不能变。"""
    prompt = assemble_system_prompt(_resident())
    assert prompt == _GOLDEN
    assert "小镇现况" not in prompt
    assert "记忆" not in prompt, "K3:无可选参数时不得出现「记忆」二字"


def test_empty_facts_render_nothing():
    """``{}`` 是 S2 闸关的返回值,``None`` 是调用方压根没取数 —— 两者同义。"""
    for empty in ({}, None):
        assert assemble_system_prompt(_resident(), town_facts=empty) == _GOLDEN
    assert format_town_facts({}) == ""


# ── 满配渲染 ────────────────────────────────────────────────────────────

def test_all_seven_sections_render():
    text = format_town_facts(_FULL_FACTS)
    assert "何巧云" in text, "mayor"
    assert "赵启文" in text and "公告与登记处" in text, "duties"
    assert "税率" in text and "5%" in text, "policies"
    assert "1234" in text, "treasury_sc"
    assert "是否在东岸花园兴建剧院" in text, "open_polls"
    assert "2026-08-09" in text and "周日" in text, "today"
    assert "市政厅" in text and "酒馆" in text, "places"


def test_policy_keys_are_spoken_chinese_not_raw_keys():
    """prompt 里出现 ``tax_rate`` 这种英文键,NPC 会学着复读它。"""
    text = format_town_facts(_FULL_FACTS)
    for raw in ("tax_rate", "business_hours", "curfew_hours",
                "npc_default_wage_sc", "market_day_discount", "medical_subsidy_sc"):
        assert raw not in text, f"{raw} 的英文键名漏进了 prompt"
    assert "营业时间" in text and "宵禁" in text and "医疗补贴" in text


def test_section_sits_between_memory_and_world_events():
    """由私到公的顺序:记忆(我记得的)→ 小镇现况(大家都知道的)→ 世界事件(正在
    发生的)。段前必须有空行 —— 记忆段自己不带后置空行(K8)。"""
    from unittest.mock import MagicMock

    from app.models.memory import Memory

    mem = MagicMock(spec=Memory)
    mem.content = "上周和白杏聊过打铁的事"
    mem.metadata_json = None
    prompt = assemble_system_prompt(
        _resident(),
        memory_context={"relationship": None, "reflections": [], "events": [mem]},
        world_events=[{"title": "元宵灯会", "description": "全城挂起花灯"}],
        town_facts=_FULL_FACTS,
    )
    assert "\n\n## 小镇现况\n" in prompt, "段前要有空行"
    assert prompt.index("## 记忆") < prompt.index("## 小镇现况") < prompt.index("## 当前世界事件")


# ── 空态 ────────────────────────────────────────────────────────────────

def test_vacant_mayor_says_so_instead_of_going_silent():
    """镇长空缺是常态(任期到期、罢免后)。不说话 = NPC 继续拿旧认知瞎编。"""
    text = format_town_facts(_facts(mayor=None))
    assert "镇长之位空缺" in text
    assert "何巧云" not in text


def test_zero_treasury_is_not_a_missing_treasury():
    """0 与缺失语义不同:0 = 镇库真的空了,None = 这个世界没有镇库这回事。"""
    assert "镇库余额 0 枚硬币" in format_town_facts(_facts(treasury_sc=0))
    assert "镇库" not in format_town_facts(_facts(treasury_sc=None))


def test_no_open_polls_renders_no_subsection():
    text = format_town_facts(_facts(open_polls=[]))
    assert "正在议" not in text
    assert "市政厅" in text, "其余各类照常渲染"


def test_empty_curfew_reads_as_none_not_empty_list():
    text = format_town_facts(_facts(policies={"curfew_hours": []}))
    assert "宵禁 无" in text
    assert "[]" not in text


def test_partial_facts_do_not_raise():
    """S5 的 decide 侧只传 DECIDE_FACT_KEYS 裁剪子集 —— 缺键必须整段跳过而不是
    KeyError。政策与镇库不在子集里,渲染结果里也就不该有它们。"""
    text = format_town_facts({k: _FULL_FACTS[k] for k in DECIDE_FACT_KEYS})
    assert "何巧云" in text and "市政厅" in text
    assert "税率" not in text and "镇库" not in text


# ── S4:自身事实(self 段) ────────────────────────────────────────────────

#: 一份真的 duty(形状照 seed/preset_characters.py 的 meta_json["duty"])。
_DUTY = {
    "key": "postman", "title": "邮差",
    "prompt_hint": "你每天沿着固定路线送信,顺路把镇上的消息带给沿途的人。",
}
_ISSUE = "该不该给夜路装灯"
_SELF = {
    "duty_title": _DUTY["title"],
    "duty_hint": _DUTY["prompt_hint"],
    "stances": [{"issue": _ISSUE, "label": "支持"}],
}


@pytest.fixture(autouse=True)
def _clean_caches():
    """S4 的 ``build_town_facts`` 会碰进程内共享快照(K11:conftest 没有 autouse
    重置)。两层都要清:事实快照本身,以及 ``today`` 依赖的活跃世界事件缓存。"""
    tfs._reset_for_tests()
    world_event_service.invalidate_active_cache()
    yield
    tfs._reset_for_tests()
    world_event_service.invalidate_active_cache()


@pytest.fixture
def facts_on(monkeypatch):
    monkeypatch.setattr(settings, "civic_facts_enabled", True)


@pytest.fixture
def opinion_on(monkeypatch):
    monkeypatch.setattr(settings, "polis_opinion_enabled", True)


def _db_resident(slug: str, name: str, *, duty: dict | None = None) -> Resident:
    return Resident(slug=slug, name=name, district="east_garden", status="idle",
                    tile_x=0, tile_y=0,
                    meta_json=({"duty": duty} if duty else None))


def _stance_row(slug: str, issue: str, stance: float) -> IssueStance:
    return IssueStance(issue_key=issue, resident_slug=slug, stance=stance,
                       interact_count=1, last_update_at=datetime.now(UTC))


def test_self_section_renders_duty_title_and_hint_verbatim():
    """M5:真正可对话的事实是 ``prompt_hint`` 原文,``title`` 只是个标签。

    这条 hint 里没有动作码,所以「原文」与 C2 剥壳后的结果是同一个串 —— 带动作
    码的 hint 归下面那一组测试管。"""
    text = format_town_facts(_facts(self=_SELF))
    assert "邮差" in text
    assert _DUTY["prompt_hint"] in text


# ── C2:玩家可见的 duty_hint 不带内部动作码 ──────────────────────────────
#
# seed 的 prompt_hint 是照 **decide 语境**写的,里面嵌着 ActionType 的取值
# (``seed/preset_characters.py:113`` 的 ``倾听心事(WORK/CHAT_RESIDENT)``、
# ``:691`` 的 ``优先 RESEARCH``)。M5 要求「用原文」,但那条要求写在 decide 语
# 境下;S4 把这段 hint 送进了**玩家可见**的对话 prompt,NPC 于是可能对玩家复读
# ``WORK/CHAT_RESIDENT``。与 F1 把 ``tax_rate`` 折成中文标签是同一个道理:内部
# 词汇不出网。decide 侧照旧拿原文(它就是靠这些码做决策的)。

#: seed 里两种带码写法的真样本(逐字取自 seed/preset_characters.py)。
_HINT_BRACKETED = ("你经营着咖啡馆,白天多在店里招待客人、倾听心事"
                   "(WORK/CHAT_RESIDENT);和你聊过的人心情会好起来。")
_HINT_BARE = ("你在实验楼做研究,维护着小镇唯一的镇况日志;在实验楼时优先 "
              "RESEARCH,平日记录观测(WORK/REFLECT)。")


def _self_text(hint: str) -> str:
    return format_town_facts({"self": {"duty_title": "客厅主理人",
                                       "duty_hint": hint, "stances": []}})


def test_player_facing_duty_hint_strips_bracketed_action_codes():
    """括号里的动作码整组剥掉,中文正文一个字不少。"""
    text = _self_text(_HINT_BRACKETED)
    assert "WORK" not in text and "CHAT_RESIDENT" not in text
    assert "(" not in text and ")" not in text, "空括号也不能留"
    for kept in ("客厅主理人", "你经营着咖啡馆", "倾听心事",
                 "和你聊过的人心情会好起来"):
        assert kept in text, f"hint 的中文正文被误伤:{kept} 不见了"


def test_player_facing_duty_hint_strips_bare_action_codes():
    """裸的全大写动作词(``优先 RESEARCH``)同样要剥 —— 它不在括号里。"""
    text = _self_text(_HINT_BARE)
    for code in ("RESEARCH", "WORK", "REFLECT"):
        assert code not in text, f"动作码 {code} 漏进了玩家可见的 prompt"
    for kept in ("你在实验楼做研究", "维护着小镇唯一的镇况日志", "平日记录观测"):
        assert kept in text
    assert "优先 ," not in text and "优先 。" not in text, \
        "剥完要顺手收拾掉动作码留下的空格"


def test_no_preset_duty_hint_leaks_an_action_code_to_players():
    """漂移网:seed 里**每一条** duty hint 过一遍渲染,ActionType 的取值一个都
    不许留下 —— 将来新增角色时照 decide 口吻写 hint 也不会漏出去。"""
    from app.agent.actions import ActionType
    from seed.preset_characters import PRESET_CHARACTERS

    hints = [(c["slug"], ((c.get("meta_json") or {}).get("duty") or {}).get("prompt_hint"))
             for c in PRESET_CHARACTERS]
    hints = [(slug, h) for slug, h in hints if h]
    assert len(hints) >= 11, "seed 的营生 hint 少了,这条网就是空转的"
    coded = [(slug, h) for slug, h in hints
             if any(a.value in h for a in ActionType)]
    assert coded, "seed 里必须确有带动作码的 hint,否则这条网证明不了什么"

    for slug, hint in hints:
        text = _self_text(hint)
        for action in ActionType:
            assert action.value not in text, \
                f"{slug} 的 duty hint 把 {action.value} 带进了玩家可见的 prompt"


def test_decide_side_still_gets_the_verbatim_hint_with_action_codes():
    """decide 侧**保持原文**:那条链路靠动作码把 NPC 的一天安排在营生上,剥了
    等于把 M5 接的线又拆掉。它走 ``duty_service.prompt_hint``,与玩家对话侧的
    ``self.duty_hint`` 是两条独立通路。"""
    from unittest.mock import MagicMock

    from app.agent.actions import ActionType
    from app.agent.prompts import build_decision_prompt

    r = MagicMock()
    r.name = "伊莎贝拉"
    r.persona_md = "p"
    r.tile_x, r.tile_y, r.status = 1, 1, "idle"
    r.mood_json = None
    r.meta_json = {"duty": {"key": "cafe_host", "title": "客厅主理人",
                            "prompt_hint": _HINT_BRACKETED}}

    system, user = build_decision_prompt(
        resident=r, schedule_phase="afternoon", world_time="14:00",
        nearby_residents=[], memories=[], today_actions=[],
        available_actions=[ActionType.IDLE], max_daily_actions=20,
    )
    assert _HINT_BRACKETED in system + user, "decide prompt 必须还是 hint 原文"


def test_self_stances_are_qualitative_never_numeric():
    """spec §2 非目标:探针数值(声誉分 / relation / stance)永不进 prompt。"""
    text = format_town_facts({"self": _SELF})
    assert _ISSUE in text and "支持" in text
    assert not any(ch.isdigit() for ch in text), f"立场数值漏进了 prompt:{text}"


def test_public_only_facts_render_no_first_person_line():
    """公共快照(以及 decide 的裁剪子集)里不该冒出第一人称的自身事实。"""
    text = format_town_facts(_FULL_FACTS)
    assert "你自己的营生" not in text and "你对镇上议题" not in text
    assert "镇上的营生分工" in text, "公共的营生清单照常渲染"


@pytest.mark.anyio
async def test_build_town_facts_attaches_self(db_session, facts_on, opinion_on):
    r = _db_resident("luo-xiaozhou", "骆小舟", duty=_DUTY)
    db_session.add(r)
    db_session.add(_stance_row(r.slug, _ISSUE, 0.6))
    await db_session.commit()

    facts = await tfs.build_town_facts(db_session, r)
    assert facts["self"] == {
        "duty_title": "邮差",
        "duty_hint": _DUTY["prompt_hint"],
        "stances": [{"issue": _ISSUE, "label": "支持"}],
    }
    prompt = assemble_system_prompt(r, town_facts=facts)
    assert _DUTY["prompt_hint"] in prompt and "支持" in prompt


@pytest.mark.anyio
async def test_build_town_facts_without_resident_is_public_only(db_session, facts_on):
    facts = await tfs.build_town_facts(db_session)
    assert facts and "self" not in facts


@pytest.mark.anyio
async def test_build_town_facts_gate_off_stays_empty(db_session):
    """总闸关 = 没有任何事实。公共段空了还硬贴自身事实等于绕过闸门。"""
    assert await tfs.build_town_facts(
        db_session, _db_resident("luo-xiaozhou", "骆小舟", duty=_DUTY)) == {}


@pytest.mark.anyio
async def test_stances_stay_empty_when_opinion_gate_off(db_session, facts_on):
    """``polis_opinion_enabled`` 关 → 立场恒空(读侧本无闸,该语义由事实层定义)。
    营生不归舆论闸门管,照常出。"""
    r = _db_resident("luo-xiaozhou", "骆小舟", duty=_DUTY)
    db_session.add(r)
    db_session.add(_stance_row(r.slug, _ISSUE, 0.6))
    await db_session.commit()

    me = (await tfs.build_town_facts(db_session, r))["self"]
    assert me["stances"] == []
    assert me["duty_title"] == "邮差"


@pytest.mark.anyio
async def test_self_facts_never_leak_between_residents(db_session, facts_on, opinion_on):
    """B3:一个 uvicorn worker 内所有会话共用同一份公共快照 —— per-resident 的
    事实一旦渗进去就是串人(A 的营生长在 B 嘴里)。"""
    a = _db_resident("luo-xiaozhou", "骆小舟", duty=_DUTY)
    b = _db_resident("a-lan", "阿岚")
    db_session.add_all([a, b])
    db_session.add(_stance_row(a.slug, _ISSUE, 0.6))
    await db_session.commit()

    fa = await tfs.build_town_facts(db_session, a)
    fb = await tfs.build_town_facts(db_session, b)

    assert fa["self"]["duty_title"] == "邮差", "A 自己那份要照常在"
    assert fb["self"] == {"duty_title": None, "duty_hint": None, "stances": []}
    # 「骆小舟(邮差)」是公共的营生清单,两人都该看到;串人只看自身事实那段。
    assert _DUTY["prompt_hint"] not in json.dumps(fb, ensure_ascii=False)
    assert _ISSUE not in json.dumps(fb, ensure_ascii=False)
    assert "你自己的营生" not in format_town_facts(fb)
    assert "self" not in tfs._cache["facts"], "自身事实绝不能进模块级共享快照"


# ── S5:两处接线(玩家对话 / decide) ──────────────────────────────────────

_MAYOR_SLUG, _MAYOR_NAME = "he-qiaoyun", "何巧云"
_POLL_QUESTION = "是否在东岸花园兴建剧院"
#: 镇库余额:decide 侧的 K4 反证 —— 事实层读到了它,但裁剪子集里没有镇库这一类。
_TREASURY_SC = 4242


async def _elect(db, slug: str) -> None:
    """记一位现任镇长(走 ``current_mayor`` 的 system_config 兜底读法)。"""
    await ConfigService(db).set("current_mayor", slug, group="civic", updated_by="test")


async def _seed_town(db) -> None:
    """一个「有镇长、有在议的事、有镇库」的世界。镇库是故意摆的:decide 那侧读
    得到它,却必须一个数字都不往 prompt 里放。"""
    db.add(TownTreasury(key=TOWN_KEY, balance_sc=_TREASURY_SC))
    db.add(_db_resident(_MAYOR_SLUG, _MAYOR_NAME))
    db.add(Poll(question=_POLL_QUESTION, options_json=[{"label": "赞成兴建"}],
                closes_at=datetime.now(UTC) + timedelta(days=2), status="open"))
    await db.commit()
    await _elect(db, _MAYOR_SLUG)


async def _open_policy_amend_poll(db, key: str = "tax_rate", value=0.05) -> None:
    """真开一张**政策修正**公投,question 由生产代码现造。

    手写一张 ``Poll(question="是否…")`` 证明不了什么:生产的公投标题有两个来源,
    另一个是 ``PolicyService._open_amend_poll`` —— 它把**原始政策键**拼进标题
    (``将「tax_rate」调整为 0.05``),而那条标题经 ``DECIDE_FACT_KEYS`` 的
    ``open_polls`` 直通 decide prompt,一头撞上 K4 的 ``"tax" not in blob.lower()``。
    2026-08-09 生产正开着的就是这一张(税率 → 0.05,08-11 12:30 UTC 截止)。

    所以这里必须**真走那条构造路径**,不能替它写一个「看起来差不多」的标题。
    """
    from app.services.policy_service import PolicyService, TIER_SIMPLE_MAJORITY

    await PolicyService(db)._open_amend_poll(
        key, value, tier=TIER_SIMPLE_MAJORITY, threshold=0.5, quorum=False,
        author="admin:1", origin="admin")


# ── decide 接线 ─────────────────────────────────────────────────────────

def test_town_facts_is_keyword_only_on_build_decision_prompt():
    """K5:既有测试按位置传满 8 个实参(test_treasury_service.py:845 等),新参数
    只能是尾部 keyword-only,否则位置实参会串位。"""
    import inspect

    from app.agent.prompts import build_decision_prompt

    params = inspect.signature(build_decision_prompt).parameters
    assert list(params)[-1] == "town_facts", "新参数必须在参数表末尾"
    assert params["town_facts"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["town_facts"].default is None


async def _decide_blob(db, resident, monkeypatch) -> str:
    """真跑一遍 decide 的 LLM 分支,回它交给模型的全文(system + user)。

    打桩只打到 ``llm_chat`` 这一层 —— 取数、裁剪、拼 prompt 全走真代码,否则测的
    就不是「接线」而是测试自己搭的假接线。
    """
    from app.agent.actions import ActionType
    from app.agent.phases.decide import basic as decide_basic
    from app.agent.schemas import TickContext

    seen: dict = {}

    async def _capture(system, messages, **kwargs):
        seen["blob"] = system + "\n" + messages[0]["content"]
        return '{"action": "IDLE", "target_slug": null, "target_tile": null, "reason": "歇会儿"}'

    monkeypatch.setattr(decide_basic, "llm_chat", _capture)
    ctx = TickContext(db=db, resident=resident, world_time="10:00", hour=10,
                      schedule_phase="工作时段", available_actions=[ActionType.IDLE])
    await decide_basic.BasicDecidePlugin()._llm_decide(ctx)
    return seen["blob"]


@pytest.mark.anyio
async def test_decide_prompt_carries_trimmed_subset(db_session, facts_on, monkeypatch):
    """闸开:decide 拿到镇长 / 今天 / 在议的事 / 地点 —— 这四类(DECIDE_FACT_KEYS)。"""
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    await _seed_town(db_session)

    blob = await _decide_blob(
        db_session, _db_resident("chen-tiesheng", "陈铁生", duty=_DUTY), monkeypatch)

    assert _MAYOR_NAME in blob, "NPC 得知道现在谁在管事"
    assert _POLL_QUESTION in blob
    assert "市政厅" in blob


@pytest.mark.anyio
async def test_decide_prompt_never_mentions_town_finance(db_session, facts_on, monkeypatch):
    """K4 硬断言:tests/test_treasury_service.py:849-851 钉死的四类财政串,一个都
    不许出现在决策 prompt 全文里 —— 所以政策与镇库整段不进这条链路。

    世界里**真开着一张政策修正公投**(见 ``_open_policy_amend_poll``)。这一条是
    整条断言链的成立前提:``open_polls`` 在 ``DECIDE_FACT_KEYS`` 里,一张都没有的
    世界能让下面每一行都空转着变绿 —— 生产 08-11 截止的那张税率公决恰好证明「一
    张都没有」不是常态。
    """
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "civic_polls_enabled", True)
    await _seed_town(db_session)
    await _open_policy_amend_poll(db_session)

    blob = await _decide_blob(
        db_session, _db_resident("chen-tiesheng", "陈铁生"), monkeypatch)

    assert "镇上正在议的事" in blob, "公投段没渲染出来,下面的断言全是空转的"
    assert "tax" not in blob.lower()
    assert "tax_rate" not in blob, "原始政策键经公投标题直通了决策 prompt"
    assert "town_treasury" not in blob and "镇财政" not in blob
    assert str(_TREASURY_SC) not in blob, "镇库余额数字漏进了决策 prompt"
    # 政策段与镇库段整段不进这条链路。查渲染形状(段首 + 标签值)而不是「税率」二字:
    # 「将「税率」调整为 …」是一张在议公投的标题,它进 decide 是设计如此。
    assert "现行的规矩" not in blob and "税率 5%" not in blob
    assert "镇库" not in blob
    assert "你自己的营生" not in blob, "self 不在 DECIDE_FACT_KEYS 里"


@pytest.mark.anyio
async def test_decide_prompt_unchanged_when_gate_off(db_session, monkeypatch):
    """闸关 = 决策 prompt 一个字都不多。"""
    await _seed_town(db_session)

    blob = await _decide_blob(
        db_session, _db_resident("chen-tiesheng", "陈铁生"), monkeypatch)

    assert "小镇现况" not in blob and _MAYOR_NAME not in blob
    assert _POLL_QUESTION not in blob


# ── 玩家对话接线 ────────────────────────────────────────────────────────

_USER_ID, _CONV_ID, _RESIDENT_ID = "u1", "c-1", "r-1"
_REPLY = "镇长是何巧云。"


class _FakeManager:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, user_id, data):
        self.sent.append(data)


def _chat_resident() -> Resident:
    """``ctx.resident`` 是 start_chat 那侧留下的 detached 快照。真实例(K10)。"""
    r = _resident()
    r.id, r.creator_id, r.token_cost_per_turn = _RESIDENT_ID, _USER_ID, 1
    return r


@pytest.fixture
async def chat_world(db_engine):
    """跑完整 ``handle_chat_msg`` 所需的最小世界:一个玩家、一场会话、一位镇长。"""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(User(id=_USER_ID, name="U", email="u@t.co", soul_coin_balance=100))
        s.add(Conversation(id=_CONV_ID, user_id=_USER_ID, resident_id=_RESIDENT_ID))
        await _seed_town(s)
    return factory


async def _chat_turn(factory, resident) -> tuple[str, list[dict]]:
    """真跑一轮玩家对话,回 (交给模型的 system prompt, 推给玩家的消息)。

    ``assemble_system_prompt`` **不打桩** —— 打了就只剩「参数传没传」的形式验证,
    而 K6 要证的是「session 关掉之前把事实取出来了」。
    """
    from app.ws.handlers import chat as chat_handler
    from app.ws.handlers.context import ConnectionContext

    captured: dict = {}

    class _FakeRouter:
        async def chat_with_media(self, *, system_prompt, messages,
                                  media_url, media_type, meter=None):
            captured["system"] = system_prompt
            yield _REPLY

    fake = _FakeManager()
    with patch.object(chat_handler, "async_session", factory), \
         patch.object(chat_handler, "manager", fake), \
         patch.object(chat_handler, "ModelRouter", _FakeRouter), \
         patch.object(chat_handler, "reward_creator_passive",
                      new=AsyncMock(return_value=None)):
        await chat_handler.ws_limiter.reset()
        ctx = ConnectionContext(user_id=_USER_ID, user_name="U",
                                conversation_id=_CONV_ID, resident=_chat_resident())
        await chat_handler.handle_chat_msg(ctx, {"type": "chat_msg", "text": "现在镇长是谁"})
    return captured.get("system", ""), fake.sent


@pytest.mark.anyio
async def test_player_chat_prompt_carries_town_facts(chat_world, facts_on):
    """生产实测的那条缺陷:世界状态三处一致,NPC 却一个字读不到。这条断言是它的
    反面 —— 玩家问「现在镇长是谁」时,答案已经在 prompt 里了。"""
    system, _sent = await _chat_turn(chat_world, _chat_resident())

    assert "## 小镇现况" in system
    assert _MAYOR_NAME in system
    assert _POLL_QUESTION in system, "玩家对话拿的是完整事实,不是 decide 的裁剪子集"


@pytest.mark.anyio
async def test_player_chat_prompt_unchanged_when_gate_off(chat_world):
    system, _sent = await _chat_turn(chat_world, _chat_resident())

    assert "小镇现况" not in system and _MAYOR_NAME not in system


@pytest.mark.anyio
async def test_player_chat_survives_town_facts_failure(chat_world, facts_on, monkeypatch):
    """K7:取事实失败顶多少一段,绝不能让玩家聊不了天。"""
    async def _boom(db, resident=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(tfs, "build_town_facts", _boom)
    system, sent = await _chat_turn(chat_world, _chat_resident())

    assert "小镇现况" not in system
    assert any(m.get("type") == "chat_reply" and m.get("text") == _REPLY for m in sent), \
        f"回复没发出去:{sent}"
