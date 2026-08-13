"""政策键 → 说得出口的中文标签。**读写两侧共享的唯一一份。**

政策目录的键是英文 snake_case(那是 API / 前端 / DB 的口径)。这些键有两条路能
走到 NPC 面前:

- **写侧** ``policy_service._open_amend_poll`` 把键拼进公投标题
  (``将「tax_rate」调整为 0.05``),这条标题还会被 ``_clerk_announce`` 广播成全镇
  14 人的持久记忆 —— 一旦写进去就擦不掉了;
- **读侧** ``town_facts_service._read_open_polls`` 把 ``Poll.question`` 原样送进
  「小镇现况」段,而 ``open_polls`` 在 ``DECIDE_FACT_KEYS`` 里,直通每位居民每个
  tick 的 decide prompt —— 一头撞上 K4 的
  ``assert "tax" not in blob.lower()``(tests/test_treasury_service.py:851)。

**两侧都要挡**:写侧治新数据,读侧治存量。生产 2026-08-09 正开着一张
``将「tax_rate」调整为 0.05`` 的公决(08-11 12:30 UTC 截止),那张的 question 已经
落库了,只有读侧那道够得着它。

标签表按 ``POLICY_CATALOG`` 的全集给(不只是 ``POLICY_WHITELIST`` 那 6 条):任何
投票档的键都能被 ``propose_amend`` 拼进标题,少一条就漏一条英文键。漂移由
``tests/test_policy_service.py::test_every_catalog_key_has_a_spoken_label`` 兜。

本模块**零 app 内部依赖**,故 ``app/llm/prompt.py`` 与 ``app/services/*`` 都能直接
导入而不成环。
"""
from __future__ import annotations

#: 政策键 → 中文标签。键集 = ``policy_service.POLICY_CATALOG`` 的全集。
POLICY_LABELS: dict[str, str] = {
    # 行政级
    "civic_poll_days": "公投时长",
    "market_day_weekday": "集市日",
    "market_day_discount": "集市日售价",
    # 简单多数级
    "tax_rate": "税率",
    "medical_subsidy_sc": "医疗补贴",
    "npc_default_wage_sc": "基础工钱",
    "caravan_enabled": "外来商队准入",
    "curfew_hours": "宵禁",
    "business_hours": "营业时间",
    # 绝对多数级
    "election_interval_days": "选举间隔",
    "recall_threshold": "罢免门槛",
    "approval_routing": "审批路由",
    "housing_development_scale": "住房开发规模",
    # 宪法核心(不可修改,但「有人试图改它」的公告仍会提到键名)
    "election_exists": "选举制度",
    "exile_right": "放逐权",
    "lab_approval_gate": "实验楼审批",
    "lab_envelope_definition": "实验楼信封",
    "lab_self_governance_immunity": "实验楼自治豁免",
}

#: 长键在前:短键是长键前缀时(将来若出现 ``tax`` / ``tax_rate`` 这种)先替换长的,
#: 否则长键会被短键切成半截。现在的键集没有这种重叠,排序是为以后加键上的保险。
_REPLACEMENTS: tuple[tuple[str, str], ...] = tuple(
    sorted(POLICY_LABELS.items(), key=lambda kv: -len(kv[0]))
)


def policy_label(key: str) -> str:
    """单个键的中文标签。认不出的键原样返回 —— 渲染层不为一个没见过的键整段哑掉。"""
    return POLICY_LABELS.get(key, key)


def scrub_policy_keys(text: str) -> str:
    """把自由文本里嵌着的原始政策键就地折成中文标签。

    读侧的兜底:``Poll.question`` 是**已落库的自由文本**,分不出哪一段是键,只能
    按子串替换。这是刻意的宽口径 —— 与其漏一条英文键进 prompt,不如把一句碰巧写
    了 ``tax_rate`` 的普通议案标题也一并折成「税率」(读起来照样通顺)。
    """
    if not text:
        return text
    for key, label in _REPLACEMENTS:
        if key in text:
            text = text.replace(key, label)
    return text
