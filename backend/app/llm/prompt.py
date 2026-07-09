from app.models.resident import Resident


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


def assemble_system_prompt(
    resident: Resident,
    memory_context: dict | None = None,
    world_events: list[dict] | None = None,
    life_goal: dict | None = None,
) -> str:
    """Assemble the three-layer system prompt from resident data.

    Optionally includes memory context, active world events (S1), and the
    resident's current life goal (A1) if provided.
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

    parts.append("请始终保持角色扮演，用你的人格风格回应访客。回复简洁，不超过200字。")
    return "\n".join(parts)
