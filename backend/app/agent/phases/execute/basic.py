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
                        # Already at destination or unreachable — reset to idle
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
        except Exception as e:
            logger.warning("Execute failed for %s: %s", ctx.resident.slug, e)

        return ctx
