import re

from app.agent.actions import ActionType
from app.models.resident import Resident
from app.services.policy_labels import policy_label


def format_memory_context(ctx: dict) -> str:
    """Format retrieved memory context into a prompt section.

    ctx has keys: relationship (Memory|None), reflections (list[Memory]), events (list[Memory])
    Returns empty string if no memories.
    """
    sections = []

    relationship = ctx.get("relationship")
    if relationship:
        sections.append("### 关于当前对话对象")
        sections.append(relationship.content)
        if relationship.metadata_json:
            meta = relationship.metadata_json
            tags = meta.get("tags", [])
            if tags:
                sections.append(f"印象标签：{', '.join(tags)}")
        sections.append("")

    reflections = ctx.get("reflections", [])
    if reflections:
        sections.append("### 你最近的思考")
        for r in reflections:
            sections.append(f"- {r.content}")
        sections.append("")

    events = ctx.get("events", [])
    if events:
        sections.append("### 相关的过往经历")
        for e in events:
            sections.append(f"- {e.content}")
        sections.append("")

    return "\n".join(sections) if sections else ""


#: world_clock.world_weekday() 是 Mon=0 .. Sun=6。
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _policy_text(key: str, value) -> str:
    """把政策值折成人话。形状由政策目录定死(policy_service.py:93-113):比率是
    float、营业时间是 ``{"open","close"}``、宵禁是 ``[起, 止]``、其余是整数硬币
    数。认不出的形状退回 ``str()`` —— 政策可公投改,渲染层不该为一个没见过的值
    整段哑掉。
    """
    if key in ("tax_rate", "market_day_discount") and isinstance(value, (int, float)):
        pct = f"{round(value * 100, 4):g}%"
        return f"原价的 {pct}" if key == "market_day_discount" else pct
    if key == "business_hours" and isinstance(value, dict):
        return f"{value.get('open')}点到{value.get('close')}点"
    if key == "curfew_hours":
        if not value:
            return "无"
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return f"{value[0]}点到次日{value[1]}点"
    if key in ("npc_default_wage_sc", "medical_subsidy_sc"):
        return f"{value} 枚硬币"
    return str(value)


# ── 动作码剥壳(C2:内部词汇不出网) ─────────────────────────────────────────
#
# seed 的 ``meta_json["duty"]["prompt_hint"]`` 是照 **decide 语境**写的,里面嵌着
# ``ActionType`` 的取值(``seed/preset_characters.py:113`` 的
# ``倾听心事(WORK/CHAT_RESIDENT)``、``:691`` 的 ``优先 RESEARCH``)。S4 把这段
# hint 挂进了 ``self``,而 ``self`` 只出现在**玩家可见**的对话 prompt 里
# (``DECIDE_FACT_KEYS`` 不含它)—— NPC 于是可能对着玩家复读 ``WORK/CHAT_RESIDENT``。
# 与「原始政策键出网前折成中文标签」是同一条:内部词汇不出网。
#
# decide 侧不受影响:它走 ``duty_service.prompt_hint(resident)`` 拿原文
# (``app/agent/prompts.py``),那条链路正是靠这些码把 NPC 的一天安排在营生上。

#: 动作码词汇表。只认 ``ActionType`` 的取值 —— 泛用的「全大写单词」正则会顺手
#: 吃掉 hint 里合法的 NPC / SC 这类词(UGC 的 duty hint 是玩家自由文本)。
_ACTION_CODES = frozenset(a.value for a in ActionType)

#: 整组括号(中英文括号都收):内容只由全大写词与分隔符组成时才考虑剥。
_BRACKETED_CODES = re.compile(r"[(（]\s*([A-Z][A-Z_]*(?:[/、,，\s]+[A-Z][A-Z_]*)*)\s*[)）]")

#: 裸的动作码(``在实验楼时优先 RESEARCH``)。前后不许接字母/下划线,免得吃掉
#: 某个恰好以动作码开头的长词。按长度倒序拼是免费的保险(今天没有前缀重叠的码)。
_BARE_CODE = re.compile(
    r"(?<![A-Za-z_])(?:%s)(?![A-Za-z_])"
    % "|".join(sorted(_ACTION_CODES, key=len, reverse=True)))

#: 剥完留下的空格:``优先 ,`` / 段尾多余空白。
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+(?=[,，;；。、!！?？)）])")


def _strip_action_codes(text: str) -> str:
    """把玩家可见文本里的内部动作码剥掉(整组括号 + 裸词),中文正文一字不动。

    括号那一路要求**组内至少有一个真动作码**才整组剥:``(SC)`` 这类全大写的普
    通括注留着,剥错比留着更难发现。

    裸码剥完可能留下一个语义残句(seed 的 ``在实验楼时优先 RESEARCH,`` →
    ``在实验楼时优先,``)。**刻意不连着整个短句一起丢**:短句里往往还有真内容,
    丢多了是静默丢信息;残句最多是读着别扭,既不泄密也不编事实。
    """
    if not text:
        return text

    def _drop(match: re.Match) -> str:
        tokens = re.split(r"[/、,，\s]+", match.group(1).strip())
        return "" if any(t in _ACTION_CODES for t in tokens) else match.group(0)

    out = _BARE_CODE.sub("", _BRACKETED_CODES.sub(_drop, text))
    return _SPACE_BEFORE_PUNCT.sub("", out).strip()


def format_town_facts(facts: dict) -> str:
    """把「小镇现况」事实字典折成 prompt 段落**正文**(不含标题)。

    与 ``format_memory_context`` 同一分工:标题由调用方给 —— 玩家对话那侧是
    markdown 的 ``## 小镇现况``,decide 那侧的 prompt 模板不用 markdown。

    纯函数:不查库、不看闸门。闸门在 ``town_facts_service`` 那层,这里只认「空
    字典 = 没有事实 = 一个字都不写」。行序 = S2 返回契约的键序,一一对着看就行。

    **缺键必须整段跳过**:decide 侧只传 ``DECIDE_FACT_KEYS`` 的裁剪子集(没有
    policies / treasury_sc),这里不能 KeyError。而 ``mayor: None`` 与「压根没有
    mayor 这个键」语义不同 —— 前者要明说空缺,后者是这条链路不谈镇长。
    ``self``(S4 的自身事实)同理:没有这个键 = 这次不谈「你自己」。
    """
    if not facts:
        return ""

    lines: list[str] = []

    mayor = facts.get("mayor")
    if mayor:
        lines.append(f"现任镇长：{mayor.get('name') or mayor.get('slug')}。")
    elif "mayor" in facts:
        # 空缺是常态(任期到期、罢免后)。这里不说话,NPC 就继续拿旧认知瞎编。
        lines.append("镇长之位空缺，眼下没有人当选。")

    duties = facts.get("duties") or []
    if duties:
        lines.append("镇上的营生分工：" + "、".join(
            f"{d['name']}（{d['title']}）" for d in duties))

    policies = [f"{policy_label(k)} {_policy_text(k, v)}"
                for k, v in (facts.get("policies") or {}).items() if v is not None]
    if policies:
        lines.append("现行的规矩：" + "；".join(policies))

    treasury = facts.get("treasury_sc")
    if treasury is not None:
        # 0 与缺失语义不同:0 = 镇库真空了,缺失 = 这个世界没有镇库这回事。
        lines.append(f"镇库余额 {treasury} 枚硬币。")

    polls = facts.get("open_polls") or []
    if polls:
        lines.append("镇上正在议的事：")
        for p in polls:
            opts = " / ".join(p.get("options") or [])
            closes = (p.get("closes_at") or "")[:10]
            tail = "；".join(x for x in (f"选项 {opts}" if opts else "",
                                        f"{closes} 截止" if closes else "") if x)
            lines.append(f"- {p.get('question', '')}" + (f"（{tail}）" if tail else ""))

    today = facts.get("today") or {}
    if today.get("date"):
        weekday = today.get("weekday")
        week = _WEEKDAYS[weekday] if isinstance(weekday, int) and 0 <= weekday < 7 else ""
        lines.append(f"今天是 {today['date']}{f'（{week}）' if week else ''}"
                     f"{'，是集市日' if today.get('is_market_day') else ''}。")

    places = facts.get("places") or []
    if places:
        lines.append("小镇的公共去处：" + "、".join(places))

    # 自身事实(S4)排在最后:先把公共的说完,再说「关于你自己」。这一段只有
    # build_town_facts(db, resident) 才会带上,公共快照与 decide 的裁剪子集都没有。
    me = facts.get("self") or {}
    duty_title = me.get("duty_title")
    # C2:hint 出网前剥掉 decide 侧的动作码(``self`` 只进玩家可见的对话 prompt,
    # decide 拿的是 DECIDE_FACT_KEYS 裁剪子集,不含这一段)。
    duty_hint = _strip_action_codes(me.get("duty_hint") or "")
    if duty_title or duty_hint:
        # hint 正文才是可对话的事实(M5),title 只是个能报得出口的名头。
        lines.append((f"你自己的营生：{duty_title}。" if duty_title else "")
                     + (duty_hint or ""))

    stances = me.get("stances") or []
    if stances:
        # 只出定性措辞。立场数值是探针,永不进 prompt(spec §2 非目标)。
        lines.append("你对镇上议题的态度：" + "；".join(
            f"「{s.get('issue', '')}」{s.get('label', '')}" for s in stances))

    return "\n".join(lines)


def assemble_system_prompt(
    resident: Resident,
    memory_context: dict | None = None,
    world_events: list[dict] | None = None,
    life_goal: dict | None = None,
    recent_dream: str | None = None,
    town_facts: dict | None = None,
) -> str:
    """Assemble the three-layer system prompt from resident data.

    Optionally includes memory context, active world events (S1), the
    resident's current life goal (A1), and the civic facts snapshot (「小镇现况」)
    if provided. Every extra section is opt-in: with none of them supplied the
    output is byte-for-byte what it was before each was added.
    """
    parts = [
        f"你是 {resident.name}，住在 Simverse World 的{resident.district}街区。",
        "",
    ]
    if resident.soul_md:
        parts.append("## 灵魂（你为什么这样做）")
        parts.append(resident.soul_md)
        parts.append("")
    if resident.persona_md:
        parts.append("## 人格（你怎么做、怎么说）")
        parts.append(resident.persona_md)
        parts.append("")
    if resident.ability_md:
        parts.append("## 能力（你能做什么）")
        parts.append(resident.ability_md)
        parts.append("")

    if memory_context:
        memory_text = format_memory_context(memory_context)
        if memory_text:
            parts.append("## 记忆（你记得的事）")
            parts.append(memory_text)

    # 世界公共记忆:由私到公的顺序 —— 记忆(我记得的事)→ 小镇现况(大家都知道的
    # 事)→ 世界事件(正在发生的事)。记忆段自己不带后置空行(K8),所以这里跟
    # world_events 一样自己 append("") 前置一个空行。
    if town_facts:
        facts_text = format_town_facts(town_facts)
        if facts_text:
            parts.append("")
            parts.append("## 小镇现况")
            parts.append(facts_text)

    if world_events:
        lines = [f"- {e.get('title', '')}：{e.get('description', '')}".rstrip("：") for e in world_events if e.get("title")]
        if lines:
            parts.append("")
            parts.append("## 当前世界事件（正在发生的事）")
            parts.extend(lines)

    # E1: let the resident's current mood color the tone of their reply.
    mood_label = (resident.mood_json or {}).get("label")
    if mood_label:
        parts.append("")
        parts.append(f"你现在的心情是「{mood_label}」，让语气自然体现出来。")

    # A1: the resident's life goal, so a player can chat about it.
    if life_goal and life_goal.get("title"):
        parts.append("")
        parts.append(f"你的人生目标：{life_goal['title']}（进度 {life_goal.get('progress', 0):.0%}）")

    # E2: last night's dream (if any), so "睡得好吗" gets a surprising answer.
    if recent_dream:
        parts.append("")
        parts.append(f"昨晚你做了个梦：{recent_dream}")

    parts.append("请始终保持角色扮演，用你的人格风格回应访客。回复简洁，不超过200字。")
    return "\n".join(parts)
