"""BasicExecutePlugin: handle movement and status changes."""
from __future__ import annotations

import logging
from typing import Any

from app.agent.actions import ActionType
from app.config import settings
from app.agent.map_data import get_valid_target_tile
from app.agent.pathfinder import get_walkable_tiles, find_path
from app.agent.schemas import TickContext

logger = logging.getLogger(__name__)


def _weather_kind(world_events) -> str | None:
    for e in world_events or []:
        if e.get("type") == "weather":
            return (e.get("payload_json") or {}).get("kind")
    return None


def _energy_critical(resident) -> bool:
    from app.agent.needs import get_needs
    return get_needs(resident).get("energy", 1.0) < settings.realism_needs_critical


def _is_market_day(world_events) -> bool:
    for e in world_events or []:
        if (e.get("payload_json") or {}).get("market_day"):
            return True
    return False


async def _charge_meal(db, resident) -> None:
    """M1 F1.2: debit the meal cost from the resident's treasury. On an empty
    wallet the resident eats on credit — recorded as a memory and a small tie to
    the dining-location's shopkeeper. Fail-open.

    M-A C1: with npc_trade_enabled the cost is no longer burned — it moves to the
    dining spot's proprietor (cafe_host / tavern_hub), the town's two zero-income
    duties. Missing proprietor, or the diner *is* the proprietor → the legacy
    sink debit. 赊账 is untouched: an empty wallet just means the transfer never
    happens (收费 + 穷人保障, no new mechanism)."""
    slug = resident.slug  # a rollback expires the ORM object — read it up front
    try:
        from app.services import coin_service
        from app.services.duty_service import set_wallet_cache, find_duty_resident
        from app.agent.map_data import get_location_id_at, location_category

        cost = settings.npc_meal_cost_sc
        # 经营者解析提前一次,转账目标与赊账分支两用 (cafe_host / tavern_hub)。
        loc_id = get_location_id_at(resident.tile_x, resident.tile_y)
        host = None
        if location_category(loc_id) == "dining":
            key = "cafe_host" if loc_id == "cafe" else "tavern_hub"
            host = await find_duty_resident(db, key)

        to_host = settings.npc_trade_enabled and host is not None and host.slug != slug
        if to_host:
            paid = await coin_service.treasury_transfer(db, slug, host.slug, cost,
                                                        reason="meal")
        else:
            paid = await coin_service.treasury_debit(db, slug, cost, reason="meal")
        balance = await coin_service.treasury_balance(db, slug)
        set_wallet_cache(db, resident, balance)
        if paid:
            if to_host:
                host_balance = await coin_service.treasury_balance(db, host.slug)
                set_wallet_cache(db, host, host_balance)
            await db.commit()
            if to_host:
                try:
                    from app.services.feed_service import push
                    await push(host.slug, "meal_income",
                               {"from": slug, "name": resident.name,
                                "amount": cost, "location": loc_id})
                except Exception:
                    logger.debug("meal income feed push failed for %s", host.slug,
                                 exc_info=True)
            return

        # 赊账: the proprietor resolved above carries the tab.
        from app.memory.service import MemoryService
        where = f"在{loc_id}" if loc_id else "在店里"
        note = (f"{where}赊了一顿饭,{host.name}说下次一起算。" if host
                else f"{where}赊了一顿饭,手头实在是紧。")
        await MemoryService(db).add_memory(
            resident.id, "event", note, 0.5, "observation",
            related_resident_id=host.id if host else None,
        )
        if host is not None:
            from app.services import relation_service
            await relation_service.bump(db, resident.id, host.id, d_familiarity=0.02)
        await db.commit()
    except Exception:
        # M-A C1: 写了半截就必须就地回滚 —— 悬挂的 debit 会被后续无关 commit
        # 落库烧钱(转账的 credit 段抛异常就是这个窗口)。
        try:
            await db.rollback()
        except Exception:
            pass
        logger.warning("meal charge failed for %s", slug, exc_info=True)


def _effective_speed(base: int, weather_kind: str | None, arousal: float | None) -> int:
    """Realism P1-7: tiles walked per tick = base × weather × arousal (min 1).
    rain 0.75 / storm 0.5 / snow 0.6; arousal>0.7 ×1.2 (in a hurry)."""
    factor = 1.0
    if weather_kind == "rain":
        factor *= settings.realism_move_rain
    elif weather_kind == "storm":
        factor *= settings.realism_move_storm
    elif weather_kind == "snow":
        factor *= settings.realism_move_snow
    if arousal is not None and arousal > settings.realism_move_arousal_threshold:
        factor *= settings.realism_move_arousal_boost
    return max(1, round(base * factor))


class BasicExecutePlugin:
    def __init__(self, params: dict[str, Any] | None = None):
        params = params or {}
        self.max_steps: int = params.get("max_steps_per_tick", 1)

    async def execute(self, ctx: TickContext) -> TickContext:
        if ctx.action_result is None:
            return ctx

        action = ctx.action_result.action
        movement_actions = {ActionType.WANDER, ActionType.GO_HOME, ActionType.VISIT_DISTRICT}

        try:
            if action in movement_actions:
                # Resolve target tile
                target = ctx.action_result.target_tile
                if action == ActionType.GO_HOME:
                    # Use home_location_id entrance
                    home_loc_id = getattr(ctx.resident, 'home_location_id', None)
                    if home_loc_id:
                        target = get_valid_target_tile(home_loc_id)
                    elif ctx.resident.home_tile_x is not None:
                        target = (ctx.resident.home_tile_x, ctx.resident.home_tile_y)

                if target:
                    walkable = get_walkable_tiles()
                    path = find_path(
                        (ctx.resident.tile_x, ctx.resident.tile_y),
                        target,
                        walkable,
                    )
                    if path and len(path) >= 2:
                        # Realism P1-7: advance up to `speed` path tiles per tick
                        # (weather/arousal-modulated), instead of a single tile.
                        if settings.realism_enabled:
                            arousal = (ctx.resident.mood_json or {}).get("arousal")
                            speed = _effective_speed(
                                settings.realism_move_speed,
                                _weather_kind(getattr(ctx, "world_events", None)),
                                arousal,
                            )
                        else:
                            speed = 1
                        idx = min(speed, len(path) - 1)
                        next_tile = path[idx]
                        ctx.resident.tile_x = next_tile[0]
                        ctx.resident.tile_y = next_tile[1]
                        ctx.resident.status = "walking"
                        ctx.new_tile = next_tile
                    else:
                        # Already at destination or unreachable — reset to idle.
                        # Realism P1-10: arriving home exhausted → sleep (energy
                        # recovers overnight; loop wakes within the schedule window).
                        if (settings.realism_enabled and action == ActionType.GO_HOME
                                and _energy_critical(ctx.resident)):
                            ctx.resident.status = "sleeping"
                        else:
                            ctx.resident.status = "idle"
                        ctx.new_tile = (ctx.resident.tile_x, ctx.resident.tile_y)
                    await ctx.db.commit()
                else:
                    # No valid target — reset to idle
                    ctx.resident.status = "idle"
                    await ctx.db.commit()
            elif action in {ActionType.IDLE, ActionType.NAP, ActionType.REFLECT, ActionType.JOURNAL}:
                if ctx.resident.status not in ("chatting", "socializing"):
                    ctx.resident.status = "idle"
                    await ctx.db.commit()
                # A4: a JOURNAL action may become a published creation (rule-gated).
                if action == ActionType.JOURNAL:
                    try:
                        from app.services.bulletin_service import maybe_create_journal_post
                        await maybe_create_journal_post(ctx.db, ctx.resident)
                    except Exception:
                        logger.warning("journal post attempt failed for %s", ctx.resident.slug, exc_info=True)
            elif action == ActionType.RESEARCH:
                # Lab narrative sync only: flag the researcher as researching so
                # the world sees "XX 正在实验楼做研究". The real sandbox work is
                # driven entirely by the Lab Runner (spec §5.4) — zero external
                # I/O on the tick.
                if ctx.resident.status not in ("chatting", "socializing"):
                    ctx.resident.status = "researching"
                    await ctx.db.commit()
            elif action == ActionType.WORK:
                # Duty system: WORK at one's job produces the duty's real output
                # (commission / bulletin / world event / sketch). on_work is
                # internally fail-open + cooldown-limited; residents without a
                # duty fall through as a no-op (narrative-only WORK, as before).
                from app.services.duty_service import on_work
                market_day = _is_market_day(getattr(ctx, "world_events", None))
                duty_line = await on_work(ctx.db, ctx.resident, market_day=market_day)
                if duty_line:
                    logger.info("duty output: %s", duty_line)
            elif action == ActionType.EAT:
                # Realism P1-10: pure state change — restore satiety (must be in a
                # dining location, enforced by get_available_actions).
                if ctx.resident.status not in ("chatting", "socializing"):
                    ctx.resident.status = "idle"
                if settings.realism_enabled:
                    from app.agent.needs import get_needs, write_needs
                    needs = get_needs(ctx.resident)
                    needs["satiety"] = min(1.0, needs["satiety"] + settings.realism_eat_restore)
                    write_needs(ctx.resident, needs)
                await ctx.db.commit()
                # M1 F1.2: eating costs money — debit the treasury; if the wallet
                # is empty the resident 赊账, which becomes a memory + a small tie
                # to the shopkeeper (gossip fodder). Fail-open, gated on economy.
                if settings.npc_economy_enabled:
                    await _charge_meal(ctx.db, ctx.resident)
        except Exception as e:
            logger.warning("Execute failed for %s: %s", ctx.resident.slug, e)

        return ctx
