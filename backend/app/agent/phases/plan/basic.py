"""BasicPlanPlugin: generate daily goal + hourly plans via LLM."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.agent.actions import ActionType
from app.agent.map_data import format_location_list_for_prompt
from app.agent.scheduler import build_schedule
from app.agent.schemas import TickContext, DailyGoal, HourlyPlan
from app.config import settings
from app.llm.client import chat as llm_chat
from app.llm.json_extract import extract_json_object
from app.llm.metering import Meter
from app.memory.service import MemoryService
from app.ws.manager import manager

logger = logging.getLogger(__name__)

PLAN_SYSTEM_PROMPT = """\
你是一个游戏 NPC 的日程规划器。根据居民的性格和记忆，生成今天的目标和分时段行动计划。

居民信息：
- 姓名：{name}
- 人格类型（SBTI）：{sbti_type}（{sbti_name}）
- 性格描述：{persona_snippet}
- 家：{home_name}

活跃时段：{wake_hour}:00 - {sleep_hour}:00，共 {slot_count} 个时段

小镇地图（可选目的地）：
{location_list}

可选行动：{action_types}

约束：
- importance 1-10，大部分为 2-4，最多 {max_high_importance} 个时段 >= 6
- 社交行动（CHAT_RESIDENT/GOSSIP）最多 {max_social_slots} 个时段
- 以第一人称自然表达目标，不要生硬的开头
- location 字段必须从上面的地点列表中选择地点名称
- target 字段：WANDER/VISIT_DISTRICT 使用目标地点的入口坐标 [x, y]，其余为 null
- GO_HOME 的 target 为 null（自动导航）
{preferred_actions_hint}

输出严格 JSON，不要其他文字：
{{
  "goal": {{"goal": "今日目标描述", "motivation": "动机"}},
  "plans": [
    {{"slot": 0, "hour_range": [{start_0}, {end_0}], "action": "ACTION_TYPE", "target": null, "location": "地点名称", "importance": 3, "reason": "原因"}},
    ...
  ]
}}
"""

#: 「镇上的事」那一段的标题。整段（标题 + 条目）只在真的取到公共记忆时才渲染 ——
#: 一个光秃秃的标题会让 LLM 去编一件根本没发生的事。
PUBLIC_MEMORY_HEADING = "镇上最近发生的事："

#: ``{public_memories}`` 空串时，本模板与改前（``69f07a7``）**逐字节**相同：
#: ``{recent_memories}\n`` + ``""`` + ``\n最近的关系：`` = ``…\n\n最近的关系：``。
#: 逐字节这件事由 backend/tests/test_plan_public_memories.py 的冻结快照对拍钉住。
PLAN_USER_PROMPT = """\
昨天做了什么：
{yesterday_summary}

最近的重要记忆：
{recent_memories}
{public_memories}
最近的关系：
{relationships}

请生成今天的目标和 {slot_count} 个时段的计划。
"""


class BasicPlanPlugin:
    def __init__(self, params: dict[str, Any] | None = None):
        params = params or {}
        self.plan_interval_hours: int = params.get("plan_interval_hours", 24)
        self.hourly_slots: int = params.get("hourly_slots", 7)
        self.max_social_slots: int = params.get("max_social_slots", 2)
        self.max_high_importance: int = params.get("max_high_importance", 2)
        self.preferred_actions: list[str] = params.get("preferred_actions", [])

    async def execute(self, ctx: TickContext) -> TickContext:
        # World time (agent-T): plans regenerate per WORLD day, so dedup keys off
        # the world date, not real wall-clock.
        from app.world_clock import world_date_key
        today = world_date_key()

        plans_data = ctx.resident.daily_plans_json
        is_fresh = (
            plans_data
            and isinstance(plans_data, dict)
            and plans_data.get("generated_date") == today
        )

        if not is_fresh:
            try:
                await self._generate_plan(ctx, today)
            except Exception as e:
                logger.warning("Plan generation failed for %s: %s", ctx.resident.slug, e)
                return ctx

        # Load daily goal into context
        goal_data = ctx.resident.daily_goal_json
        if goal_data:
            ctx.daily_goal = DailyGoal(
                goal=goal_data.get("goal", ""),
                motivation=goal_data.get("motivation", ""),
                created_at=goal_data.get("created_at", ""),
                status=goal_data.get("status", "active"),
            )

        # Find current time slot
        plans_data = ctx.resident.daily_plans_json
        if plans_data and "plans" in plans_data:
            for p in plans_data["plans"]:
                hr = p.get("hour_range", [0, 0])
                if hr[0] <= ctx.hour < hr[1]:
                    ctx.current_plan = HourlyPlan(
                        slot=p["slot"],
                        hour_range=tuple(hr),
                        action=p["action"],
                        target=p.get("target"),
                        location=p.get("location"),
                        # LLM 偶发漏 importance 键：缺省取 prompt 示例值 3，
                        # 硬下标会 KeyError 导致整个 phase 反复失败（vm212 生产 bug）
                        importance=p.get("importance", 3),
                        reason=p.get("reason", ""),
                        status=p.get("status", "pending"),
                    )
                    break

        return ctx

    @staticmethod
    async def _public_memories_block(
        memory_svc: MemoryService, resident_id: str, *, rendered_ids: set[str],
    ) -> str:
        """计划 prompt 里「镇上的事」那一段；闸关或取不到时返回**空串**。

        空串是本函数的默认答案，也是它唯一的旧行为：模板里 ``{public_memories}``
        为空时渲染出的字节与改前（``69f07a7``）一模一样。所以下面三条路径全都返回
        空串 —— 闸关（``<= 0``）、库里一条都没有、取数抛异常。

        **fail-open**：``_generate_plan`` 的异常会被 ``execute`` 吞成一行 warning，
        代价是这位居民**整天没有计划**（无目标、无时段计划）。一段锦上添花的 prompt
        不该有这个权力，所以这里自己兜住。

        ``rendered_ids`` 是已经渲染进 prompt 的记忆 id（个人近况 + 昨天那两段）：
        镇务结果档 importance=0.99，结票后的几分钟里它必然也在最近 20 条里 ——
        不去重的话同一句话在 prompt 里出现两次。
        """
        limit = settings.realism_plan_public_memories
        if limit <= 0:
            return ""
        try:
            public = await memory_svc.get_public_memories(resident_id, limit)
        except Exception:
            logger.debug("public memories fetch failed (fail-open)", exc_info=True)
            return ""
        lines = [f"- {m.content}" for m in public if m.id not in rendered_ids]
        if not lines:
            return ""
        return "\n" + PUBLIC_MEMORY_HEADING + "\n" + "\n".join(lines) + "\n"

    async def _generate_plan(self, ctx: TickContext, today: str) -> None:
        resident = ctx.resident
        sbti = (resident.meta_json or {}).get("sbti", {})
        schedule = build_schedule(sbti)

        # Compute time slots
        awake_hours = schedule.sleep_hour - schedule.wake_hour
        slot_duration = max(1, awake_hours // self.hourly_slots)
        slots_info = []
        for i in range(self.hourly_slots):
            start = schedule.wake_hour + i * slot_duration
            end = min(start + slot_duration, schedule.sleep_hour)
            if start >= schedule.sleep_hour:
                break
            slots_info.append((i, start, end))

        # Fetch memories for context
        memory_svc = MemoryService(ctx.db)
        recent_all = await memory_svc.get_memories(resident.id, type="event", limit=20)
        # Filter to importance > 0.5, take top 5
        recent = [m for m in recent_all if m.importance > 0.5][:5]
        if not recent:
            recent = recent_all[:3]  # fallback: at least some context
        rels = await memory_svc.get_memories(resident.id, type="relationship", limit=3)

        recent_text = "\n".join(f"- {m.content}" for m in recent) or "（无）"
        rels_text = "\n".join(f"- {m.content}" for m in rels) or "（无）"

        # Yesterday's event summary. Memories store created_at in real UTC, so we
        # convert each to world time before comparing against the world date — a
        # world "yesterday" is any memory dated before today's world date.
        from app.world_clock import now_world, real_to_world
        world_today = now_world().date()
        yesterday_events = [
            m for m in recent_all
            if m.created_at and real_to_world(m.created_at).date() < world_today
        ][:5]
        yesterday_text = "\n".join(f"- {m.content}" for m in yesterday_events) if yesterday_events else "（无）"

        # 「镇上的事」（REALISM_PLAN_PUBLIC_MEMORIES，默认 0 = 逐字节旧行为）。
        #
        # 上面那两条口径都**不动**：`limit=20` 是「个人近况」的口径（生产实测它只
        # 覆盖 20-30 分钟，因为每人每天写 480-545 条 event 记忆），`importance>0.5`
        # 筛掉的是天气（恒 0.5）与低价值 agent_action —— 两者都在做该做的事。
        # 缺的是另一件事：镇上出了什么（实质世界事件约 1.6 条/人/周，镇务结果更
        # 稀疏），落进那 20 分钟窗口的概率约等于 0 —— 生产实测六位居民的
        # world_event 计数**全是 0**。所以这里另开一段，用另一个口径去取。
        public_text = await self._public_memories_block(
            memory_svc, resident.id,
            # 已经渲染过的那些行（个人近况 + 昨天）不再出现第二次
            rendered_ids={m.id for m in recent} | {m.id for m in yesterday_events},
        )

        action_types = ", ".join(a.value for a in ActionType)

        # Resolve home name
        home_loc_id = resident.home_location_id
        home_name = "未分配"
        if home_loc_id:
            from app.agent.map_data import get_location_by_id
            home_loc = get_location_by_id(home_loc_id)
            if home_loc:
                home_name = home_loc["name"]

        location_list = format_location_list_for_prompt(
            from_tile=(ctx.resident.tile_x, ctx.resident.tile_y),
        )

        preferred_hint = ""
        if self.preferred_actions:
            preferred_hint = "- 偏好行为权重：" + ", ".join(self.preferred_actions)

        system_prompt = PLAN_SYSTEM_PROMPT.format(
            name=resident.name,
            sbti_type=sbti.get("type", "OJBK"),
            sbti_name=sbti.get("type_name", "无所谓人"),
            persona_snippet=(resident.persona_md or "")[:200],
            home_name=home_name,
            wake_hour=schedule.wake_hour,
            sleep_hour=schedule.sleep_hour,
            slot_count=len(slots_info),
            location_list=location_list,
            action_types=action_types,
            max_high_importance=self.max_high_importance,
            max_social_slots=self.max_social_slots,
            preferred_actions_hint=preferred_hint,
            start_0=slots_info[0][1] if slots_info else 7,
            end_0=slots_info[0][2] if slots_info else 9,
        )

        user_prompt = PLAN_USER_PROMPT.format(
            yesterday_summary=yesterday_text,
            recent_memories=recent_text,
            public_memories=public_text,
            relationships=rels_text,
            slot_count=len(slots_info),
        )

        # A1: align the daily plan with the resident's active life goal.
        # Lightweight single select, fail-open — a goal-fetch hiccup must
        # never block plan generation (mirrors the chat-prompt injection).
        try:
            from app.services.goal_service import get_active_goal
            goal_row = await get_active_goal(ctx.db, resident.id)
            title = getattr(goal_row, "title", None)
            if goal_row is not None and isinstance(title, str) and title:
                progress = float(goal_row.progress or 0)
                ctx.life_goal = {"title": title, "progress": progress}
                user_prompt = f"你的长期目标：{title}（进度 {progress:.0%}）\n\n" + user_prompt
        except Exception:
            logger.debug("life goal fetch failed (fail-open)", exc_info=True)

        raw = await llm_chat(
            system_prompt, [{"role": "user", "content": user_prompt}], max_tokens=1200,
            meter=Meter(scenario="plan", resident_id=resident.id), expects_json=True,
        )

        # Parse JSON response (strips fences, balanced-brace extraction, tolerates
        # trailing commas) — unified extractor (P1-1, E-05).
        data = extract_json_object(raw)
        if data is None:
            raise ValueError(f"No parseable JSON in plan response: {raw[:200]}")

        # Store goal
        goal = data.get("goal", {})
        resident.daily_goal_json = {
            "goal": goal.get("goal", "无目标"),
            "motivation": goal.get("motivation", ""),
            "created_at": datetime.now().isoformat(),
            "status": "active",
        }

        # Store plans with status field
        plans = data.get("plans", [])
        for p in plans:
            p["status"] = "pending"

        resident.daily_plans_json = {
            "generated_date": today,
            "plans": plans,
        }

        await ctx.db.commit()
        logger.info("Generated daily plan for %s: %s (%d slots)",
                     resident.slug, goal.get("goal", "?"), len(plans))

        # Broadcast plan generation event
        try:
            top_plan = max(plans, key=lambda p: p.get("importance", 0)) if plans else None
            await manager.broadcast({
                "type": "resident_plan_generated",
                "resident_slug": resident.slug,
                "goal": goal.get("goal", ""),
                "plan_count": len(plans),
                "top_plan": {
                    "action": top_plan["action"],
                    "importance": top_plan.get("importance", 3),
                    "hour_range": top_plan.get("hour_range", []),
                } if top_plan else None,
            })
        except Exception as e:
            logger.debug("Plan broadcast failed (non-fatal): %s", e)
