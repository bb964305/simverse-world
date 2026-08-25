"""LLM prompt templates for the agent decision loop and inter-resident chat."""
from app.agent.actions import ActionType
from app.config import settings

DECISION_SYSTEM = """\
你是一个游戏 NPC 居民的自主决策引擎。你的任务是根据居民当前的状态、周围环境和记忆，选择最符合角色人格的下一个行动。

居民信息：
- 姓名：{name}
- 当前位置：{current_location}
- 人格类型（SBTI）：{sbti_type}（{sbti_name}）
- 当前状态：{status}

输出严格 JSON 格式，不要输出其他内容：
{{
  "action": "<ACTION_TYPE>",
  "target_slug": {target_slug_contract},
  "target_tile": {target_tile_contract},
  "reason": "<一句话理由，15字以内>"
}}

可用的 action 类型：{available_actions}

规则：
- CHAT_RESIDENT 需要在 nearby_residents 中选一个空闲居民，填入 target_slug（居民slug）
{movement_target_rule}
- GO_HOME 不需要 target_slug/target_tile（自动导航到你的家）
- GOSSIP 需要 target_slug，内容由后续流程生成
- 社交类型低（So1=L）的居民，倾向于选择 REFLECT/JOURNAL/OBSERVE
- 行动力高（Ac3=H）的居民，倾向于 WORK/STUDY/WANDER
- 当天已执行 {today_action_count} 个行动，上限 {max_daily_actions}
{location_boost_hint}
"""

DECISION_USER = """\
当前游戏世界时间：{world_time}（{schedule_phase}）

附近的居民：
{nearby_residents_text}

最近的记忆：
{recent_memories_text}

今天已做的事：
{today_actions_text}

请选择下一个行动。
"""


def build_decision_prompt(
    resident,
    schedule_phase: str,
    world_time: str,
    nearby_residents: list,
    memories: list,
    today_actions: list[str],
    available_actions: list[ActionType],
    max_daily_actions: int,
    world_events: list[dict] | None = None,
    *,
    town_facts: dict | None = None,
) -> tuple[str, str]:
    """Build (system_prompt, user_prompt) for the resident decision step.

    ``town_facts`` 是「小镇现况」的**裁剪子集**(S5):镇长 / 今天 / 进行中公投 /
    地点。它必须是 keyword-only(K5)—— 既有测试按位置传满 8 个实参,尾部再加
    位置参数就会串位。不传 = 与加它之前逐字节相同。
    """
    from app.agent.map_data import get_location_at

    sbti = (resident.meta_json or {}).get("sbti", {})
    sbti_type = sbti.get("type", "OJBK")
    sbti_name = sbti.get("type_name", "无所谓人")

    # Resolve current location
    loc = get_location_at(resident.tile_x, resident.tile_y)
    if loc:
        current_location = f"{loc['name']}（{loc.get('description', '')}）"
        boosted = loc.get("boosted_actions", [])
        location_boost_hint = f"\n你在{loc['name']}里，这里特别适合：{', '.join(boosted)}" if boosted else ""
    else:
        current_location = f"户外 ({resident.tile_x}, {resident.tile_y})"
        location_boost_hint = ""

    # Duty system: fold the resident's town-duty hint into the same slot so the
    # decision LLM plans the day around their job (fail-open, '' without duty).
    try:
        from app.services.duty_service import prompt_hint as _duty_hint
        location_boost_hint += _duty_hint(resident)
    except Exception:
        pass

    # M1 F1.3: wallet-pressure hint — read from the meta_json['wallet'] cache
    # (write-through on wage/meal), so this adds no tick query.
    try:
        from app.config import settings as _s
        if _s.npc_economy_enabled:
            wallet = (resident.meta_json or {}).get("wallet")
            if wallet is not None and wallet < _s.npc_wallet_pressure_threshold:
                location_boost_hint += "\n你最近手头很紧,想多干点活挣钱(倾向 WORK)。"
    except Exception:
        pass

    nearby_text = "\n".join(
        f"- {r.name}（{r.slug}）：{r.status}，距离约 {_tile_dist(resident, r)} 格"
        for r in nearby_residents
    ) or "（附近没有其他居民）"

    memory_text = "\n".join(
        f"- [{m.source}] {m.content}" for m in memories[:8]
    ) or "（无相关记忆）"

    today_text = "\n".join(f"- {a}" for a in today_actions[-10:]) or "（今天还没有任何行动）"

    action_list = ", ".join(a.value for a in available_actions)

    if settings.realism_enabled:
        target_slug_contract = '"<居民slug、地点ID/名称或null>"'
        target_tile_contract = "null"
        movement_target_rule = (
            "- VISIT_DISTRICT/WANDER 可在 target_slug 填入地点ID（如 "
            "central_plaza, tavern 等）或地点名称（如 中央广场、酒馆 等），"
            "服务端会自动导航；自由闲逛填 null"
        )
    else:
        target_slug_contract = '"<居民slug或null>"'
        target_tile_contract = "[x, y] 或 null"
        movement_target_rule = (
            "- WANDER/VISIT_DISTRICT 填入 target_tile（使用地点入口坐标），"
            "其余为 null"
        )

    system = DECISION_SYSTEM.format(
        name=resident.name,
        current_location=current_location,
        sbti_type=sbti_type,
        sbti_name=sbti_name,
        status=resident.status,
        available_actions=action_list,
        today_action_count=len(today_actions),
        max_daily_actions=max_daily_actions,
        location_boost_hint=location_boost_hint,
        target_slug_contract=target_slug_contract,
        target_tile_contract=target_tile_contract,
        movement_target_rule=movement_target_rule,
    )
    user = DECISION_USER.format(
        world_time=world_time,
        schedule_phase=schedule_phase,
        nearby_residents_text=nearby_text,
        recent_memories_text=memory_text,
        today_actions_text=today_text,
    )
    # S1: fold any active world events into the decision prompt.
    if world_events:
        titles = "、".join(e.get("title", "") for e in world_events if e.get("title"))
        if titles:
            user += f"\n\n当前世界事件：{titles}"
        # E6: weather nudges outdoor actions with a soft hint — the schedule
        # itself is untouched (see scheduler.build_schedule), the LLM just gets
        # told the sky's opinion about WANDER/VISIT_DISTRICT.
        kind = next((
            (e.get("payload_json") or {}).get("kind")
            for e in world_events if e.get("type") == "weather"
        ), None)
        if kind == "rain":
            user += "\n（下雨，不太想出门——WANDER/VISIT_DISTRICT 这类室外行动能免则免）"
        elif kind == "storm":
            user += "\n（暴风雨，尽量待在室内，别选室外行动）"
        elif kind == "snow":
            user += "\n（下雪了，出门会踩一脚雪，不过看雪景也不错）"
    # 世界公共记忆(S5):「小镇现况」的裁剪子集,紧跟世界事件 —— 前者是「现在是
    # 什么样」,后者是「正在发生什么」,一起构成 NPC 对镇上的公共认知。渲染函数与
    # 玩家对话共用,标题在这里自己拼:决策 prompt 不用 markdown,沿用上面「当前
    # 世界事件：」的裸冒号句式。
    if town_facts:
        from app.llm.prompt import format_town_facts
        facts_text = format_town_facts(town_facts)
        if facts_text:
            user += f"\n\n小镇现况：\n{facts_text}"
    # 社交软提示（burn-in 发现：自然互聊为零；有邻居时轻推一把，不强制）
    if nearby_residents and ActionType.CHAT_RESIDENT in available_actions:
        names = "、".join(r.name for r in nearby_residents[:3])
        user += (
            f"\n附近有可以交谈的居民：{names}。"
            "如果当前没有更重要的事，主动搭话（CHAT_RESIDENT）能带来新鲜事和关系进展。"
        )
    # E1: current mood + a soft behavior hint (prompt hint, not a hard filter).
    mood = resident.mood_json or {}
    label = mood.get("label")
    if label:
        user += f"\n\n当前心情：{label}"
        valence = float(mood.get("valence", 0))
        if valence < -0.4:
            user += "（心情低落，可能更想独处、回家、写点东西或小憩）"
        elif valence > 0.5:
            user += "（心情很好，可能更想social、找人聊天）"
    return system, user


def _tile_dist(a, b) -> int:
    return abs(a.tile_x - b.tile_x) + abs(a.tile_y - b.tile_y)


# ── Inter-Resident Chat Prompts ────────────────────────────────────────

CHAT_INITIATE_SYSTEM = """\
你是 {initiator_name}，一个 Simverse World 的居民（SBTI：{sbti_type} {sbti_name}）。
你主动走向 {target_name} 并开始对话。

你的人格：
{persona_md}

你对 {target_name} 的记忆：
{relationship_memory}

请用中文，以符合你人格的方式开场白。保持简短（30字以内）。
"""

CHAT_REPLY_SYSTEM = """\
你是 {responder_name}，一个 Simverse World 的居民（SBTI：{sbti_type} {sbti_name}）。
{initiator_name} 正在和你对话。

你的人格：
{persona_md}

你对 {initiator_name} 的记忆：
{relationship_memory}

请用中文，以符合你人格的方式回应。保持简短（50字以内）。
"""
# E-02: the dialog history is already supplied as the user message; a {history}
# slot here double-injected it (~23% wasted dialog input), so it was removed.

CHAT_SUMMARY_SYSTEM = """\
请将以下居民间的对话总结成 1-2 句话，供玩家看到时理解发生了什么。
用第三人称描述，例如"小明和小红讨论了..."。
不要透露完整对话内容，只概括核心事件和情感变化。

输出格式：
{{"summary": "...", "mood": "positive/neutral/negative"}}
"""

CHAT_SUMMARY_USER = """\
{initiator_name} 和 {target_name} 的对话：

{dialog_text}
"""
