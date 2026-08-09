"""S3/S4 —— 「小镇现况」段的渲染层 ``format_town_facts`` 与自身事实层。

``format_town_facts`` 是纯函数,不查库不看闸门:S2 那层决定**有没有事实**(闸关
返 ``{}``),这层只决定**怎么说**。所以本文件前半段一条 db 夹具都不用;后半段
(S4 的 ``self`` 事实)必须落到真库,因为 B3 的串人风险只在「同一进程先后服务两
位居民」时才现形。

两条硬约束各有断言守着:

- **不传即逐字节旧行为**:``assemble_system_prompt`` 新参数是尾部可选参数,不传
  时输出与改动前一模一样(下面的 ``_GOLDEN`` 是改动前真跑出来的固化快照),且
  ``"记忆" not in prompt``(K3:tests/test_memory_prompt.py:79 的既有断言)。
- **真 ``Resident`` 实例**:K10 —— ``MagicMock(spec=Resident)`` 的属性访问永远
  返回 mock,新段会静默渲染成 ``<MagicMock id=...>`` 串而测试照样绿。
"""
import json
from datetime import datetime, UTC

import pytest

from app.config import settings
from app.llm.prompt import assemble_system_prompt, format_town_facts
from app.models.issue_stance import IssueStance
from app.models.resident import Resident
from app.models.user import User  # noqa: F401 —— residents.creator_id 的 FK 目标
from app.services import town_facts_service as tfs, world_event_service
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
    """M5:真正可对话的事实是 ``prompt_hint`` 原文,``title`` 只是个标签。"""
    text = format_town_facts(_facts(self=_SELF))
    assert "邮差" in text
    assert _DUTY["prompt_hint"] in text


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
