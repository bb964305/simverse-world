"""BasicDecidePlugin: decide next action, plan-aware with hybrid execution."""
from __future__ import annotations

import logging
import random
from datetime import timedelta
from typing import Any

from app.agent.actions import ActionType, ActionResult, get_available_actions
from app.agent.prompts import build_decision_prompt
from app.agent.schemas import HourlyPlan, TickContext, parse_action_result
from app.config import settings
from app.llm.client import chat as llm_chat
from app.llm.metering import Meter
from app.memory.service import MemoryService

logger = logging.getLogger(__name__)

# Movement actions whose target tile is resolved server-side (realism P0-1).
# GO_HOME is excluded — execute resolves the home entrance itself.
_MOVEMENT_ACTIONS = {ActionType.WANDER, ActionType.VISIT_DISTRICT}


def _weather_kind(world_events) -> str | None:
    for e in world_events or []:
        if e.get("type") == "weather":
            return (e.get("payload_json") or {}).get("kind")
    return None


def _needs_prompt_hint(resident) -> str:
    """Realism P1-10: a one-line need summary softly injected into the decide
    prompt so the LLM's non-forced choices lean toward the resident's state."""
    from app.agent.needs import get_needs
    needs = get_needs(resident)
    parts = []
    if needs["satiety"] < 0.4:
        parts.append("有点饿了")
    if needs["energy"] < 0.4:
        parts.append("有些疲惫")
    if needs["social"] < 0.4:
        parts.append("想找人说说话")
    return f"\n（你现在{'，'.join(parts)}）" if parts else ""


class BasicDecidePlugin:
    def __init__(self, params: dict[str, Any] | None = None):
        params = params or {}
        self.interrupt_threshold: int = params.get("interrupt_threshold", 6)
        self.plan_adherence_hint: bool = params.get("plan_adherence_hint", True)
        # E-09/E-10 (largest cost lever,全服省 29–37%): with a fresh plan, execute
        # it rule-based (no decide LLM) unless a rule-level interrupt fires. Off by
        # default at the plugin level; enabled in the shipped agent YAML configs.
        self.skip_decide_when_planned: bool = params.get("skip_decide_when_planned", False)
        # Newest event memory at/above this importance (0–1 scale) counts as a
        # fresh notable event -> interrupt and re-decide with the LLM.
        self.interrupt_memory_importance: float = params.get("interrupt_memory_importance", 0.8)
        self.social_interrupt_chance: float = params.get("social_interrupt_chance", 0.15)

    async def execute(self, ctx: TickContext) -> TickContext:
        ctx.available_actions = get_available_actions(ctx.resident, ctx.nearby_residents)
        await self._load_memories(ctx)

        plan = ctx.current_plan

        # Case 1: High-importance plan -> force execute
        if (plan and plan.importance >= self.interrupt_threshold
                and not ctx.continuation_trip):
            result = self._force_execute_plan(plan, ctx)
            if result:
                ctx.action_result = result
                ctx.plan_followed = True
                plan.status = "executing"
                return ctx

        # Realism P1-10: needs arbitration — a critical need (<0.25) forces the
        # matching behavior (zero LLM). Below a high-importance plan, above the
        # weather/plan-skip paths.
        needs_action = self._maybe_needs_action(ctx)
        if needs_action is not None:
            await self._clear_continuation(ctx, "critical_need")
            ctx.action_result = needs_action
            ctx.plan_followed = False
            if plan:
                plan.status = "interrupted"
            return ctx

        # Realism P1-8: rule-level weather interrupt — duck out of rain/storm to
        # the nearest indoor place (zero LLM). Below a high-importance plan,
        # above the plan-skip fast path.
        shelter = self._maybe_shelter(ctx)
        if shelter is not None:
            await self._clear_continuation(ctx, "severe_weather")
            ctx.action_result = shelter
            ctx.plan_followed = False
            if plan:
                plan.status = "interrupted"
            return ctx

        # Once a trip has started it stays sticky against crowd and ordinary
        # social noise, but never outranks the survival/weather rules above.
        if settings.realism_plan_continuity_enabled and ctx.continuation_trip:
            result = await self._continue_active_trip(ctx)
            if result is not None:
                ctx.action_result = result
                ctx.plan_followed = True
                return ctx

        # Realism P2-7: festival/script events draw a crowd — with the crowd gate
        # on, the event location wins the VISIT_DISTRICT draw ×3 (人流聚集, zero LLM).
        crowd = await self._maybe_crowd_draw(ctx)
        if crowd is not None:
            ctx.action_result = crowd
            ctx.plan_followed = False
            if plan:
                plan.status = "interrupted"
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
        # call when nothing warrants reconsidering. force_plan_only (budget 95%+)
        # hard-disables interrupts — the breaker's rule-based fallback.
        if plan and (self.skip_decide_when_planned or ctx.force_plan_only):
            interrupt_reason = None if ctx.force_plan_only else await self._should_interrupt(ctx)
            if ctx.force_plan_only or interrupt_reason is None:
                result = self._force_execute_plan(plan, ctx)
                if result:
                    ctx.action_result = result
                    ctx.plan_followed = True
                    plan.status = "executing"
                    return ctx
                # Plan not executable now (e.g. target left). Under a budget
                # crunch, skip the tick rather than spend on an LLM decide.
                if ctx.force_plan_only:
                    ctx.skip_remaining = True
                    return ctx
            else:
                ctx.plan_interrupt_reason = interrupt_reason

        # Case 3: no plan, an interrupt fired, or the plan was unexecutable -> LLM
        try:
            action_result = await self._llm_decide(ctx)
        except Exception as e:
            logger.warning("Decide LLM failed for %s: %s", ctx.resident.slug, e)
            ctx.skip_remaining = True
            return ctx

        if action_result is None:
            ctx.skip_remaining = True
            return ctx

        if action_result.action not in ctx.available_actions:
            logger.debug("Resident %s chose unavailable action %s", ctx.resident.slug, action_result.action)
            ctx.skip_remaining = True
            return ctx

        ctx.action_result = action_result

        if plan:
            try:
                planned_action = ActionType(plan.action)
                if action_result.action == planned_action:
                    ctx.plan_followed = True
                    plan.status = "executing"
                else:
                    ctx.plan_followed = False
                    plan.status = "interrupted"
            except ValueError:
                ctx.plan_followed = False

        return ctx

    async def _should_interrupt(self, ctx: TickContext) -> str | None:
        """Rule-level interrupt detection (E-09/E-10): should the fresh plan be
        overridden by an LLM decision? Uses only TickContext data — zero LLM.

        Two signals:
        - a fresh notable event: the newest event memory is high-importance
          (memories are loaded newest-first, so this approximates "just happened");
        - a social opportunity: a partner is available nearby (CHAT_RESIDENT is in
          available_actions) and the plan isn't already social.
        """
        if not settings.realism_plan_continuity_enabled:
            if ctx.memories:
                newest = ctx.memories[0]
                importance = getattr(newest, "importance", None)
                if importance is not None and importance >= self.interrupt_memory_importance:
                    return "notable_event"
            plan = ctx.current_plan
            if ActionType.CHAT_RESIDENT in ctx.available_actions:
                if plan is None or plan.action != ActionType.CHAT_RESIDENT.value:
                    return "social"
            return None

        plan = ctx.current_plan
        if plan is None:
            return None

        async def _claim(reason: str) -> str | None:
            from app.agent.plan_continuity import claim_slot_interrupt
            claimed = await claim_slot_interrupt(
                ctx.resident.id, ctx.plan_date, plan.slot, reason)
            return reason if claimed else None

        if ctx.memories:
            newest = ctx.memories[0]
            importance = getattr(newest, "importance", None)
            if importance is not None and importance >= self.interrupt_memory_importance:
                created_at = getattr(newest, "created_at", None)
                fresh = False
                if created_at is not None:
                    from app.world_clock import now_world, real_to_world
                    age = now_world() - real_to_world(created_at)
                    fresh = timedelta(0) <= age <= timedelta(
                        minutes=settings.realism_notable_event_max_world_minutes)
                if fresh:
                    return await _claim("notable_event")

        # A route already in progress is sticky against ordinary social noise.
        # Critical needs/weather/crowd have already been arbitrated above.
        if ctx.resident.status == "walking":
            return None

        if (ActionType.CHAT_RESIDENT in ctx.available_actions
                and plan.action != ActionType.CHAT_RESIDENT.value
                and plan.importance <= settings.realism_social_interrupt_max_importance):
            from app.agent.needs import get_needs
            if (get_needs(ctx.resident).get("social", 1.0)
                    <= settings.realism_social_interrupt_need_max):
                claimed = await _claim("social")
                if claimed and random.random() < self.social_interrupt_chance:
                    return claimed

        return None

    async def _continue_active_trip(self, ctx: TickContext) -> ActionResult | None:
        """Rehydrate the saved route; no LLM and no new action-cap charge."""
        trip = ctx.continuation_trip or {}
        if trip.get("kind") == "market_day":
            from app.services.crowd_service import active_market_day_id

            if active_market_day_id(ctx.world_events) != trip.get("event_id"):
                await self._clear_continuation(ctx, "market_closed")
                return None
        try:
            action = ActionType(trip.get("action"))
        except (TypeError, ValueError):
            await self._clear_continuation(ctx, "invalid_action")
            return None
        if action not in _MOVEMENT_ACTIONS | {ActionType.GO_HOME}:
            await self._clear_continuation(ctx, "invalid_action")
            return None
        raw_tile = trip.get("target_tile")
        if not (isinstance(raw_tile, list) and len(raw_tile) == 2):
            await self._clear_continuation(ctx, "invalid_target")
            return None
        try:
            target_tile = (int(raw_tile[0]), int(raw_tile[1]))
        except (TypeError, ValueError):
            await self._clear_continuation(ctx, "invalid_target")
            return None
        plan = HourlyPlan(
            slot=int(trip.get("plan_slot") or 0),
            hour_range=(ctx.hour, ctx.hour + 1),
            action=action.value,
            target=trip.get("target"),
            location=trip.get("location"),
            importance=int(trip.get("importance") or 3),
            reason=str(trip.get("reason") or "继续前往目的地"),
            status="executing",
        )
        ctx.current_plan = plan
        ctx.scheduled_plan = plan
        ctx.plan_date = trip.get("plan_date")
        return ActionResult(
            action=action,
            target_slug=trip.get("target"),
            target_tile=target_tile,
            reason="继续完成已开始的行程",
        )

    @staticmethod
    async def _clear_continuation(ctx: TickContext, reason: str) -> None:
        if ctx.continuation_trip is None:
            return
        from app.agent.plan_continuity import clear_active_trip
        try:
            await clear_active_trip(ctx.resident.id, reason=reason)
        except Exception:
            logger.warning(
                "Active plan trip clear failed for %s reason=%s",
                ctx.resident.slug, reason,
            )
        ctx.continuation_trip = None
        ctx.plan_interrupt_reason = reason

    def _maybe_needs_action(self, ctx: TickContext) -> ActionResult | None:
        """Realism P1-10: force a behavior when a need is critical. energy→GO_HOME
        (execute sleeps once home); satiety→EAT here or head to nearest dining.
        social is soft (CHAT weight / prompt), not a hard force — returns None."""
        if not settings.realism_enabled:
            return None
        from app.agent.needs import get_needs, most_critical
        crit = most_critical(get_needs(ctx.resident))
        if crit is None or crit == "social":
            return None
        from app.agent.map_data import (
            get_location_id_at, location_category, nearest_dining_location,
            get_valid_target_tile,
        )
        if crit == "energy":
            if ActionType.GO_HOME in ctx.available_actions:
                return ActionResult(ActionType.GO_HOME, None, None, "精力耗尽，回家休息")
            return None
        # satiety
        # P1: 与 actions.py 的 EAT 门挂同一道闸、用同一个 resolver。口径分叉 =
        # 「EAT 已解锁,_maybe_needs_action 却判此处不是餐馆」→ 走
        # nearest_dining_location → 目标恰是脚下这栋楼 → VISIT_DISTRICT 到自己的
        # entrance → execute already-at-destination → 不进食 → satiety 单调到 0,
        # 而 most_critical 取 min 后恒返 satiety,GO_HOME 被永久挡在门外。这就是
        # 0809「7/11 居民饿死在自家门口」的同型链。
        if settings.location_capabilities_enabled:
            from app.agent.location_caps import CAP_DINING
            from app.agent.map_data import capability_location_at
            here = capability_location_at(
                ctx.resident.tile_x, ctx.resident.tile_y, CAP_DINING)
            dining_here = here is not None
        else:
            here = get_location_id_at(ctx.resident.tile_x, ctx.resident.tile_y)
            dining_here = location_category(here) == "dining"
        if dining_here and ActionType.EAT in ctx.available_actions:
            return ActionResult(ActionType.EAT, here, None, "饿了，吃点东西")
        target = nearest_dining_location((ctx.resident.tile_x, ctx.resident.tile_y))
        if target and ActionType.VISIT_DISTRICT in ctx.available_actions:
            return ActionResult(
                ActionType.VISIT_DISTRICT, target, get_valid_target_tile(target), "去找吃的")
        return None

    def _maybe_shelter(self, ctx: TickContext) -> ActionResult | None:
        """Realism P1-8: in rain/storm, an outdoor resident reroutes to the
        nearest indoor location with probability realism_shelter_prob."""
        if not settings.realism_enabled:
            return None
        if _weather_kind(getattr(ctx, "world_events", None)) not in ("rain", "storm"):
            return None
        if ActionType.VISIT_DISTRICT not in ctx.available_actions:
            return None
        from app.agent.map_data import (
            get_location_id_at, location_is_indoor, nearest_indoor_location,
            get_valid_target_tile,
        )
        here = get_location_id_at(ctx.resident.tile_x, ctx.resident.tile_y)
        if here and location_is_indoor(here):
            return None  # already sheltered
        if random.random() >= settings.realism_shelter_prob:
            return None
        target_id = nearest_indoor_location((ctx.resident.tile_x, ctx.resident.tile_y))
        if not target_id:
            return None
        return ActionResult(
            action=ActionType.VISIT_DISTRICT, target_slug=target_id,
            target_tile=get_valid_target_tile(target_id), reason="躲雨",
        )

    async def _maybe_crowd_draw(self, ctx: TickContext, rng=random) -> ActionResult | None:
        """Realism P2-7: during an active festival/script event, the event location
        gets a ×realism_festival_weight pull in the VISIT_DISTRICT draw.  An
        active market day additionally gives its persisted cohort of at most
        four real residents a direct pull to the market hall. Generic festival
        crowd draws are gated on the crowd flag; durable caravan invitations are
        not. High-priority/urgent/active-trip behavior is handled before here."""
        # A durable caravan invitation is gameplay authority, not a cosmetic
        # realism effect. Generic festival crowds may stay disabled while the
        # four persisted buyers still need a complete route to their purchase
        # slots. Without either feature there is no reason to query a cohort.
        crowd_enabled = settings.realism_crowd_enabled
        lifecycle_market_enabled = settings.caravan_lifecycle_enabled
        if not crowd_enabled and not lifecycle_market_enabled:
            return None
        if ActionType.VISIT_DISTRICT not in ctx.available_actions:
            return None
        # A cached cohort can be a few seconds stale. Never use it to pull a
        # resident out of a live conversation or an already-started journey.
        if ctx.resident.status in ("sleeping", "chatting", "socializing"):
            return None
        if ctx.continuation_trip is not None:
            return None
        # Returning home is not an entertainment plan. Critical energy and an
        # active GO_HOME trip are already protected above; this also protects
        # the first step before continuity state has been persisted.
        if any(
            plan is not None and plan.action == ActionType.GO_HOME.value
            for plan in (ctx.current_plan, ctx.scheduled_plan)
        ):
            return None
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
            target_tile = crowd_service.market_day_visitor_tile(
                ctx.resident.id, cohort, world_events,
            )
            ctx.market_trip_event_id = crowd_service.active_market_day_id(world_events)
        else:
            if not crowd_enabled:
                return None
            # Keep the established ×3 draw for non-market festivals and for
            # residents outside the bounded deterministic market cohort.
            target = crowd_service.festival_draw_target(world_events, here, rng)
            target_tile = None
        if not target:
            return None
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
        那个现场」两侧不互相硬编码 slug,二来本文件被 P1-S9 的守卫读全文,map_data 的
        两个能力反查 helper 的名字在这里一个字都不能出现(注释与 docstring 同样算数)。

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
        当舞台」两侧不互相硬编码 slug,二来本文件被 P1-S9 的守卫扫过 —— map_data 的
        两个能力反查 helper 的名字在这里一个字都不能出现(注释与 docstring 同样算数),
        也不得留裸的地点字面量。

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
        """Realism P2-7 herd micro-rule: soft "那边好像很热闹" nudge when the
        resident's social need is low and a nearby spot is already lively."""
        from app.agent.needs import get_needs
        social = get_needs(ctx.resident).get("social", 1.0)
        if social >= settings.realism_crowd_social_max:
            return ""
        from app.agent.map_data import get_location_id_at, get_location_by_id
        from app.services import crowd_service
        counts = await crowd_service.location_resident_counts(ctx.db)
        here = get_location_id_at(ctx.resident.tile_x, ctx.resident.tile_y)
        busy = crowd_service.busiest_crowded_location(counts, exclude=here)
        if not busy:
            return ""
        loc = get_location_by_id(busy)
        name = (loc or {}).get("name", busy)
        return f"\n（{name}那边好像很热闹，很多人聚在那里。）"

    def _force_execute_plan(self, plan, ctx: TickContext) -> ActionResult | None:
        try:
            action = ActionType(plan.action)
        except ValueError:
            logger.warning("Invalid action in plan: %s", plan.action)
            return None
        if action not in ctx.available_actions:
            return None
        # Realism P0-1: resolve the target tile server-side from the plan's
        # location (id or display name); model-reported coords are ignored.
        target_tile = None
        if settings.realism_enabled and action in _MOVEMENT_ACTIONS:
            from app.agent.plan_target import resolve_location_id, resolve_target_tile
            canonical_target = resolve_location_id(plan.target, plan.location)
            target_tile = resolve_target_tile(canonical_target, plan.location)
        else:
            canonical_target = plan.target
        return ActionResult(
            action=action,
            target_slug=canonical_target,
            target_tile=target_tile,
            reason=plan.reason[:100],
        )

    async def _town_facts(self, ctx: TickContext) -> dict | None:
        """世界公共记忆(S5):「小镇现况」的裁剪子集,给决策 prompt 用。

        只取 ``DECIDE_FACT_KEYS``(镇长 / 今天 / 进行中公投 / 地点)—— 政策与镇库
        一律不进这条链路(K4:tests/test_treasury_service.py:849-851 钉死了全文不
        得出现 tax / town_treasury / 镇财政 / 余额数字)。

        取数点在这里而不是 ``execute``:上面那几条零 LLM 的规则分支(needs / 躲雨
        / 凑热闹 / 照计划走)压根不拼 prompt,没必要为它们查一次库。闸关时
        ``get_town_facts_cached`` 直接返 ``{}``,连 db 都不碰。fail-open:事实取不
        到就不注入,决策照常跑。
        """
        try:
            from app.services.town_facts_service import (
                DECIDE_FACT_KEYS, get_town_facts_cached,
            )
            facts = await get_town_facts_cached(ctx.db)
            return {k: facts[k] for k in DECIDE_FACT_KEYS if k in facts}
        except Exception:
            logger.warning("Town facts fetch failed for %s", ctx.resident.slug, exc_info=True)
            return None

    async def _llm_decide(self, ctx: TickContext) -> ActionResult | None:
        # World time (agent-T): "today's actions" is a world-calendar concept.
        # Memories store created_at in real UTC, so each is mapped to world time
        # before its date key is compared against today's world date — otherwise
        # a raw strftime on the UTC timestamp compares real dates to a world key.
        from app.world_clock import world_date_key, real_to_world
        today_key = world_date_key()
        today_actions = [
            m.content for m in ctx.memories
            if m.created_at and real_to_world(m.created_at).strftime("%Y-%m-%d") == today_key
        ]
        ctx.today_actions = today_actions

        system_prompt, user_prompt = build_decision_prompt(
            resident=ctx.resident,
            schedule_phase=ctx.schedule_phase,
            world_time=ctx.world_time,
            nearby_residents=ctx.nearby_residents,
            memories=ctx.memories,
            today_actions=today_actions,
            available_actions=ctx.available_actions,
            max_daily_actions=settings.agent_max_daily_actions,
            world_events=ctx.world_events,
            town_facts=await self._town_facts(ctx),
        )

        if ctx.current_plan and self.plan_adherence_hint:
            plan = ctx.current_plan
            hint = f"\n\n你原本计划在这个时段 {plan.action}（{plan.reason}），但你可以根据当前情况改变主意。"
            user_prompt += hint

        if settings.realism_enabled:
            user_prompt += _needs_prompt_hint(ctx.resident)

        # Realism P2-7: herd micro-rule soft hint (independent crowd gate).
        if settings.realism_crowd_enabled:
            user_prompt += await self._crowd_hint(ctx)

        raw = await llm_chat(
            system_prompt, [{"role": "user", "content": user_prompt}], max_tokens=200,
            meter=Meter(scenario="decide", resident_id=ctx.resident.id), expects_json=True,
        )
        result = parse_action_result(raw)
        # Realism P0-1: ignore any model-reported target_tile for movement
        # actions; resolve it server-side from target_slug (tried as id and name).
        if (result is not None and settings.realism_enabled
                and result.action in _MOVEMENT_ACTIONS):
            from app.agent.plan_target import resolve_location_id, resolve_target_tile
            canonical_target = resolve_location_id(result.target_slug, result.target_slug)
            result.target_slug = canonical_target
            result.target_tile = resolve_target_tile(canonical_target, canonical_target)
        return result

    async def _load_memories(self, ctx: TickContext) -> None:
        try:
            memory_svc = MemoryService(ctx.db)
            ctx.memories = await memory_svc.get_memories(ctx.resident.id, type="event", limit=10)
        except Exception as e:
            logger.warning("Memory retrieval failed for %s: %s", ctx.resident.slug, e)
            ctx.memories = []
